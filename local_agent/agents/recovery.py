"""Conservative recovery of a durable workflow checkpoint."""

import asyncio
import time
from contextlib import AsyncExitStack, asynccontextmanager

from .base import AgentResult, AgentTask, AttemptOutcome, TaskState, Usage
from .inference_scheduler import resource_lock
from .scheduler import TaskExecution


@asynccontextmanager
async def workflow_owner(config, run_id):
    async with AsyncExitStack() as stack:
        try:
            async with asyncio.timeout(2):
                await stack.enter_async_context(resource_lock(config.data_dir / "resource-locks", f"workflow:{run_id}"))
        except TimeoutError as exc:
            raise ValueError("Another coordinator owns this run") from exc
        yield


def restore_tasks(journal):
    specs = {}
    outcomes = {}
    for row in journal["agent_tasks"]:
        task_id = row["task_id"]
        specs[task_id] = AgentTask.model_validate_json(row["spec"])
        state = TaskState(row["status"])
        if state in {TaskState.PENDING, TaskState.READY}:
            continue
        execution = TaskExecution.model_validate_json(row["result"]) if row["result"] else None
        if execution is None or execution.state != state:
            attempts = tuple(
                AgentResult.model_validate_json(attempt["result"]) for attempt in journal["task_attempts"]
                if attempt["task_id"] == task_id and attempt["result"]
            )
            execution = TaskExecution(task_id=task_id, state=state, attempts=attempts,
                                      detail="Previous attempt was interrupted; review its outcome")
        if state == TaskState.BLOCKED and (execution.detail == "A prerequisite did not pass verification"
                                           or execution.failure_code in {"RESOURCE_UNAVAILABLE", "BROWSER_OUTCOME_PENDING"}):
            continue
        outcomes[task_id] = execution
    return specs, outcomes


async def reconcile(runtime, store, task, deadline):
    """Recheck operator-confirmed evidence. Never dispatch the worker."""
    attempt = store.attempt_count(task.run_id, task.task_id)
    if attempt < 1:
        raise ValueError(f"Task {task.task_id} has no attempt to reconcile")
    seconds = deadline - time.time()
    if seconds <= 0:
        raise ValueError("Original workflow deadline expired")
    async with asyncio.timeout(seconds), runtime.resources.acquire(task, seconds=seconds):
        lease_id = store.open_resources(task, browser=task.agent.value == "browser")
        try:
            report = await runtime.verifier.check(task, attempt)
            if not report.accepts(task, attempt=attempt):
                store.event(task.run_id, task.task_id, "reconciliation_failed", {"attempt": attempt})
                raise ValueError(f"Original checks did not pass for {task.task_id}")
            manifests = tuple(runtime.artifacts.manifest(path) for path in task.expected_outputs)
            recorded = store.attempt_results(task.run_id, task.task_id)
            prior = next((item for item in recorded if item.attempt == attempt), None)
            evidence = AgentResult(
                run_id=task.run_id, task_id=task.task_id, agent=task.agent, attempt=attempt,
                outcome=AttemptOutcome.REPORTED_SUCCESS, summary="Operator reviewed the outcome and original checks passed",
                artifacts=manifests, usage=prior.usage if prior else Usage(
                    tool_steps=0, model_calls=0, prompt_tokens=0, generated_tokens=0, wall_time_ms=0,
                ),
            )
            previous = tuple(item for item in recorded if item.attempt != attempt)
            execution = TaskExecution(task_id=task.task_id, state=TaskState.COMPLETED, attempts=(*previous, evidence),
                                      verification=report, detail="Reconciled without replaying the attempt")
            store.finish_task(task.run_id, execution, reconciled=True)
            return execution
        finally:
            store.close_resources(task, lease_id)
