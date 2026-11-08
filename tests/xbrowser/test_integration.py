"""Agent client -> real sandbox routes -> Chromium, without external websites."""

from pathlib import Path

import httpx
import pytest

from local_agent.sandbox.client import SandboxClient
from local_agent.tools.browser import (
    BrowserClickTool,
    BrowserObserveTool,
    BrowserRunTool,
    BrowserScreenshotTool,
    BrowserTypeTool,
)
from local_agent.tools.registry import ToolRegistry
from sandbox import browser_server, tool_server


@pytest.fixture
async def client(browser, monkeypatch):
    monkeypatch.setattr(browser_server, "session", browser)
    agent = SandboxClient("http://sandbox")
    await agent.close()
    agent._client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=tool_server.app), base_url="http://sandbox",
    )
    async with tool_server.app.router.lifespan_context(tool_server.app):
        yield agent
    assert not browser.running  # The production shutdown hook owns cleanup.
    await agent.close()


async def test_foundation_end_to_end(client, site):
    started = await client.browser_session("start")
    assert started.success
    opened = await client.browser_open(f"{site}/first.html")
    assert opened.success
    first_id = opened.metadata["tab_id"]
    second = await client.browser_tabs("new", url=f"{site}/second.html")
    assert second.success
    second_id = second.metadata["tab_id"]
    assert second_id != first_id
    for tab_id, suffix in [(first_id, "first.html"), (second_id, "second.html")]:
        switched = await client.browser_tabs("switch", tab=tab_id)
        assert switched.success and switched.metadata["tab_id"] == tab_id
        state = await client.browser_tabs()
        active = next(tab for tab in state.metadata["data"]["tabs"] if tab["active"])
        assert active["tab_id"] == tab_id
        assert active["url"] == f"{site}/{suffix}"
    screenshot = await client.browser_screenshot(path="evidence/second.png")
    assert screenshot.success
    artifact = Path(screenshot.artifacts[0])
    assert artifact.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert screenshot.metadata["action_verification"]["size_bytes"] > 0
    assert screenshot.metadata["task_verification"] == "not_evaluated"
    stopped = await client.browser_session("stop")
    assert stopped.success and not stopped.metadata["data"]["running"]
    restarted = await client.browser_session("restart")
    assert restarted.success and restarted.metadata["restarted"]
    assert restarted.metadata["generation"] > opened.metadata["generation"]
    assert restarted.metadata["tab_id"] not in {first_id, second_id}
    invalid = await client.browser_tabs("switch", tab=first_id)
    assert not invalid.success and invalid.metadata["error_code"] == "TAB_NOT_FOUND"
    recovered = await client.browser_open(f"{site}/first.html")
    assert recovered.success and recovered.metadata["tab_id"] == restarted.metadata["tab_id"]


async def test_legacy_endpoints_share_active_tab(client, site):
    assert (await client.browser_navigate(f"{site}/first.html")).success
    assert (await client.browser_type("#name", "XBrowser")).success
    content = await client.browser_get_content()
    assert content.success and "XBrowser" in content.output
    html = await client.browser_get_content("html")
    assert html.success and "<h1>XBrowser first page</h1>" in html.output
    assert (await client.browser_click(text="Confirm", role="button")).success
    assert "dismissed" in (await client.browser_get_content("text")).output
    assert (await client.browser_screenshot(selector="#name")).success
    second = await client.browser_tabs("new", url=f"{site}/second.html")
    content = await client.browser_get_content("text")
    assert content.success and "second page" in content.output
    assert content.metadata["tab_id"] == second.metadata["tab_id"]


async def test_validation_and_artifact_failures(client, site):
    invalid = await client.browser_tabs("switch")
    assert not invalid.success and "422" in invalid.error
    unsafe = await client.browser_open("file:///etc/passwd")
    assert not unsafe.success and unsafe.metadata["error_code"] == "INVALID_URL"
    assert (await client.browser_open(f"{site}/first.html")).success
    escaped = await client.browser_screenshot(path="../escape.png")
    assert not escaped.success and escaped.metadata["error_code"] == "INVALID_PATH"
    assert (await client.browser_screenshot(path="same.png")).success
    duplicate = await client.browser_screenshot(path="same.png")
    assert not duplicate.success and duplicate.metadata["error_code"] == "ARTIFACT_EXISTS"


async def test_scroll_reports_actual_movement(client, site):
    await client.browser_open(f"{site}/first.html")
    moved = await client._post("/tools/browser/scroll", {"direction": "down", "pixels": 300, "wait_ms": 0})
    assert moved.success and moved.metadata["action_verification"]["moved"]
    assert moved.metadata["action_verification"]["position"][1] == 300


