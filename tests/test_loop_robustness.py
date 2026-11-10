"""Regression tests for the 'no tool call -> burn the whole step budget' failure."""

import json
import tempfile
from pathlib import Path
from typing import Any

import httpx
import pytest

from local_agent.config import AgentConfig
from local_agent.core.loop import AgentLoop
from local_agent.memory.store import MemoryStore, RunStatus
from local_agent.models import ChatResponse, ModelManager, ToolCall
from local_agent.security.permissions import PermissionPolicy, PermissionTier
from local_agent.tools.base import Tool, ToolResult
from local_agent.tools.registry import ToolRegistry

PLAN = json.dumps({"plan": ["do the thing"], "reasoning": "x"})


class OkTool(Tool):
    name = "ok_tool"
    description = "Always succeeds."
    permission_tier = PermissionTier.READ_ONLY
    parameters_schema = {"type": "object", "properties": {}}

    async def execute(self, params: dict[str, Any]) -> ToolResult:
        return ToolResult(success=True, output="done", exit_code=0)


class ScriptedModel:
    """Planner returns a plan; executor returns the queued responses in order."""

    def __init__(self, executor_responses):
        self.queue = list(executor_responses)
        self.executor_calls = 0

    async def chat(self, role, messages, **kwargs):
        if role == "planner":
            return ChatResponse(PLAN, [], "m", "assistant", True)
        self.executor_calls += 1
        return self.queue.pop(0) if self.queue else ChatResponse("", [], "m", "assistant", True)


def _loop(model, tmp):
    cfg = AgentConfig(db_path=Path(tmp) / "a.db", trace_dir=Path(tmp) / "t", workspace_dir=Path(tmp) / "w")
    reg = ToolRegistry(PermissionPolicy())
    reg.register(OkTool())
    return AgentLoop(cfg, model, reg, MemoryStore(cfg.db_path)), cfg


@pytest.mark.asyncio
async def test_empty_responses_fail_fast_with_diagnosis():
    with tempfile.TemporaryDirectory() as tmp:
        model = ScriptedModel([])  # executor always returns an empty response
        loop, cfg = _loop(model, tmp)
        run = await loop.run("goal")
        assert run.status == RunStatus.FAILED
        assert model.executor_calls == 3  # not 50
        assert "without a tool call" in run.summary


@pytest.mark.asyncio
async def test_tool_call_then_done_completes():
    with tempfile.TemporaryDirectory() as tmp:
        model = ScriptedModel([
            ChatResponse("", [ToolCall("c0", "ok_tool", {})], "m", "assistant", True),
            ChatResponse("DONE: finished", [], "m", "assistant", True),
        ])
        loop, _ = _loop(model, tmp)
        run = await loop.run("goal")
        assert run.status == RunStatus.COMPLETED


@pytest.mark.asyncio
async def test_nudge_counter_resets_after_tool_call():
    with tempfile.TemporaryDirectory() as tmp:
        empty = ChatResponse("", [], "m", "assistant", True)
        model = ScriptedModel([
            empty, empty,
            ChatResponse("", [ToolCall("c0", "ok_tool", {})], "m", "assistant", True),
            ChatResponse("DONE: ok", [], "m", "assistant", True),
        ])
        loop, _ = _loop(model, tmp)
        run = await loop.run("goal")
        assert run.status == RunStatus.COMPLETED


def _manager(handler):
    cfg = AgentConfig()
    mgr = ModelManager(cfg)
    mgr._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://test")
    return mgr


@pytest.mark.asyncio
async def test_think_option_sent_and_retried_without_on_400():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/generate":
            return httpx.Response(200, json={})
        body = json.loads(request.content)
        seen.append("think" in body)
        if "think" in body:
            return httpx.Response(400, json={"error": "model does not support thinking"})
        return httpx.Response(200, json={
            "message": {"role": "assistant", "content": "hi"}, "model": "m", "done": True,
            "done_reason": "stop", "prompt_eval_count": 10, "eval_count": 2,
        })

    mgr = _manager(handler)
    resp = await mgr.chat("executor", [{"role": "user", "content": "x"}])
    assert resp.content == "hi" and resp.done_reason == "stop"
    assert seen == [True, False]
    await mgr.chat("executor", [{"role": "user", "content": "x"}])  # remembered: no think sent
    assert seen == [True, False, False]


# @pytest.mark.asyncio
# async def test_context_overflow_warning(caplog):
#     def handler(request: httpx.Request) -> httpx.Response:
#         if request.url.path == "/api/generate":
#             return httpx.Response(200, json={})
#         return httpx.Response(200, json={
#             "message": {"role": "assistant", "content": ""}, "model": "m", "done": True,
#             "done_reason": "length", "prompt_eval_count": 4090, "eval_count": 0,
#         })

#     mgr = _manager(handler)
#     with caplog.at_level("WARNING"):
#         await mgr.chat("executor", [{"role": "user", "content": "x"}])
#     assert "truncates input" in caplog.text
#     assert "token/context limit" in caplog.text
@pytest.mark.asyncio
async def test_context_overflow_warning(caplog):
    cfg = AgentConfig()
    prompt_eval_count = int(
        cfg.model_roles["executor"].context_length * 0.95
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/generate":
            return httpx.Response(200, json={})

        return httpx.Response(200, json={
            "message": {"role": "assistant", "content": ""},
            "model": "m",
            "done": True,
            "done_reason": "length",
            "prompt_eval_count": prompt_eval_count,
            "eval_count": 0,
        })

    # mgr = ModelManager(cfg)
    mgr = _manager(handler)

    with caplog.at_level("WARNING"):
        await mgr.chat("executor", [{"role": "user", "content": "x"}])

    assert "truncates input" in caplog.text
    assert "token/context limit" in caplog.text

@pytest.mark.asyncio
async def test_write_file_append_flag_reaches_sandbox():
    from local_agent.tools.file_ops import WriteFileTool

    class FakeSandbox:
        def __init__(self): self.calls = []
        async def write_file(self, path, content, append=False):
            self.calls.append((path, content, append))
            return ToolResult(success=True, output="ok")
        async def read_file(self, path, max_lines=200):
            return ToolResult(success=True, output="x")

    sb = FakeSandbox()
    tool = WriteFileTool(sb)
    await tool.execute({"path": "/workspace/a.html", "content": "part1"})
    await tool.execute({"path": "/workspace/a.html", "content": "part2", "append": True})
    assert sb.calls == [("/workspace/a.html", "part1", False), ("/workspace/a.html", "part2", True)]
    assert "append" in tool.parameters_schema["properties"]
