import asyncio

import pytest

from sandbox.xbrowser import BrowserSession
from sandbox.xbrowser.errors import BrowserError


async def test_lifecycle_and_persistent_storage(browser, site):
    health = await browser.control("health")
    assert not health.data["running"]
    assert health.generation == 0  # Health checks never launch Chromium.
    first = await browser.open(f"{site}/first.html")
    assert first.success and first.generation == 1
    async with browser.operation() as page:
        await page.evaluate("localStorage.setItem('persisted', 'yes')")
    assert (await browser.control("start")).generation == 1
    await browser.control("stop")
    second = await browser.open(f"{site}/first.html")
    assert second.generation == 2
    assert second.tab_id != first.tab_id
    async with browser.operation() as page:
        assert await page.evaluate("localStorage.getItem('persisted')") == "yes"


async def test_unexpected_context_close_recovers_without_reusing_ids(browser, site):
    original = await browser.open(f"{site}/first.html")
    await browser._context.close()
    restored = await browser.tab_action("list")
    assert restored.restarted
    assert restored.generation == original.generation + 1
    assert restored.tab_id != original.tab_id
    with pytest.raises(BrowserError, match="Unknown or closed tab"):
        await browser.tab_action("switch", tab=original.tab_id)


async def test_explicit_restart(browser):
    original = await browser.control("start")
    restarted = await browser.control("restart")
    assert restarted.restarted
    assert restarted.session_id == original.session_id
    assert restarted.generation == original.generation + 1
    assert restarted.tab_id != original.tab_id


async def test_operations_are_serialized_and_cancellation_releases_lock(browser):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def hold():
        async with browser.operation():
            entered.set()
            await release.wait()

    holder = asyncio.create_task(hold())
    await asyncio.wait_for(entered.wait(), 30)
    waiter = asyncio.create_task(browser.control("stop"))
    await asyncio.sleep(0)
    assert not waiter.done()
    holder.cancel()
    with pytest.raises(asyncio.CancelledError):
        await holder
    await asyncio.wait_for(waiter, 10)
    assert not browser.running


async def test_profile_scopes_are_isolated(browser, tmp_path, site):
    await browser.open(f"{site}/first.html")
    async with browser.operation() as page:
        await page.evaluate("localStorage.setItem('private', 'secret')")
    other = BrowserSession(tmp_path, scope="other", headless=True)
    other.policy = browser.policy
    try:
        await other.open(f"{site}/first.html")
        async with other.operation() as page:
            assert await page.evaluate("localStorage.getItem('private')") is None
    finally:
        await other.control("stop")


async def test_screenshots_are_unique_and_never_overwritten(browser, site):
    await browser.open(f"{site}/first.html")
    async with browser.operation() as page:
        first = await browser.capture(page)
        second = await browser.capture(page)
        assert first != second
        assert first.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
        before = first.read_bytes()
        with pytest.raises(BrowserError) as exc:
            await browser.capture(page, path=str(first))
        assert exc.value.code == "ARTIFACT_EXISTS"
        assert first.read_bytes() == before


async def test_failed_capture_removes_partial_artifact(browser):
    class FailedPage:
        async def screenshot(self, **kwargs):
            raise RuntimeError("capture interrupted")

    with pytest.raises(RuntimeError, match="capture interrupted"):
        await browser.capture(FailedPage(), path="incomplete.png")
    assert not (browser.workspace / "incomplete.png").exists()


async def test_crash_notification_triggers_new_generation(browser):
    original = await browser.control("start")
    browser._on_crash()  # Inject the renderer event without hanging a CDP Page.crash call.
    recovered = await browser.tab_action("list")
    assert recovered.restarted and recovered.generation == original.generation + 1
    assert recovered.tab_id != original.tab_id


@pytest.mark.parametrize("path", ["../escape.png", ".xbrowser/leak.png", "capture.jpg"])
def test_screenshot_paths_confined(tmp_path, path):
    with pytest.raises(BrowserError):
        BrowserSession(tmp_path).screenshot_target(path)


@pytest.mark.parametrize("url", ["file:///etc/passwd", "javascript:alert(1)", "data:text/html,x", "https://u:p@host/"])
def test_unsafe_schemes_and_credentials_rejected(url):
    with pytest.raises(BrowserError):
        BrowserSession.validate_url(url)


def test_invalid_scope_rejected(tmp_path):
    with pytest.raises(ValueError):
        BrowserSession(tmp_path, scope="../other")
