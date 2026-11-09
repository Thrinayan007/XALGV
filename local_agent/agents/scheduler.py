"""Sequential dispatch with verified dependency release and bounded safe retries."""

import asyncio
import time

from pydantic import Field

from .base import (
    AgentResult,
    AgentRole,
    AttemptOutcome,
    Envelope,
    ErrorCode,
    Identifier,
    Summary,
    TaskState,
    VerificationReport,
)
from .context import current_task
from .messages import ArtifactHandoff
from .resources import ResourceUnavailable


class TaskExecution(Envelope):
    task_id: Identifier
    state: TaskState
    attempts: tuple[AgentResult, ...] = ()
    verification: VerificationReport | None = None
    detail: Summary | None = None
    failure_code: ErrorCode | None = None


class WorkflowResult(Envelope):
    run_id: Identifier
    tasks: tuple[TaskExecution, ...]
    final_checks: tuple[bool, ...]
    completed: bool
    wall_time_ms: int = Field(ge=0)
    detail: Summary | None = None
    model_tokens_used: int = Field(default=0, ge=0)


class Scheduler:
    def __init__(self, runtime):
        self.runtime = runtime

    async def run(self, graph, *, started: float, store, replan=None, restored=None) -> WorkflowResult:
        request = graph.request
        deadline = started + request.max_wall_time_seconds
        completed = dict(restored or {})
        reconsidered = False
        while pending := [task_id for task_id in graph.order if task_id not in completed]:
            task_id = pending[0]
            task = graph.task(task_id)
            if any(completed[parent].state != TaskState.COMPLETED for parent in task.dependencies):
                completed[task_id] = TaskExecution(
                    task_id=task_id, state=TaskState.BLOCKED, detail="A prerequisite did not pass verification",
                )
            elif task.agent == AgentRole.BROWSER and any(
                item.state == TaskState.OUTCOME_UNKNOWN and graph.task(key).agent == AgentRole.BROWSER
                for key, item in completed.items()
            ):
                completed[task_id] = TaskExecution(task_id=task_id, state=TaskState.BLOCKED,
                                                   detail="A browser outcome needs review before further browser work",
                                                   failure_code="BROWSER_OUTCOME_PENDING")
            elif deadline - time.monotonic() < 1:
                completed[task_id] = TaskExecution(task_id=task_id, state=TaskState.CANCELLED, detail="Workflow deadline expired")
            else:
                try:
                    manifests = store.manifests(request.run_id, task.dependencies)
                    if any(self.runtime.artifacts.manifest(path) != manifest
                           for entries in manifests.values() for path, manifest in entries.items()):
                        raise ValueError("Dependency artifact changed after verification")
                    task = graph.bind(task_id, manifests)
                    for item in task.inputs:
                        if self.runtime.artifacts.manifest(item.path).sha256 != item.sha256:
                            raise ValueError("Dependency artifact changed after verification")
                except (OSError, ValueError) as exc:
                    completed[task_id] = TaskExecution(task_id=task_id, state=TaskState.BLOCKED, detail=str(exc)[:2000])
                    store.finish_task(request.run_id, completed[task_id])
                    continue
                execution = await self._run_task(task, deadline, graph.tool_grant(task_id), manifests, store)
                completed[task_id] = execution
                if replan is not None and not reconsidered and execution.state == TaskState.FAILED:
                    ready = any(all(parent in completed and completed[parent].state == TaskState.COMPLETED
                                    for parent in graph.task(item).dependencies)
                                for item in pending[1:])
                    if ready and deadline - time.monotonic() >= 10:
                        reconsidered = True
                        revised = await replan(graph, completed)
                        if revised is not graph:
                            store.revise(revised, completed)
                            graph = revised
                continue
            store.finish_task(request.run_id, completed[task_id])
        all_tasks_passed = all(item.state == TaskState.COMPLETED for item in completed.values())
        try:
            manifests = store.manifests(request.run_id, completed)
            artifacts_intact = all(self.runtime.artifacts.manifest(path) == manifest
                                   for entries in manifests.values() for path, manifest in entries.items())
        except (OSError, ValueError):
            artifacts_intact = False
        final = tuple(all_tasks_passed and artifacts_intact and time.monotonic() < deadline
                      and self.runtime.verifier.artifact_passes(check) for check in request.final_checks)
        detail = None
        if not all_tasks_passed:
            detail = "One or more required tasks did not pass verification"
        elif not artifacts_intact:
            detail = "An accepted artifact changed before final verification"
        elif time.monotonic() >= deadline:
            detail = "Workflow deadline expired"
        elif not all(final):
            detail = "One or more final checks failed"
        return WorkflowResult(
            run_id=request.run_id, tasks=tuple(completed[key] for key in graph.order), final_checks=final,
            completed=detail is None, wall_time_ms=int((time.monotonic() - started) * 1000), detail=detail,
        )

    async def _run_task(self, task, deadline, tool_grant, manifests, store):
        lease_id = None
        context = current_task.set(task.task_id)
        try:
            remaining_tokens = max(0, task.max_model_tokens - store.task_token_charge(task.run_id, task.task_id))
            async with self.runtime.resources.acquire(task, seconds=max(0, deadline - time.monotonic()),
                                                      remaining_tokens=remaining_tokens):
                lease_id = store.open_resources(task, browser=task.agent.value == "browser")
                for item in task.inputs:
                    message = ArtifactHandoff(
                        run_id=task.run_id, producer_task_id=item.producer_task_id, consumer_task_id=task.task_id,
                        input_name=item.name, artifact=manifests[item.producer_task_id][item.path],
                    )
                    store.event(task.run_id, task.task_id, "artifact_handoff", message.model_dump(mode="json"))
                store.transition(task.run_id, task.task_id, TaskState.READY)
                execution = await self._attempt(task, deadline, tool_grant, store)
                store.finish_task(task.run_id, execution)
                return execution
        except ResourceUnavailable as exc:
            execution = TaskExecution(task_id=task.task_id, state=TaskState.BLOCKED, detail=str(exc)[:2000],
                                      failure_code="RESOURCE_UNAVAILABLE")
            store.finish_task(task.run_id, execution)
            return execution
        finally:
            current_task.reset(context)
            if lease_id:
                store.close_resources(task, lease_id)

    async def _attempt(self, task, deadline, tool_grant, store):
        attempts = store.attempt_results(task.run_id, task.task_id)
        verification = None
        state = TaskState.READY
        detail = None
        previous = store.attempt_count(task.run_id, task.task_id)
        if previous >= task.max_attempts:
            return TaskExecution(task_id=task.task_id, state=TaskState.BLOCKED, detail="Attempt allowance is exhausted")
        for attempt in range(previous + 1, task.max_attempts + 1):
            remaining = min(task.max_wall_time_seconds, deadline - time.monotonic())
            if remaining < 1:
                state = TaskState.CANCELLED
                detail = "Workflow deadline expired"
                break
            state = TaskState.RUNNING
            attempt_deadline = time.monotonic() + remaining
            store.begin_attempt(task, attempt)
            try:
                async with asyncio.timeout(remaining):
                    result = await self.runtime.execute(task, attempt, seconds=int(remaining), tool_grant=tool_grant)
            except TimeoutError:
                store.end_attempt(task, attempt, TaskState.OUTCOME_UNKNOWN)
                state, detail = TaskState.OUTCOME_UNKNOWN, "Attempt timed out; inspect before retrying"
                break
            except asyncio.CancelledError:
                store.end_attempt(task, attempt, TaskState.OUTCOME_UNKNOWN)
                store.finish_task(task.run_id, TaskExecution(
                    task_id=task.task_id, state=TaskState.OUTCOME_UNKNOWN, attempts=tuple(attempts),
                    detail="Cancelled during a dispatched attempt",
                ))
                raise
            except Exception as exc:  # noqa: BLE001 - An interrupted adapter can have unknown external effects.
                store.end_attempt(task, attempt, TaskState.OUTCOME_UNKNOWN)
                state, detail = TaskState.OUTCOME_UNKNOWN, f"Attempt interrupted: {type(exc).__name__}"
                break
            attempts.append(result)
            store.end_attempt(task, attempt, result.outcome, result)
            if not result.matches(task, attempt=attempt) or result.usage.tool_steps > task.max_steps:
                state, detail = TaskState.FAILED, "Worker result exceeded or did not match the task grant"
                break
            if result.outcome == AttemptOutcome.FAILED and result.usage.tool_steps == 0 and attempt < task.max_attempts:
                state = TaskState.READY
                store.transition(task.run_id, task.task_id, state)
                continue
            if result.outcome != AttemptOutcome.REPORTED_SUCCESS:
                state = TaskState(result.outcome.value)
                break
            state = TaskState.VERIFYING
            store.transition(task.run_id, task.task_id, state)
            try:
                async with asyncio.timeout(max(0, min(deadline, attempt_deadline) - time.monotonic())):
                    verification = await self.runtime.verifier.check(task, attempt)
                manifests_match = all(self.runtime.artifacts.manifest(item.path) == item for item in result.artifacts)
                if verification.accepts(task, attempt=attempt) and manifests_match:
                    state = TaskState.COMPLETED
                else:
                    state, detail = TaskState.FAILED, "Independent verification rejected the task result"
            except (TimeoutError, OSError, ValueError):
                state, detail = TaskState.FAILED, "Independent verification could not confirm the task result"
            except asyncio.CancelledError:
                store.finish_task(task.run_id, TaskExecution(
                    task_id=task.task_id, state=TaskState.OUTCOME_UNKNOWN, attempts=tuple(attempts),
                    detail="Cancelled during independent verification",
                ))
                raise
            break
        code = {
            TaskState.OUTCOME_UNKNOWN: "OUTCOME_UNKNOWN", TaskState.CANCELLED: "DEADLINE",
            TaskState.BLOCKED: "REVIEW_REQUIRED", TaskState.FAILED: "TASK_FAILED",
        }.get(state)
        if state == TaskState.FAILED:
            code = "VERIFICATION_FAILED" if verification is not None else (attempts[-1].error_code or code if attempts else code)
        return TaskExecution(task_id=task.task_id, state=state, attempts=tuple(attempts),
                             verification=verification, detail=detail, failure_code=code)
