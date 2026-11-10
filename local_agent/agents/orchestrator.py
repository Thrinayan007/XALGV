"""Admit a bounded DAG, then dispatch one independently verified specialist at a time."""

import asyncio
import json
import time

import httpx
from pydantic import TypeAdapter

from .base import Identifier, TaskState
from .context import current_task
from .inference_scheduler import InferenceLimitError, token_budget
from .recovery import reconcile, restore_tasks, workflow_owner
from .scheduler import Scheduler, WorkflowResult
from .task_graph import Plan, TaskGraph, WorkflowRequest
from .workflow_store import WorkflowStore

PLAN_PROMPT = """Plan the workflow using every trusted task exactly once. Each task is already assigned
to a specialist and has fixed outputs, checks and budgets. Propose a short approach
and add dependencies only where they are necessary. Preserve the trusted requirements.
Return only JSON: {"tasks":[{"task_id":"id","approach":"short approach","dependencies":[]}]}
"""


class Orchestrator:
    def __init__(self, model, runtime):
        if model is not runtime.model:
            raise ValueError("The orchestrator and specialists must share one model provider")
        self.runtime = runtime
        self.scheduler = Scheduler(runtime)

    def _graph(self, request, plan=None):
        return TaskGraph(request, self.runtime.registry, self.runtime.effective_tools, plan)

    async def run(self, goal: str, request: WorkflowRequest, *, plan: Plan | None = None):
        """An explicit plan is a trusted-caller bypass for deterministic fixtures."""
        request = WorkflowRequest.model_validate(request)
        async with workflow_owner(self.runtime.config, request.run_id):
            started = time.monotonic()
            deadline = time.time() + request.max_wall_time_seconds
            self._graph(request)
            for item in request.tasks:
                self.runtime.validate_task(item.task)
            supplied_plan = plan is not None
            with token_budget(request.max_model_tokens) as planning_budget:
                if not supplied_plan:
                    plan = await self._plan(goal, request, started)
            plan = Plan.model_validate(plan)
            graph = self._graph(request, plan)
            self.runtime.artifacts.claim(request.run_id)
            store = WorkflowStore(self.runtime.config.data_dir / "agent-runs" / "workflows.sqlite3")
            used = request.max_model_tokens - planning_budget.remaining
            store.start(goal, graph, deadline=deadline, token_charge=used)

            async def revise(original, completed):
                fixed = tuple(task for task in plan.tasks if task.task_id in completed)
                try:
                    revised = await self._plan(goal, request, started, fixed=fixed, completed=completed)
                    return self._graph(request, revised)
                except (ValueError, TimeoutError, httpx.HTTPError, InferenceLimitError):
                    return original

            return await self._execute(graph, started, store, used, replan=None if supplied_plan else revise)

    async def resume(self, run_id: str, *, confirm: tuple[str, ...] = ()):
        run_id = TypeAdapter(Identifier).validate_python(run_id)
        async with workflow_owner(self.runtime.config, run_id):
            store = WorkflowStore(self.runtime.config.data_dir / "agent-runs" / "workflows.sqlite3")
            journal = store.inspect(run_id)
            row = journal["run"]
            if row["status"] == "completed":
                if confirm:
                    raise ValueError("Completed runs have no outcomes to reconcile")
                manifests = store.manifests(run_id, [item["task_id"] for item in journal["agent_tasks"]])
                if any(self.runtime.artifacts.manifest(item.path) != item
                       for outputs in manifests.values() for item in outputs.values()):
                    raise ValueError("A verified output changed after completion")
                return WorkflowResult.model_validate_json(row["result"])
            deadline = row["deadline"]
            if deadline is None or deadline <= time.time():
                raise ValueError("The original workflow deadline is unavailable or expired")
            request = WorkflowRequest.model_validate_json(row["contract"])
            specs, _ = restore_tasks(journal)
            graph = TaskGraph(request, self.runtime.registry, self.runtime.effective_tools, checkpoint=specs)
            for task in specs.values():
                self.runtime.validate_task(task)
            started = time.monotonic() - max(0, request.max_wall_time_seconds - (deadline - time.time()))
            eligible = {item["task_id"] for item in journal["agent_tasks"]
                        if item["status"] in {"running", "verifying", "outcome_unknown"}}
            if not set(confirm) <= eligible or len(set(confirm)) != len(confirm):
                raise ValueError("Only distinct tasks with an uncertain outcome can be reconciled")
            store.resume(run_id)
            try:
                _, restored = restore_tasks(store.inspect(run_id))
                for task_id in graph.order:
                    if task_id in confirm:
                        task = graph.task(task_id)
                        if any(parent not in restored or restored[parent].state != TaskState.COMPLETED
                               for parent in task.dependencies):
                            raise ValueError(f"Prerequisites are incomplete for {task_id}")
                        manifests = store.manifests(run_id, task.dependencies)
                        if any(self.runtime.artifacts.manifest(item.path) != item
                               for outputs in manifests.values() for item in outputs.values()):
                            raise ValueError(f"Verified prerequisite evidence changed for {task_id}")
                        task = graph.bind(task_id, manifests)
                        restored[task_id] = await reconcile(self.runtime, store, task, deadline)
            except BaseException:
                store.interrupt(run_id)
                raise
            return await self._execute(graph, started, store, row["token_charge"], restored=restored)

    async def _execute(self, graph, started, store, used, **kwargs):
        request = graph.request
        with token_budget(max(0, request.max_model_tokens - used),
                          lambda delta: store.account(request.run_id, delta, current_task.get())) as budget:
            initial = budget.remaining
            try:
                result = await self.scheduler.run(graph, started=started, store=store, **kwargs)
                result = result.model_copy(update={"model_tokens_used": used + initial - budget.remaining})
                store.finish(result)
                return result
            except BaseException:
                store.interrupt(request.run_id)
                raise

    async def _plan(self, goal, request, started, *, fixed=(), completed=None):
        completed = completed or {}
        tasks = [{
            "task_id": item.task.task_id,
            "agent": item.task.agent.value,
            "objective": item.task.objective[:200],
            "required_dependencies": list(dict.fromkeys(
                (*item.task.dependencies, *(binding.producer_task_id for binding in item.bindings)),
            )),
        } for item in request.tasks if item.task.task_id not in completed]
        content = json.dumps({
            "goal": goal[:2000], "trusted_tasks": tasks,
            "recorded_outcomes": {key: result.state.value for key, result in completed.items()},
        })
        context_limit = self.runtime.config.model_roles["planner"].context_length
        if len(content) > max(0, context_limit - 2600) * 2:
            raise ValueError("Workflow description exceeds the planner context budget")
        messages = [
            {"role": "system", "content": PLAN_PROMPT},
            {"role": "user", "content": content},
        ]
        attempts = 1 if completed else 2
        for attempt in range(attempts):
            remaining = request.max_wall_time_seconds - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError("Workflow deadline expired during planning")
            if completed:
                remaining = min(remaining, 45)
            response = await asyncio.wait_for(self.runtime.chat("planner", messages, max_tokens=2048), timeout=remaining)
            raw = response.content.strip()
            if raw.startswith("```") and "\n" in raw:
                raw = raw.split("\n", 1)[1].removesuffix("```").strip()
            try:
                plan = Plan.model_validate_json(raw)
                if any(item.task_id in completed for item in plan.tasks):
                    raise ValueError("Replanning cannot change recorded tasks")
                if fixed:
                    plan = Plan(tasks=(*fixed, *plan.tasks))
                self._graph(request, plan)
                return plan
            except ValueError as exc:
                if attempt + 1 == attempts:
                    raise ValueError("Model could not produce a valid bounded workflow plan") from exc
                messages.append({"role": "user", "content": (
                    "The plan was rejected. Return valid JSON with every trusted task exactly once, "
                    "known dependencies, and no cycles. Do not change checks or permissions."
                )})
