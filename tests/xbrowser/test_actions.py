from sandbox.xbrowser.controller import execute
from sandbox.xbrowser.schemas import PressRequest, ScrollRequest, WaitRequest


async def test_keyboard_and_wait_conditions(browser, site):
    await browser.open(f"{site}/first.html")
    async with browser.operation() as page:
        await page.locator("#name").focus()
    pressed = await execute(browser, "press", PressRequest(key="A"))
    assert pressed.success and pressed.state_changed
    async with browser.operation() as page:
        assert await page.locator("#name").input_value() == "A"
    for condition, value in [("text", "XBrowser first page"), ("selector", "#name"), ("idle", None), ("ms", None)]:
        result = await execute(browser, "wait", WaitRequest(condition=condition, value=value, milliseconds=1))
        assert result.success and result.action_verification["condition_met"]
    failure = await execute(browser, "wait", WaitRequest(condition="selector", value="#absent", timeout_ms=30))
    assert not failure.success and failure.error_code == "ACTION_TIMEOUT"


async def test_container_scroll_reports_edges_and_movement(browser):
    async with browser.operation() as page:
        await page.set_content('<div id="box" style="height:100px;width:100px;overflow:auto"><div style="height:1000px">Content</div></div>')
    result = await execute(browser, "scroll", ScrollRequest(selector="#box", pixels=200, wait_ms=0))
    assert result.success and result.action_verification["moved"]
    assert result.state_changed
    assert result.action_verification["position"][1] == 200
    bottom = await execute(browser, "scroll", ScrollRequest(selector="#box", direction="bottom", wait_ms=0))
    assert bottom.action_verification["at_bottom"]
    stalled = await execute(browser, "scroll", ScrollRequest(selector="#box", direction="down", wait_ms=0))
    assert stalled.success and not stalled.action_verification["moved"]
