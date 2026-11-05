"""Agent control loop — the core ReAct-style execution engine.

Flow:
    Goal → Plan → [Observe → Decide → Permission Check → Act → Verify] → Done?
                    ↑                                                      |
                    +──────────── Adapt / Re-plan ←────────────────────────+

Hard limits cap steps, wall time, and repeated identical actions.
Every step is persisted to SQLite and checkpointed to disk.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from local_agent.config import AgentConfig
from local_agent.core.planner import Planner
from local_agent.core.state import AgentState
from local_agent.core.verifier import Verifier
from local_agent.memory.checkpoint import Checkpoint, CheckpointStore
from local_agent.memory.store import MemoryStore, RunRecord, RunStatus, StepRecord
from local_agent.memory.working import WorkingMemory
from local_agent.models import ModelManager
from local_agent.security.budget import BudgetExhaustedError, BudgetTracker
from local_agent.security.permissions import PermissionDeniedError
from local_agent.tools.base import VerificationStatus
from local_agent.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

EXECUTOR_SYSTEM_PROMPT = """\
You are an autonomous computer-using agent operating inside a sandboxed Linux environment.
You have access to tools: {tool_names}.

## Rules
- Execute ONE tool call per turn.
- After each action, wait for the result before deciding the next step.
- If a step fails, try a different approach (up to 3 retries).
- When the goal is achieved, respond with: DONE: <summary of what was accomplished>
- When you cannot proceed, respond with: BLOCKED: <reason>
- Keep your reasoning concise.

