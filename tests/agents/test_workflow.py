import asyncio
import json
import os
from dataclasses import dataclass

import pytest

from local_agent.agents.artifacts import Artifacts, TaskFiles
from local_agent.agents.base import AgentResult, AgentRole, AgentTask, AttemptOutcome, TaskState, Usage
from local_agent.agents.definitions import default_registry
from local_agent.agents.orchestrator import Orchestrator
from local_agent.agents.runtime import SpecialistRuntime
from local_agent.agents.task_graph import Plan, TaskGraph, WorkflowRequest
from local_agent.agents.tools import ScopedBrowserTool
from local_agent.agents.workflow_store import WorkflowStore
from local_agent.config import AgentConfig
from local_agent.models import ChatResponse, ToolCall
from local_agent.tools.browser import BrowserRunTool


@dataclass
class ScriptedModel:
    replies: list

    async def chat(self, role, messages, *, tools=None, **kwargs):
        reply, expected_tools = self.replies.pop(0)
        if expected_tools is not None:
            assert {tool["function"]["name"] for tool in tools} == expected_tools
        if isinstance(reply, tuple):
            calls = [ToolCall("tool", *reply)]
            content = ""
        else:
            calls, content = [], reply
        return ChatResponse(content, calls, "fixture", role, True)


def requirement(task_id, *, depends=(), tools=("write_file",), attempts=1):
    path = f"artifacts/run1/{task_id}/report.txt"
    return {"task": {
        "run_id": "run1", "task_id": task_id, "agent": "coding", "objective": f"Write {task_id}",
        "dependencies": depends, "expected_outputs": [path],
        "max_steps": 6, "max_wall_time_seconds": 20, "max_attempts": attempts,
        "acceptance_checks": [{"kind": "artifact", "path": path, "contains": "approved"}],
    }, "required_tools": tools}


def request(*requirements, steps=32):
    return WorkflowRequest.model_validate_json(json.dumps({
        "run_id": "run1", "tasks": requirements, "max_steps": steps, "max_wall_time_seconds": 100,
    }))


def runtime(tmp_path, model=None, **kwargs):
    config = AgentConfig(workspace_dir=tmp_path / "workspace", data_dir=tmp_path / "data")
    return SpecialistRuntime(config, model, None, default_registry(), **kwargs)


@pytest.mark.parametrize(("tasks", "steps", "message"), [
    ([requirement("a", depends=("b",)), requirement("b", depends=("a",))], 32, "cycle"),
    ([requirement("a", depends=("missing",))], 32, "Unknown dependency"),
    ([requirement("a"), requirement("a")], 32, "Duplicate task"),
    ([requirement("a", tools=("shell_exec",))], 32, "Unavailable capabilities"),
    ([requirement("a", attempts=2)], 10, "workflow budget"),
])
def test_graph_rejects_invalid_contracts(tmp_path, tasks, steps, message):
    adapter = runtime(tmp_path)
    with pytest.raises(ValueError, match=message):
        TaskGraph(request(*tasks, steps=steps), adapter.registry, adapter.effective_tools)


async def test_coding_adapter_hides_and_denies_unauthorized_tools(tmp_path):
    names = {"read_file", "write_file", "validate_python_schema"}
    target = "artifacts/run1/a/report.txt"
    model = ScriptedModel([
        (("shell_exec", {"command": "should never execute"}), names),
        (("write_file", {"path": "outside.txt", "content": "unauthorized"}), names),
        (("write_file", {"path": target, "content": "approved"}), names),
        ("DONE: report written", names),
    ])
    adapter = runtime(tmp_path, model)
    assert adapter.effective_tools(AgentRole.CODING) == names
    task = request(requirement("a")).tasks[0].task
    adapter.artifacts.claim("run1")
    result = await adapter.execute(task, 1)
    assert result.outcome == AttemptOutcome.REPORTED_SUCCESS
    assert not (adapter.artifacts.workspace / "outside.txt").exists()
    assert (adapter.config.data_dir / "agent-runs/run1/a/1/memory.db").is_file()
    assert (await adapter.verifier.check(task, 1)).accepts(task, attempt=1)


