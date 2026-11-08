import asyncio
import json
import sqlite3
from copy import deepcopy

import pytest

from local_agent.config import AgentConfig
from local_agent.core.browser_progress import signature
from local_agent.core.loop import AgentLoop
from local_agent.memory.store import _SCHEMA, MemoryStore, RunStatus, StepRecord
from local_agent.memory.working import WorkingMemory
from local_agent.models import ChatResponse, ToolCall
from local_agent.security.permissions import PermissionPolicy, PermissionTier
from local_agent.tools.base import ToolResult, VerificationResult, VerificationStatus
from local_agent.tools.browser import BrowserRunTool, BrowserScrollTool, browser_tools
from local_agent.tools.registry import ToolRegistry


def response(text="", tool=None, args=None):
    return ChatResponse(text, [ToolCall("call", tool, args or {})] if tool else [], "test", "assistant", True)


class Model:
    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts = []

    async def chat(self, role, messages, **kwargs):
        if role == "planner":
            return response(json.dumps({"plan": ["Capture all evidence", "Create report"], "reasoning": "test"}))
        self.prompts.append(deepcopy(messages))
        return self.responses.pop(0) if self.responses else response("DONE: complete")


class Sandbox:
    def __init__(self):
        self.calls = []
        self.moving = True
        self.entered = asyncio.Event()
        self.block = False

    async def browser_scroll(self, **kwargs):
        self.calls.append(kwargs)
        return ToolResult(success=True, output="scroll evidence", metadata={
            "session_id": "session", "generation": 1, "tab_id": "t1", "state_after": str(len(self.calls)),
            "action_verification": {"moved": self.moving},
        })

    async def browser_run(self, **kwargs):
        self.calls.append(kwargs)
        self.entered.set()
        if self.block:
            await asyncio.Event().wait()
        return ToolResult(success=True, output="macro evidence", metadata={
            "data": {"attempted_actions": len(kwargs["steps"])},
        })


def make_loop(tmp_path, model, sandbox, **config):
    cfg = AgentConfig(db_path=tmp_path / "runs.db", trace_dir=tmp_path / "traces", **config)
    registry = ToolRegistry(PermissionPolicy(auto_approve_up_to=PermissionTier.NETWORK))
    registry.register_many(BrowserScrollTool(sandbox), BrowserRunTool(sandbox))
    return AgentLoop(cfg, model, registry, MemoryStore(cfg.db_path))


async def test_success_does_not_advance_multi_action_plan(tmp_path):
    model = Model([response(tool="browser_scroll"), response(tool="browser_scroll"),
                   response("STEP_DONE: both captures finished"), response(tool="browser_scroll"), response("DONE: report complete")])
    run = await make_loop(tmp_path, model, Sandbox()).run("Capture evidence and create report")
    assert run.status == RunStatus.COMPLETED
    assert "Current task: Capture all evidence" in model.prompts[1][0]["content"]
    assert "Current task: Capture all evidence" in model.prompts[2][0]["content"]
    assert "Current task: Create report" in model.prompts[3][0]["content"]
    assert run.steps[1].result_metadata["action_verification"]["moved"]


async def test_macro_cannot_exceed_remaining_action_allowance(tmp_path):
    sandbox = Sandbox()
    model = Model([response(tool="browser_run", args={"steps": [{"tool": "observe", "args": {}}] * 3})])
    run = await make_loop(tmp_path, model, sandbox, max_steps=2).run("goal")
    assert run.status == RunStatus.FAILED and "allowance" in run.summary
    assert sandbox.calls == []


async def test_macro_counts_subactions_and_clamps_deadline(tmp_path):
    sandbox = Sandbox()
    steps = [{"tool": "observe", "args": {}}] * 3
    model = Model([response(tool="browser_run", args={"steps": steps, "timeout_ms": 90000}),
                   response(tool="browser_scroll")])
    run = await make_loop(tmp_path, model, sandbox, max_steps=3, max_wall_time_seconds=10).run("goal")
    assert run.status == RunStatus.FAILED and "Step limit" in run.summary
    assert len(sandbox.calls) == 1
    assert 0 < sandbox.calls[0]["timeout_ms"] <= 10000
    assert sandbox.calls[0]["action_budget"] == 3


async def test_changed_scroll_state_allows_repetition_but_no_movement_stops(tmp_path):
    sandbox = Sandbox()
    calls = [response(tool="browser_scroll") for _ in range(5)]
    run = await make_loop(tmp_path, Model(calls), sandbox).run("scroll")
    assert run.status == RunStatus.COMPLETED and len(sandbox.calls) == 5
    sandbox = Sandbox()
    sandbox.moving = False
    run = await make_loop(tmp_path, Model(calls), sandbox).run("scroll")
    assert run.status == RunStatus.FAILED and "no movement" in run.summary
    assert len(sandbox.calls) == 3


