"""Reference-aware actions with deterministic mechanical evidence."""

import asyncio

from playwright.async_api import Error as PlaywrightError

from .approvals import guard_key, guard_target
from .state import capture_state


async def target_for(session, page, request):
    if request.ref is not None:
        return await session.references.resolve(session, request.snapshot_id, request.ref)
    if request.selector:
        return page.locator(request.selector)
    if request.role:
        return page.get_by_role(request.role, name=request.text)
    return page.get_by_text(request.text, exact=False).first


async def finish(session, page, action, before, evidence):
    result = await session.describe(action, evidence=evidence)
    result.state_before = before["fingerprint"]
    try:
        after = await capture_state(page)
        result.state_after = after["fingerprint"]
        result.state_changed = result.state_before != result.state_after
        result.action_verification["url_changed"] = before["url"] != after["url"]
    except PlaywrightError:
        result.data["warning"] = "Action dispatched, but page closed or navigated before state capture; observe before proceeding"
    if action == "click" and not result.state_changed:
        result.data.setdefault("warning", "Click dispatched; no immediate page-state change detected")
    if "warning" in result.data:
        result.output += "\n" + result.data["warning"]
    return result


async def click(session, page, request):
    target = await target_for(session, page, request)
    await guard_target(session, target)
    before = await capture_state(page)
    await target.click(timeout=5000)
    return await finish(session, page, "click", before, {"dispatched": True})


async def type_text(session, page, request):
    target = await target_for(session, page, request)
    await guard_target(session, target)
    before = await capture_state(page)
    old_value = await target.evaluate("el => el.isContentEditable ? el.textContent : el.value")
    if request.clear_first:
        await target.fill(request.text, timeout=5000)
    else:
        await target.type(request.text, timeout=5000)
    expected = request.text if request.clear_first else (old_value or "") + request.text
    matches = await target.evaluate("(el, expected) => (el.isContentEditable ? el.textContent : el.value) === expected", expected)
    if request.press_enter and matches:
        await target.press("Enter", timeout=5000)
    result = await finish(session, page, "type", before, {"value_matches": matches, "submitted": request.press_enter and matches})
    if not matches:
        result.success = False
        result.error_code = "VALUE_MISMATCH"
        result.error = "Field value did not match the requested text; submission was skipped"
    return result


async def press(session, page, request):
    await guard_key(session, page, request.key)
    before = await capture_state(page)
    await page.keyboard.press(request.key)
    return await finish(session, page, "press", before, {"dispatched": True, "key": request.key})


async def wait(session, page, request):
    if request.condition == "text":
        await page.get_by_text(request.value, exact=False).first.wait_for(state="visible", timeout=request.timeout_ms)
    elif request.condition == "selector":
        await page.locator(request.value).wait_for(state="visible", timeout=request.timeout_ms)
    elif request.condition == "idle":
        await page.wait_for_load_state("networkidle", timeout=request.timeout_ms)
    else:
        await asyncio.sleep(request.milliseconds / 1000)
    return await session.describe("wait", evidence={"condition_met": True, "condition": request.condition})


SCROLL = """(el, {direction, pixels}) => {
    const node=el || document.scrollingElement;
    const before=[node.scrollLeft,node.scrollTop];
    let x=node.scrollLeft, y=node.scrollTop;
    if(direction==='top') y=0;
    if(direction==='bottom') y=node.scrollHeight;
    if(direction==='up') y-=pixels;
    if(direction==='down') y+=pixels;
    if(direction==='left') x-=pixels;
    if(direction==='right') x+=pixels;
    node.scrollTo({left:x,top:y,behavior:'instant'});
    return {before, position:[node.scrollLeft,node.scrollTop],
        moved:before[0]!==node.scrollLeft || before[1]!==node.scrollTop,
        at_top:node.scrollTop<=1, at_bottom:node.scrollTop+node.clientHeight>=node.scrollHeight-1,
        at_left:node.scrollLeft<=1, at_right:node.scrollLeft+node.clientWidth>=node.scrollWidth-1};
}"""


async def scroll(session, page, request):
    payload = {"direction": request.direction, "pixels": request.pixels}
    if request.ref is not None or request.selector:
        target = await target_for(session, page, request)
        evidence = await target.evaluate(SCROLL, payload)
    else:
        evidence = await page.evaluate(f"args => ({SCROLL})(null, args)", payload)
    if request.wait_ms:
        await asyncio.sleep(request.wait_ms / 1000)
    return await session.describe("scroll", evidence=evidence)