async def test_orchestrator_rejects_cycle_then_blocks_unverified_dependency(tmp_path):
    first = "artifacts/run1/a/report.txt"
    names = {"read_file", "write_file", "validate_python_schema"}
    model = ScriptedModel([
        (json.dumps({"tasks": [{"task_id": "a", "approach": "a", "dependencies": ["b"]},
                               {"task_id": "b", "approach": "b"}]}), None),
        (json.dumps({"tasks": [{"task_id": "a", "approach": "a"}, {"task_id": "b", "approach": "b"}]}), None),
        (("write_file", {"path": first, "content": "incorrect"}), names),
        ("DONE: report complete", names),
        ("DONE: report complete", names),
        ("DONE: report complete", names),
    ])
    adapter = runtime(tmp_path, model)
    spec = request(requirement("a"), requirement("b", depends=("a",)))
    result = await Orchestrator(model, adapter).run("Produce two reports", spec)
    assert [item.state for item in result.tasks] == [TaskState.FAILED, TaskState.BLOCKED]
    assert not result.completed
    assert not model.replies


async def test_handoff_is_read_only_and_digest_bound(tmp_path):
    store = Artifacts(tmp_path)
    store.claim("run1")
    producer = request(requirement("a")).tasks[0].task
    path = producer.expected_outputs[0]
    assert (await TaskFiles(store, producer).write_file(path, "approved")).success
    manifest = store.manifest(path)
    consumer = request(requirement("b", depends=("a",))).tasks[0].task
    consumer = AgentTask.model_validate_json(json.dumps({
        **consumer.model_dump(mode="json"), "inputs": [{
            "name": "input", "producer_task_id": "a", "path": path, "sha256": manifest.sha256,
        }],
    }))
    files = TaskFiles(store, consumer)
    assert (await files.read_file(path)).output == "approved"
    assert not (await files.write_file(path, "overwritten")).success
    assert (await TaskFiles(store, producer).write_file(path, "changed")).success
    assert not (await files.read_file(path)).success


async def test_browser_macro_cannot_escape_narrowed_capabilities(tmp_path):
    class Sandbox:
        async def browser_run(self, **kwargs):
            pytest.fail("A disallowed macro must be rejected before reaching the sandbox")

    task = request(requirement("a")).tasks[0].task
    scoped = ScopedBrowserTool(BrowserRunTool(Sandbox()), task, Artifacts(tmp_path), {"browser_run", "browser_open", "browser_screenshot"})
    denied = await scoped.execute({"steps": [
        {"tool": "open", "args": {"url": "https://example.test/"}},
        {"tool": "click", "args": {"selector": "#hidden"}},
    ]})
    assert not denied.success and "capability" in denied.error
    denied = await scoped.execute({"steps": [
        {"tool": "screenshot", "args": {"path": "artifacts/other/task/photo.png"}},
    ]})
    assert not denied.success and "declared output" in denied.error


async def test_replan_preserves_completed_tasks_and_failed_attempts(tmp_path):
    names = {"read_file", "write_file", "validate_python_schema"}
    model = ScriptedModel([
        (json.dumps({"tasks": [{"task_id": task, "approach": "write"} for task in ("a", "b", "c")]}), None),
        (("write_file", {"path": "artifacts/run1/a/report.txt", "content": "approved"}), names),
        ("DONE: a", names),
        ("DONE: b", names), ("DONE: b", names), ("DONE: b", names),
        (json.dumps({"tasks": [{"task_id": "c", "approach": "write c", "dependencies": ["a"]}]}), None),
        (("write_file", {"path": "artifacts/run1/c/report.txt", "content": "approved"}), names),
        ("DONE: c", names),
    ])
    adapter = runtime(tmp_path, model)
    result = await Orchestrator(model, adapter).run("Write the reports", request(
        requirement("a"), requirement("b"), requirement("c"),
    ))
    assert [item.state for item in result.tasks] == [TaskState.COMPLETED, TaskState.FAILED, TaskState.COMPLETED]
    assert len(result.tasks[0].attempts) == 1
    assert len(result.tasks[1].attempts) == 1
    assert not result.completed and not model.replies