## Current State
{state_summary}
"""


class AgentLoop:
    """The main agent execution loop.

    Usage::

        loop = AgentLoop(config, model_manager, tool_registry, memory_store)
        result = await loop.run("Create a Python hello-world project with tests")
    """

    def __init__(
        self,
        config: AgentConfig,
        model_manager: ModelManager,
        tool_registry: ToolRegistry,
        memory_store: MemoryStore,
        checkpoint_store: CheckpointStore | None = None,
    ) -> None:
        self._config = config
        self._model = model_manager
        self._tools = tool_registry
        self._memory = memory_store
        self._checkpoint = checkpoint_store or CheckpointStore(config.trace_dir / "checkpoints")
        self._planner = Planner(model_manager)
        self._verifier = Verifier(model_manager)
        self._killed = False

    # ── Public API ───────────────────────────────────────────────────────

    def kill(self) -> None:
        """Signal the loop to stop after the current step."""
        self._killed = True

    async def run(self, goal: str, *, run_id: str | None = None) -> RunRecord:
        """Execute the full agent loop for *goal* and return the run record."""
        run_id = run_id or str(uuid.uuid4())
        self._killed = False

        # Initialise state.
        state = AgentState(run_id=run_id, goal=goal)
        self._memory.create_run(run_id, goal)

        budget = BudgetTracker(
            max_steps=self._config.max_steps,
            max_wall_time_seconds=self._config.max_wall_time_seconds,
            max_repeated_actions=self._config.max_repeated_actions,
        )
        budget.start()

        working = WorkingMemory(
            max_tokens=self._config.model_roles.get("executor", self._config.model_roles["planner"]).context_length,
            system_prompt="",  # Will be set dynamically each turn.
        )

        try:
            # ── Phase 1: Planning ────────────────────────────────────────
            state.status = RunStatus.PLANNING
            self._memory.update_run_status(run_id, RunStatus.PLANNING)

            plan, reasoning = await self._planner.create_plan(goal)
            state.plan = plan
            self._memory.update_run_plan(run_id, plan)

            logger.info("Plan for '%s': %s", goal, plan)

            # Record the planning step.
            plan_step = StepRecord(
                step_index=0,
                action_type="plan",
                decision=f"Plan: {json.dumps(plan)}",
                observation=reasoning,
                result_success=True,
            )
            state.record(plan_step)
            self._memory.record_step(run_id, plan_step)

            # ── Phase 2: Execution loop ──────────────────────────────────
            state.status = RunStatus.EXECUTING
            self._memory.update_run_status(run_id, RunStatus.EXECUTING)

            while not state.is_terminal and state.current_step < len(state.plan):
                if self._killed:
                    state.status = RunStatus.KILLED
                    break

                # Budget check.
                action_key = f"step_{state.current_step}"
                try:
                    budget.check(action_key)
                except BudgetExhaustedError as exc:
                    logger.warning("Budget exhausted: %s", exc.reason)
                    state.status = RunStatus.FAILED
                    state.last_error = exc.reason
                    break

                # Build the executor prompt.
                system = EXECUTOR_SYSTEM_PROMPT.format(
                    tool_names=", ".join(self._tools.names),
                    state_summary=state.summary_for_model(),
                )
                working.system_prompt = system
                working.set_plan(state.plan)
                working.set_scratchpad(state.scratchpad)

                # Add current task instruction.
                current_task = state.current_plan_step or "Continue working on the goal."
                working.add_message("user", f"Execute this step: {current_task}")

                # Get model decision.
                tool_defs = self._tools.get_tool_definitions()
                response = await self._model.chat(
                    "executor",
                    working.get_messages(),
                    tools=tool_defs if tool_defs else None,
                )

                step_index = len(state.history)

                # ── Handle tool calls ────────────────────────────────────
                if response.has_tool_calls:
                    tc = response.tool_calls[0]  # One tool per turn.
                    action_key = f"{tc.name}:{json.dumps(tc.arguments, sort_keys=True)[:100]}"

                    try:
                        budget.check(action_key)
                    except BudgetExhaustedError as exc:
                        state.status = RunStatus.FAILED
                        state.last_error = exc.reason
                        break

                    logger.info("Step %d: %s(%s)", step_index, tc.name, tc.arguments)

                    try:
                        result, verification = await self._tools.execute_and_verify(
                            tc.name, tc.arguments
                        )
                    except PermissionDeniedError as exc:
                        # Tool needs human approval.
                        state.status = RunStatus.AWAITING_HUMAN
                        state.last_error = str(exc)
                        step_rec = StepRecord(
                            step_index=step_index,
                            action_type="tool_call",
                            tool_name=tc.name,
                            tool_params=tc.arguments,
                            decision=response.content,
                            error=str(exc),
                            result_success=False,
                            verification_status="blocked",
                            verification_message=str(exc),
                        )
                        state.record(step_rec)
                        self._memory.record_step(run_id, step_rec)
                        break

                    # Record the step.
                    step_rec = StepRecord(
                        step_index=step_index,
                        action_type="tool_call",
                        tool_name=tc.name,
                        tool_params=tc.arguments,
                        decision=response.content,
                        result_output=result.truncated(),
                        result_success=result.success,
                        verification_status=verification.status.value,
                        verification_message=verification.message,
                        error=result.error,
                    )
                    state.record(step_rec)
                    self._memory.record_step(run_id, step_rec)
                    budget.record_step(action_key)

                    # Feed result back to working memory.
                    working.add_message("assistant", response.content)
                    working.add_tool_result(
                        tc.id,
                        f"Success: {result.success}\nOutput: {result.truncated()}"
                        + (f"\nError: {result.error}" if result.error else "")
                        + f"\nVerification: {verification.status.value} — {verification.message}",
                    )

                    # ── Decide next action based on result ───────────────
                    if result.success and verification.status != VerificationStatus.FAILED:
                        state.advance()
                    else:
                        state.mark_retry(result.error or verification.message)
                        if state.retry_count >= self._config.max_retries_per_step:
                            logger.warning("Max retries for step %d, re-planning", state.current_step)
                            try:
                                new_plan, reason = await self._planner.replan(state)
                                state.plan = state.plan[: state.current_step] + new_plan
                                self._memory.update_run_plan(run_id, state.plan)
                                state.retry_count = 0
                            except Exception:
                                logger.exception("Re-planning failed")
                                state.status = RunStatus.FAILED
                                break

                # ── Handle text-only response (DONE / BLOCKED) ───────────
                else:
                    content = response.content.strip()
                    step_rec = StepRecord(
                        step_index=step_index,
                        action_type="response",
                        decision=content,
                        result_success=True,
                    )
                    state.record(step_rec)
                    self._memory.record_step(run_id, step_rec)

                    if content.upper().startswith("DONE:"):
                        state.status = RunStatus.COMPLETED
                        state.scratchpad = content[5:].strip()
                    elif content.upper().startswith("BLOCKED:"):
                        state.status = RunStatus.AWAITING_HUMAN
                        state.last_error = content[8:].strip()
                    else:
                        # Model responded without calling a tool — nudge it.
                        working.add_message("assistant", content)
                        working.add_message(
                            "user",
                            "You must call a tool to make progress.  "
                            "Use one of the available tools, or respond with DONE: or BLOCKED: if finished.",
                        )
                        budget.record_step("nudge")
                        continue

                # Checkpoint after each step.
                self._save_checkpoint(state, working)

            # ── Finalise ─────────────────────────────────────────────────
            if state.status == RunStatus.EXECUTING:
                # Ran out of plan steps → assume completed.
                state.status = RunStatus.COMPLETED

        except Exception as exc:
            logger.exception("Agent loop crashed")
            state.status = RunStatus.FAILED
            state.last_error = f"Loop error: {exc}"

        # Persist final status.
        summary = state.scratchpad or state.last_error or f"Run {state.status.value}"
        self._memory.update_run_status(run_id, state.status, summary=summary)
        self._checkpoint.delete(run_id)

        return self._memory.get_run(run_id)  # type: ignore[return-value]

    # ── Internal helpers ─────────────────────────────────────────────────

    def _save_checkpoint(self, state: AgentState, working: WorkingMemory) -> None:
        cp = Checkpoint(
            run_id=state.run_id,
            goal=state.goal,
            step_index=state.current_step,
            plan=state.plan,
            scratchpad=state.scratchpad,
            status=state.status.value,
        )
        try:
            self._checkpoint.save(cp)
        except Exception:
            logger.warning("Failed to save checkpoint for run %s", state.run_id)