async def test_kill_cancels_in_flight_macro(tmp_path):
    sandbox = Sandbox()
    sandbox.block = True
    model = Model([response(tool="browser_run", args={"steps": [{"tool": "observe", "args": {}}]})])
    loop = make_loop(tmp_path, model, sandbox)
    task = asyncio.create_task(loop.run("goal"))
    await asyncio.wait_for(sandbox.entered.wait(), 3)
    loop.kill()
    run = await asyncio.wait_for(task, 3)
    assert run.status == RunStatus.KILLED
    assert "cancelled" in run.steps[-1].error


def test_repeat_signature_uses_full_arguments_and_scope():
    assert signature("click", {"text": "x" * 200 + "a"}) != signature("click", {"text": "x" * 200 + "b"})
    assert signature("click", {"a": 1, "b": 2}) == signature("click", {"b": 2, "a": 1})
    assert signature("click", {}, ["t1"]) != signature("click", {}, ["t2"])


def test_observation_compaction_preserves_errors_and_action_evidence():
    memory = WorkingMemory()
    memory.add_tool_result("browser_observe", "old page content", metadata={"action": "observe", "snapshot_id": "s1"})
    memory.add_tool_result("browser_screenshot", "created evidence.png")
    memory.add_tool_result("browser_observe", "STALE_REF", success=False, metadata={"action": "observe"})
    memory.add_tool_result("browser_observe", "new page content", metadata={"action": "observe", "snapshot_id": "s2"})
    text = str(memory.get_messages())
    assert "old page content" not in text and "s1 elided" in text
    assert "new page content" in text and "STALE_REF" in text and "created evidence.png" in text


def test_existing_database_migration_and_structured_evidence_round_trip(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(_SCHEMA)  # Original schema has no result_metadata column.
    store = MemoryStore(path)
    store.create_run("run", "goal")
    metadata = {"artifacts": ["evidence.png"], "data": {"results": [{"success": True}]}}
    store.record_step("run", StepRecord(step_index=0, action_type="tool_call", result_metadata=metadata, result_output="x" * 9000))
    loaded = MemoryStore(path).get_run("run").steps[0]
    assert loaded.result_metadata == metadata and len(loaded.result_output) == 9000


def test_default_browser_surface_has_nine_core_actions_and_optional_macro():
    assert len(browser_tools(include_macro=False)) == 9
    assert len(browser_tools()) == 10
    assert {tool.name for tool in browser_tools()} == {
        "browser_open", "browser_observe", "browser_click", "browser_type", "browser_press",
        "browser_scroll", "browser_tabs", "browser_wait", "browser_screenshot", "browser_run",
    }


async def test_model_done_cannot_override_failed_trusted_acceptance(tmp_path):
    class Verifier:
        async def verify(self, checks):
            assert checks == [{"kind": "artifact", "path": "required.png", "png": True}]
            return VerificationResult(status=VerificationStatus.FAILED, message="Screenshot missing", evidence={"missing": "required.png"})
    model = Model([response("DONE: all files created")] * 3)
    loop = make_loop(tmp_path, model, Sandbox())
    loop._task_verifier = Verifier()
    run = await loop.run("Create screenshot", acceptance_checks=[{"kind": "artifact", "path": "required.png", "png": True}])
    assert run.status == RunStatus.FAILED
    assert run.metadata["task_verification"]["status"] == "failed"
    assert len([step for step in run.steps if step.action_type == "task_verification"]) == 3


async def test_trusted_acceptance_allows_verified_completion(tmp_path):
    class Verifier:
        async def verify(self, checks):
            return VerificationResult(status=VerificationStatus.PASSED, message="Checked", evidence={"artifacts": 5})
    loop = make_loop(tmp_path, Model([response("DONE: complete")]), Sandbox())
    loop._task_verifier = Verifier()
    run = await loop.run("goal", acceptance_checks=[{"kind": "tab_count", "expected": "1"}])
    assert run.status == RunStatus.COMPLETED and run.metadata["task_verification"]["status"] == "passed"


def test_profile_documents_are_excluded_from_ingestion(tmp_path):
    from local_agent.rag import DocumentParser
    profile = tmp_path / ".xbrowser" / "scope" / "profile" / "cookies.txt"
    profile.parent.mkdir(parents=True)
    profile.write_text("private authentication data")
    with pytest.raises(PermissionError):
        DocumentParser().parse(profile)


def test_terminal_marker_can_follow_reasoning_but_not_a_code_example():
    from local_agent.core.completion import terminal_response
    assert terminal_response("I observed the correct page.\n\nDONE: title found") == "DONE: title found"
    assert terminal_response("Example:\n```\nDONE: this is quoted\n```").startswith("Example:")


async def test_arbitrary_execution_requires_system_permission():
    from local_agent.security.permissions import PermissionDeniedError
    from local_agent.tools.python_exec import PythonExecTool
    from local_agent.tools.shell import ShellExecTool
    registry = ToolRegistry(PermissionPolicy(auto_approve_up_to=PermissionTier.NETWORK))
    registry.register_many(ShellExecTool(), PythonExecTool())
    for name in ("shell_exec", "python_exec"):
        assert registry.get(name).permission_tier == PermissionTier.SYSTEM
        with pytest.raises(PermissionDeniedError):
            await registry.execute(name, {})