async def test_executor_tools_observe_type_and_click_by_ref(client, site):
    from local_agent.security.permissions import PermissionPolicy, PermissionTier
    from local_agent.tools.base import VerificationStatus

    registry = ToolRegistry(PermissionPolicy(auto_approve_up_to=PermissionTier.NETWORK))
    registry.register_many(BrowserObserveTool(client), BrowserClickTool(client), BrowserTypeTool(client))
    await client.browser_open(f"{site}/marks.html")
    observed, verification = await registry.execute_and_verify("browser_observe", {})
    assert observed.success and verification.status == VerificationStatus.PASSED
    fields = observed.metadata["data"]["elements"]
    field = next(item for item in fields if item["name"] == "Display name")
    button = next(item for item in fields if item["name"] == "Save profile")
    snapshot_id = observed.metadata["snapshot_id"]
    typed, verification = await registry.execute_and_verify("browser_type", {
        "ref": field["ref"], "snapshot_id": snapshot_id, "text": "Grace Hopper",
    })
    assert typed.success and verification.status == VerificationStatus.PASSED
    clicked, _ = await registry.execute_and_verify("browser_click", {
        "ref": button["ref"], "snapshot_id": snapshot_id,
    })
    assert clicked.success and clicked.metadata["state_changed"]
    assert "Saved" in (await client.browser_get_content("text")).output
    await client.browser_observe()  # Supersedes the previous snapshot.
    stale = await client.browser_click(ref=button["ref"], snapshot_id=snapshot_id)
    assert not stale.success and stale.metadata["error_code"] == "STALE_REF"
    invalid = await client.browser_click(ref=button["ref"])
    assert not invalid.success and "422" in invalid.error


async def test_macro_tool_uses_validated_http_route(client, site):
    tool = BrowserRunTool(client)
    steps = [{"tool": "open", "args": {"url": f"{site}/first.html"}},
             {"tool": "wait", "args": {"condition": "text", "value": "XBrowser first page"}},
             {"tool": "screenshot", "args": {"path": "macro-evidence.png"}}]
    result = await tool.execute({"steps": steps})
    assert result.success and result.metadata["data"]["attempted_actions"] == 3
    assert len(result.artifacts) == 1 and Path(result.artifacts[0]).exists()
    observed = await client.browser_observe()
    assert f"State: {observed.metadata['state_after']}" in observed.output
    invalid = await client.browser_run([{"tool": "open", "args": {"url": f"{site}/second.html"}},
                                        {"tool": "type", "args": {"selector": "#name"}}])
    assert not invalid.success and "422" in invalid.error
    assert (await client.browser_tabs()).metadata["data"]["tabs"][0]["url"].endswith("first.html")


async def test_sensitive_approval_requires_admin_and_is_not_a_model_tool(client, site, monkeypatch):
    await client.browser_open(f"{site}/sensitive.html")
    args = {"selector": "#password", "text": "approved-test-value"}
    denied = await client.browser_authorize("type", args, "wrong")
    assert not denied.success and "403" in denied.error
    monkeypatch.setenv("XBROWSER_ADMIN_TOKEN", "test-admin-key")
    granted = await client.browser_authorize("type", args, "test-admin-key")
    assert granted.success
    client.use_browser_approval(granted.metadata["data"]["approval_token"])
    assert (await client.browser_type(**args)).success
    assert not (await client.browser_type(**args)).success  # Next call has no reusable grant.
    checks = await client.browser_verify([{"kind": "url", "expected": f"{site}/sensitive.html"}])
    assert checks.success and checks.metadata["task_verification"] == "passed"


async def test_vision_fallback_transfers_only_confined_png_and_preserves_evidence(client, site):
    import base64

    from local_agent.models import ChatResponse

    class Vision:
        async def chat(self, role, messages, **kwargs):
            assert role == "vision"
            assert base64.b64decode(messages[1]["images"][0]).startswith(b"\x89PNG")
            assert "untrusted" in messages[0]["content"]
            return ChatResponse("The page shows a Name field and a link.", [], "test-vision", "assistant", True)

    await client.browser_open(f"{site}/first.html")
    result = await BrowserScreenshotTool(client, vision_model=Vision()).execute({"describe": True, "marks": True})
    assert result.success and len(result.artifacts) == 1
    assert "Visual observation (untrusted)" in result.output and "vision" in result.metadata
    invalid = await client.browser_image("../outside.png")
    assert not invalid.success and invalid.metadata["error_code"] == "INVALID_PATH"


async def test_shell_cannot_bypass_browser_security_without_admin(client, monkeypatch, browser):
    monkeypatch.delenv("SANDBOX_ADMIN_TOKEN", raising=False)
    result = await client.execute_shell("echo privileged")
    assert not result.success and "403" in result.error
    monkeypatch.setenv("SANDBOX_ADMIN_TOKEN", "test-shell-control")
    client._admin_token = "test-shell-control"
    authorized = await client.execute_shell("echo ADMIN_OK", working_dir=str(browser.workspace))
    assert authorized.success and "ADMIN_OK" in authorized.output
