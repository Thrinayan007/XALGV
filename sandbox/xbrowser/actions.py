"""Reference-aware actions with deterministic mechanical evidence."""

import asyncio
from contextlib import asynccontextmanager, suppress

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Locator

from .approvals import guard_key, guard_target
from .errors import BrowserError
from .marks import INSPECT, resolve_semantic
from .state import capture_state


async def target_for(session, page, request):
    if request.ref is not None:
        return await session.references.resolve(session, request.snapshot_id, request.ref)
    if request.selector:
        return page.locator(request.selector)
    if request.role:
        return page.get_by_role(request.role, name=request.text)
    return page.get_by_text(request.text, exact=False).first


@asynccontextmanager
async def action_target(session, page, request):
    semantic = request.semantic_target
    if semantic is None:
        target = await target_for(session, page, request)
        if isinstance(target, Locator):
            if session._approval is not None and not request.selector:
                raise BrowserError("TARGET_MISMATCH", "Reviewed actions require an exact reference, semantic target, or unique selector")
            target = await target.element_handle(timeout=5000)
            try:
                yield target, "legacy"
            finally:
                with suppress(PlaywrightError):
                    await target.dispose()
        else:
            yield target, "reference"
        return
    reference = None
    if request.ref is not None:
        reference = session.references.lookup(session, request.snapshot_id, request.ref)
        info = reference.info
        if (info["name_truncated"] or info["nearby_truncated"] or semantic.role != info["role"] or
                semantic.name != info["name"] or semantic.frame_id not in (None, reference.frame_id) or
                semantic.nearby_label not in (None, info["nearby_label"])):
            raise BrowserError("TARGET_MISMATCH", "Semantic target does not match the observed reference")
        try:
            target = await session.references.resolve(session, request.snapshot_id, request.ref)
        except BrowserError as exc:
            if exc.code != "STALE_REF" or session._approval is not None:
                raise
            session.references.lookup(session, request.snapshot_id, request.ref)
            try:
                current = await reference.handle.evaluate(INSPECT)
            except PlaywrightError:
                raise exc from None
            if current["connected"]:
                raise
            unique = info["unique_group"] if semantic.nearby_label else info["unique_name"]
            if not unique:
                raise BrowserError("AMBIGUOUS_TARGET", "The original observation did not establish a unique target") from exc
        else:
            yield target, "reference"
            return
    frame = reference.frame if reference else page.main_frame
    target = await resolve_semantic(frame, semantic, reference.info if reference else None)
    try:
        if reference:
            session.references.lookup(session, request.snapshot_id, request.ref)
        yield target, "semantic_recovery" if reference else "semantic"
    finally:
        with suppress(PlaywrightError):
            await target.dispose()


async def finish(session, page, action, before, evidence):
    result = await session.describe(action, evidence=evidence)
    result.state_before = before["fingerprint"]
    try:
        after = await capture_state(page)
        result.state_after = after["fingerprint"]
        result.state_changed = result.state_before != result.state_after
        result.action_verification["url_changed"] = before["url"] != after["url"]
        result.action_verification["viewport_moved"] = (before["x"], before["y"]) != (after["x"], after["y"])
    except PlaywrightError:
        result.data["warning"] = "Action dispatched, but page closed or navigated before state capture; observe before proceeding"
    if action == "click" and not result.state_changed:
        result.data.setdefault("warning", "Click dispatched; no immediate page-state change detected")
    if "warning" in result.data:
        result.output += "\n" + result.data["warning"]
    return result


async def click(session, page, request):
    before = await capture_state(page)
    async with action_target(session, page, request) as (target, strategy):
        await guard_target(session, target, click=True)
        if session._approval:
            session._approval.armed = True
        await target.click(timeout=5000)
    return await finish(session, page, "click", before, {"dispatched": True, "target_strategy": strategy})


async def type_text(session, page, request):
    before = await capture_state(page)
    async with action_target(session, page, request) as (target, strategy):
        await guard_target(session, target, submits=request.press_enter)
        old_value = await target.evaluate("el => el.isContentEditable ? el.textContent : el.value")
        if session._approval:
            session._approval.armed = True
        if request.clear_first:
            await target.fill(request.text, timeout=5000)
        else:
            await target.type(request.text, timeout=5000)
        expected = request.text if request.clear_first else (old_value or "") + request.text
        matches = await target.evaluate("(el, expected) => (el.isContentEditable ? el.textContent : el.value) === expected", expected)
        if request.press_enter and matches:
            await target.press("Enter", timeout=5000)
    result = await finish(session, page, "type", before, {
        "value_matches": matches, "value_changed": matches and expected != old_value,
        "submitted": request.press_enter and matches, "target_strategy": strategy,
    })
    if not matches:
        result.success = False
        result.error_code = "VALUE_MISMATCH"
        result.error = "Field value did not match the requested text; submission was skipped"
    return result


async def press(session, page, request):
    await guard_key(session, page, request.key)
    before = await capture_state(page)
    if session._approval:
        session._approval.armed = True
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
