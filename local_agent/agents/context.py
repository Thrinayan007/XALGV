"""The model receives a bounded manifest projection, never another attempt's history."""

import json
from contextvars import ContextVar

from .base import AgentTask

current_task: ContextVar[str | None] = ContextVar("workflow_task", default=None)


def specialist_context(task: AgentTask, context_tokens: int) -> str:
    payload = json.dumps({
        "inputs": [{"name": item.name, "path": item.path, "producer": item.producer_task_id} for item in task.inputs],
        "outputs": task.expected_outputs,
    }, separators=(",", ":"))
    criteria = json.dumps([item.model_dump(mode="json") for item in task.acceptance_checks])
    if len(payload) + len(criteria) + len(task.objective) > max(0, context_tokens - 4500) * 2:
        raise ValueError("Task handoff and checks exceed the specialist context budget")
    return payload