async def test_independent_verification_rejects_worker_success_claim(tmp_path, monkeypatch):
    adapter = runtime(tmp_path)

    async def claim(task, attempt, **kwargs):
        path = task.expected_outputs[0]
        assert (await TaskFiles(adapter.artifacts, task).write_file(path, "wrong content")).success
        return AgentResult(
            run_id=task.run_id, task_id=task.task_id, attempt=attempt, agent=task.agent,
            outcome=AttemptOutcome.REPORTED_SUCCESS, summary="The worker claims this is finished",
            artifacts=(adapter.artifacts.manifest(path),),
            usage=Usage(tool_steps=1, model_calls=1, prompt_tokens=0, generated_tokens=0, wall_time_ms=1),
        )

    monkeypatch.setattr(adapter, "execute", claim)
    plan = Plan.model_validate_json('{"tasks":[{"task_id":"a","approach":"write"},{"task_id":"b","approach":"write"}]}')
    result = await Orchestrator(None, adapter).run(
        "Write two verified reports", request(requirement("a"), requirement("b", depends=("a",))), plan=plan,
    )
    assert result.tasks[0].attempts[0].outcome == AttemptOutcome.REPORTED_SUCCESS
    assert result.tasks[0].verification.passed is False
    assert result.tasks[0].state == TaskState.FAILED
    assert result.tasks[1].state == TaskState.BLOCKED


async def test_declared_output_cannot_alias_an_outside_file(tmp_path):
    store = Artifacts(tmp_path / "workspace")
    store.claim("run1")
    task = request(requirement("a")).tasks[0].task
    outside = tmp_path / "outside.txt"
    outside.write_text("private", encoding="utf-8")
    destination = store.workspace / task.expected_outputs[0]
    destination.parent.mkdir()
    os.link(outside, destination)
    files = TaskFiles(store, task)
    assert not (await files.read_file(task.expected_outputs[0])).success
    assert not (await files.write_file(task.expected_outputs[0], "modified")).success
    assert outside.read_text(encoding="utf-8") == "private"


async def test_cancellation_journals_unknown_attempt_and_closes_resources(tmp_path, monkeypatch):
    adapter = runtime(tmp_path)
    started = asyncio.Event()

    async def interrupted(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(adapter, "execute", interrupted)
    plan = Plan.model_validate_json('{"tasks":[{"task_id":"a","approach":"write"}]}')
    work = asyncio.create_task(Orchestrator(None, adapter).run("Write report", request(requirement("a")), plan=plan))
    await asyncio.wait_for(started.wait(), 5)
    work.cancel()
    with pytest.raises(asyncio.CancelledError):
        await work
    store = WorkflowStore(adapter.config.data_dir / "agent-runs/workflows.sqlite3")
    journal = store.inspect("run1")
    assert journal["run"]["status"] == "interrupted"
    assert journal["agent_tasks"][0]["status"] == "outcome_unknown"
    assert journal["task_attempts"][0]["status"] == "outcome_unknown"
    assert journal["resource_leases"][0]["closed_at"]
    assert journal["task_artifacts"] == []


async def test_resume_reconciles_abandoned_effect_without_dispatching_it_again(tmp_path):
    names = {"read_file", "write_file", "validate_python_schema"}
    model = ScriptedModel([
        (("write_file", {"path": "artifacts/run1/b/report.txt", "content": "approved"}), names),
        ("DONE: downstream finished", names),
    ])
    adapter = runtime(tmp_path, model)
    spec = request(requirement("a"), requirement("b", depends=("a",)))
    graph = TaskGraph(spec, adapter.registry, adapter.effective_tools)
    adapter.artifacts.claim("run1")
    store = WorkflowStore(adapter.config.data_dir / "agent-runs/workflows.sqlite3")
    store.start("Write both reports", graph, token_charge=200)
    task = graph.task("a")
    store.transition("run1", "a", TaskState.READY)
    store.begin_attempt(task, 1)
    store.account("run1", 100, "a")
    coordinator = Orchestrator(model, adapter)
    recovered = await coordinator.resume("run1")
    assert [item.state for item in recovered.tasks] == [TaskState.OUTCOME_UNKNOWN, TaskState.BLOCKED]
    assert len(model.replies) == 2
    with pytest.raises(ValueError, match="Original checks"):
        await coordinator.resume("run1", confirm=("a",))
    assert (await TaskFiles(adapter.artifacts, task).write_file(task.expected_outputs[0], "approved")).success
    accepted = await coordinator.resume("run1", confirm=("a",))
    assert accepted.completed and accepted.model_tokens_used == 300
    assert not model.replies
    journal = store.inspect("run1")
    assert [(row["task_id"], row["attempt"]) for row in journal["task_attempts"]] == [("a", 1), ("b", 1)]
    assert journal["task_attempts"][0]["status"] == "outcome_unknown"
    assert any(row["kind"] == "operator_reconciled" for row in journal["agent_events"])
    assert not (adapter.config.data_dir / "agent-runs/run1/a").exists()
