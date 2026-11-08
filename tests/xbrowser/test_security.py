import asyncio
import socket
from urllib.parse import quote

import pytest

from sandbox.xbrowser.controller import execute
from sandbox.xbrowser.errors import BrowserError
from sandbox.xbrowser.safety import BrowserPolicy
from sandbox.xbrowser.schemas import ClickRequest, OpenRequest, PressRequest, TypeRequest


@pytest.mark.parametrize("url", ["http://127.0.0.1", "http://[::1]", "http://169.254.169.254", "http://10.0.0.1",
                                  "http://2130706433", "http://0x7f000001", "http://[::ffff:127.0.0.1]",
                                  "http://[64:ff9b::a00:1]", "http://224.0.0.1"])
async def test_default_policy_blocks_private_metadata_and_encoded_ips(url):
    with pytest.raises(BrowserError) as error:
        await BrowserPolicy().resolve(url)
    assert error.value.code == "NAVIGATION_DENIED"


async def test_mixed_dns_answers_fail_closed(monkeypatch):
    async def addresses(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 80)) for ip in ("93.184.215.14", "127.0.0.1")]
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", addresses)
    with pytest.raises(BrowserError):
        await BrowserPolicy().resolve("http://public.example")


def test_domain_policy_uses_exact_and_explicit_subdomain_rules():
    policy = BrowserPolicy(allowed_domains=("example.com", "*.example.org"), denied_domains=("bad.example.org",))
    assert policy.destination("https://example.com")[0] == "example.com"
    assert policy.destination("https://good.example.org")[0] == "good.example.org"
    for url in ["https://example.com.attacker.test", "https://bad.example.org", "https://x.example.com"]:
        with pytest.raises(BrowserError):
            policy.destination(url)


async def test_redirect_and_subresource_cannot_bypass_private_network_policy(browser, site):
    result = await execute(browser, "open", OpenRequest(url=f"{site}/redirect?target={quote('http://127.0.0.1/private', safe='')}"))
    assert not result.success
    async with browser.operation() as page:
        await page.goto(f"{site}/first.html")
        blocked = await page.evaluate("""async () => {
            try { await fetch('http://127.0.0.1:1/private'); return false; } catch { return true; }
        }""")
        assert blocked


async def test_sensitive_actions_fail_closed_and_single_use_approval_is_scoped(browser, site):
    await browser.open(f"{site}/sensitive.html")
    request = TypeRequest(selector="#password", text="do-not-echo")
    denied = await execute(browser, "type", request)
    assert denied.error_code == "HUMAN_APPROVAL_REQUIRED"
    assert "do-not-echo" not in denied.model_dump_json()
    async with browser.operation():
        token = await browser.approvals.issue(browser, "type", request)
    approved = await execute(browser, "type", request, approval_token=token)
    assert approved.success
    replay = await execute(browser, "type", request, approval_token=token)
    assert replay.error_code == "APPROVAL_INVALID"
    click = await execute(browser, "click", ClickRequest(selector="#submit"))
    assert click.error_code == "HUMAN_APPROVAL_REQUIRED"
    key = await execute(browser, "press", PressRequest(key="Enter"))
    assert key.error_code == "HUMAN_APPROVAL_REQUIRED"
    async with browser.operation():
        token = await browser.approvals.issue(browser, "type", request)
    altered = await execute(browser, "type", TypeRequest(selector="#password", text="changed"), approval_token=token)
    assert altered.error_code == "APPROVAL_INVALID"


async def test_scripted_network_write_is_blocked_without_approval(browser, site):
    await browser.open(f"{site}/first.html")
    async with browser.operation() as page:
        assert await page.evaluate("""async () => {
            try { await fetch('/submitted', {method:'POST',body:'write'}); return false; } catch { return true; }
        }""")
    assert any(event.get("code") == "HUMAN_APPROVAL_REQUIRED" for event in browser.events)


@pytest.mark.parametrize("key", ["Control+L", "Control+O", "Control+Shift+J", "F12", "Alt+F4", "Control+S"])
async def test_browser_chrome_and_developer_shortcuts_are_disabled(browser, key):
    result = await execute(browser, "press", PressRequest(key=key))
    assert not result.success and result.error_code == "KEY_DENIED"


async def test_file_api_cannot_read_process_environment_or_profiles(tmp_path, monkeypatch):
    from sandbox import tool_server
    monkeypatch.setattr(tool_server, "WORKSPACE", tmp_path)
    outside = tmp_path.parent / "outside-secret.txt"
    outside.write_text("administrative secret")
    profile = tmp_path / ".xbrowser" / "profile" / "cookies.txt"
    profile.parent.mkdir(parents=True)
    profile.write_text("private cookies")
    for path in (outside, profile):
        result = await tool_server.read_file(tool_server.ReadFileRequest(path=str(path)))
        assert not result.success
    allowed = tmp_path / "report.md"
    allowed.write_text("public report")
    assert (await tool_server.read_file(tool_server.ReadFileRequest(path=str(allowed)))).success


async def test_webrtc_and_file_uploads_are_disabled(browser, site):
    await browser.open(f"{site}/sensitive.html")
    async with browser.operation() as page:
        assert await page.evaluate("typeof RTCPeerConnection") == "undefined"
    result = await execute(browser, "click", ClickRequest(selector="#upload"))
    assert not result.success and result.error_code == "UPLOAD_DISABLED"
