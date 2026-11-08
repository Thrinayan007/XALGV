import asyncio

import pytest

from sandbox.xbrowser.errors import BrowserError
from sandbox.xbrowser.tabs import TabRegistry


async def test_switch_close_and_active_url(browser, site):
    first = await browser.open(f"{site}/first.html")
    second = await browser.tab_action("new", url=f"{site}/second.html")
    assert first.tab_id != second.tab_id
    assert len(second.data["tabs"]) == 2
    switched = await browser.tab_action("switch", tab=first.tab_id)
    assert switched.tab_id == first.tab_id
    active = next(tab for tab in switched.data["tabs"] if tab["active"])
    assert active["url"] == f"{site}/first.html"
    assert active["title"] == "First fixture"
    closed = await browser.tab_action("close", tab=first.tab_id)
    assert closed.tab_id == second.tab_id
    assert len(closed.data["tabs"]) == 1
    await browser.tab_action("close", tab=second.tab_id)
    reopened = await browser.tab_action("list")
    assert reopened.tab_id not in {first.tab_id, second.tab_id}


async def test_popup_registration_and_external_close(browser, site):
    first = await browser.open(f"{site}/first.html")
    async with browser.operation() as page:
        async with page.expect_popup() as popup_event:
            await page.get_by_text("Open second tab").click()
        popup = await popup_event.value
        await popup.wait_for_load_state()
    state = await browser.tab_action("list")
    assert len(state.data["tabs"]) == 2
    assert state.tab_id != first.tab_id
    await popup.close()
    state = await browser.tab_action("list")
    assert state.tab_id == first.tab_id


async def test_invalid_tab_does_not_change_active(browser):
    original = await browser.control("start")
    with pytest.raises(BrowserError) as exc:
        await browser.tab_action("switch", tab="t999")
    assert exc.value.code == "TAB_NOT_FOUND"
    assert browser.tabs.active == original.tab_id


async def test_concurrent_new_tabs_get_distinct_ids(browser, site):
    await browser.control("start")
    results = await asyncio.gather(*[
        browser.tab_action("new", url=f"{site}/second.html") for _ in range(3)
    ])
    assert len({result.tab_id for result in results}) == 3
    assert len((await browser.tab_action("list")).data["tabs"]) == 4


async def test_history_navigation(browser, site):
    await browser.open(f"{site}/first.html")
    await browser.open(f"{site}/second.html")
    back = await browser.tab_action("back")
    assert back.data["tabs"][0]["url"].endswith("first.html")
    forward = await browser.tab_action("forward")
    assert forward.data["tabs"][0]["url"].endswith("second.html")
    assert (await browser.tab_action("reload")).data["tabs"][0]["title"] == "Second fixture"


async def test_confirmation_is_dismissed(browser, site):
    await browser.open(f"{site}/first.html")
    async with browser.operation() as page:
        await page.get_by_role("button", name="Confirm").click()
        assert await page.locator("#answer").inner_text() == "dismissed"
    assert browser.events[-1]["kind"] == "confirm"


def test_registry_registration_is_idempotent_and_ids_survive_clear():
    class Page:
        def on(self, *args):
            pass

    registry = TabRegistry()
    page = Page()
    assert registry.register(page) == registry.register(page) == "t1"
    registry.clear()
    assert registry.register(page) == "t2"
