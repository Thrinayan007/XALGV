import asyncio
from pathlib import Path

import pytest
from pydantic import ValidationError

from sandbox.xbrowser import macros
from sandbox.xbrowser.controller import execute
from sandbox.xbrowser.safety import CORE_ACTIONS, BrowserPolicy
from sandbox.xbrowser.schemas import OpenRequest, RunRequest


def request(steps, **kwargs):
    return RunRequest(steps=steps, **kwargs)


def shot(path):
    return {"tool": "screenshot", "args": {"path": path}}


async def test_navigate_and_five_screenshots_in_one_macro(browser, site):
    steps = [{"tool": "open", "args": {"url": f"{site}/first.html"}}]
    for index in range(5):
        if index:
            steps.append({"tool": "scroll", "args": {"pixels": 300, "wait_ms": 0}})
        steps.append(shot(f"evidence/shot-{index}.png"))
    result = await macros.run(browser, request(steps))
    assert result.success
    assert result.data["attempted_actions"] == 10
    assert result.data["failed_index"] is None
    assert len(result.artifacts) == 5
    assert result.task_verification == "not_evaluated"
    for artifact in result.artifacts:
        assert Path(artifact).read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert result.state_before != result.state_after


async def test_stops_on_failure_and_preserves_prior_evidence(browser):
    result = await macros.run(browser, request([shot("first.png"), shot("first.png"), shot("never.png")]))
    assert not result.success and result.error_code == "ARTIFACT_EXISTS"
    assert result.data["failed_index"] == 1
    assert result.data["attempted_actions"] == 2
    assert len(result.artifacts) == 1
    assert not (browser.workspace / "never.png").exists()


@pytest.mark.parametrize("steps", [[], [shot("x.png")] * 21,
    [{"tool": "eval", "args": {"js": "alert(1)"}}],
    [{"tool": "open", "args": {"url": "about:blank"}}, {"tool": "type", "args": {"selector": "input"}}],
    [{"tool": "run", "args": {"steps": []}}]])
def test_entire_macro_is_validated_before_execution(steps):
    with pytest.raises(ValidationError):
        request(steps)


def test_action_allowance_rejects_oversized_macro():
    with pytest.raises(ValidationError):
        request([shot("a.png"), shot("b.png")], action_budget=1)


async def test_capability_denial_applies_to_single_and_macro_without_starting_browser(browser):
    browser.policy = BrowserPolicy(frozenset({"observe", "screenshot"}))
    single = await execute(browser, "open", OpenRequest(url="about:blank"))
    macro = await macros.run(browser, request([shot("should-not-exist.png"), {"tool": "open", "args": {"url": "about:blank"}}]))
    assert single.error_code == macro.error_code == "ACTION_DENIED"
    assert macro.data["attempted_actions"] == 0
    assert browser.generation == 0
    assert not (browser.workspace / "should-not-exist.png").exists()


async def test_capabilities_rechecked_between_macro_steps(browser, monkeypatch):
    original = macros.dispatch

    async def change_policy(session, action, args):
        result = await original(session, action, args)
        session.policy = BrowserPolicy(CORE_ACTIONS - {"screenshot"})
        return result

    monkeypatch.setattr(macros, "dispatch", change_policy)
    result = await macros.run(browser, request([{"tool": "open", "args": {"url": "about:blank"}}, shot("denied.png")]))
    assert not result.success and result.error_code == "ACTION_DENIED"
    assert result.data["attempted_actions"] == 2
    assert not (browser.workspace / "denied.png").exists()


async def test_shared_deadline_returns_partial_artifacts(browser, monkeypatch):
    await browser.control("start")
    original = macros.dispatch

    async def fast_evidence(session, action, args):
        if action == "screenshot":
            path = session.workspace / args.path
            path.write_bytes(b"completed evidence")
            return await session.describe(action, artifacts=[str(path)])
        return await original(session, action, args)

    # Isolate deadline behavior from platform-dependent screenshot rendering time;
    # real PNG creation is covered by the five-capture workflow above.
    monkeypatch.setattr(macros, "dispatch", fast_evidence)
    result = await macros.run(browser, request([
        shot("before-timeout.png"), {"tool": "wait", "args": {"condition": "ms", "milliseconds": 1500}}, shot("late.png"),
    ], timeout_ms=500))
    assert not result.success and result.error_code == "ACTION_TIMEOUT"
    assert result.data["failed_index"] == 1
    assert len(result.artifacts) == 1
    assert not (browser.workspace / "late.png").exists()
    assert not browser._lock.locked()


async def test_deadline_includes_lock_wait(browser):
    async with browser.operation():
        result = await macros.run(browser, request([shot("locked.png")], timeout_ms=30))
    assert not result.success and result.error_code == "ACTION_TIMEOUT"
    assert result.data["attempted_actions"] == 0


async def test_cancellation_keeps_evidence_and_prevents_later_steps(browser, monkeypatch):
    entered = asyncio.Event()
    original = macros.dispatch

    async def notify(session, action, args):
        if action == "wait":
            entered.set()
        return await original(session, action, args)

    monkeypatch.setattr(macros, "dispatch", notify)
    task = asyncio.create_task(macros.run(browser, request([
        shot("before-cancel.png"), {"tool": "wait", "args": {"condition": "ms", "milliseconds": 5000}}, shot("after-cancel.png"),
    ])))
    await asyncio.wait_for(entered.wait(), 20)
    competing = asyncio.create_task(browser.tab_action("new"))
    await asyncio.sleep(0)
    assert not competing.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (browser.workspace / "before-cancel.png").is_file()
    assert not (browser.workspace / "after-cancel.png").exists()
    assert (await asyncio.wait_for(competing, 5)).success


async def test_client_disconnect_cancels_remaining_macro(browser):
    class Connection:
        async def is_disconnected(self):
            return True

    await browser.control("start")
    with pytest.raises(asyncio.CancelledError):
        await macros.run_connected(browser, request([
            {"tool": "wait", "args": {"condition": "ms", "milliseconds": 3000}}, shot("disconnected.png"),
        ]), Connection())
    assert not browser._lock.locked()
    assert not (browser.workspace / "disconnected.png").exists()
