"""Shared validated action execution. The public boundary owns the lock once."""

import asyncio
import hashlib
import json

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout

from . import actions
from .errors import BrowserError
from .observe import observe
from .schemas import BrowserResponse, ObserveRequest
from .state import capture_state
from .token_budget import token_count


def failure(session, action, exc, *, code=None):
    if code is None:
        code = exc.code if isinstance(exc, BrowserError) else (
            "ACTION_TIMEOUT" if isinstance(exc, (TimeoutError, PlaywrightTimeout)) else "BROWSER_ERROR"
        )
    uncertain = code == "ACTION_TIMEOUT"
    return BrowserResponse(success=False, action=action, error=str(exc) or code, error_code=code,
                           output="Action timed out; observe before retrying. Side effects may have completed." if uncertain else "",
                           data={"outcome_uncertain": True} if uncertain else {},
                           session_id=session.session_id, generation=session.generation,
                           tab_id=session.tabs.active, restarted=session._restarted)


async def state(session):
    data = {"session": session.session_id, "generation": session.generation, "tab": session.tabs.active}
    if session.tabs.active:
        data["page"] = (await capture_state(session.tabs.page()))["fingerprint"]
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16]


async def dispatch(session, action, request):
    """Internal: caller must already own session.operation(). Never retries."""
    session.policy.check(action)
    if not session.running:
        raise BrowserError("SESSION_LOST", "Browser stopped during the sequence; observe after restarting")
    if session.tabs.active is None:
        await session._context.new_page()
    page = session.tabs.page()
    before = await state(session)
    if action == "open":
        result = await session._open(page, **request.model_dump())
    elif action == "tabs":
        result = await session._tab_action(page, **request.model_dump())
    elif action == "observe":
        result = await observe(session, page, request)
    elif action == "screenshot":
        observation = await observe(session, page, ObserveRequest()) if request.marks else None
        path = await session.capture(page, **request.model_dump(), annotations=observation.data["elements"] if observation else None)
        result = await session.describe(action, artifacts=[str(path)], evidence={
            "file_exists": path.is_file(), "size_bytes": path.stat().st_size,
        })
        result.output = f"Screenshot saved to {path}\n" + result.output
        if observation:
            result.snapshot_id = observation.snapshot_id
            result.data["elements"] = observation.data["elements"]
            result.output += "\n" + observation.output
    else:
        method = {"click": actions.click, "type": actions.type_text, "press": actions.press,
                  "wait": actions.wait, "scroll": actions.scroll}[action]
        result = await method(session, page, request)
    # All actions expose the same scope-aware state, including navigation/tabs.
    previous_state = result.state_after
    result.state_before = before
    try:
        result.state_after = await state(session)
        result.state_changed = result.state_after != before
        if action == "observe" and previous_state:
            result.output = result.output.replace(f"State: {previous_state}", f"State: {result.state_after}")
            result.data["token_count"] = token_count(result.output)
    except (BrowserError, PlaywrightError):
        result.data["warning"] = "Action completed; subsequent browser state is unavailable. Observe before proceeding."
    return result


async def execute(session, action, request, *, approval_token=None):
    try:
        session.policy.check(action)  # Deny without launching a browser.
        async with asyncio.timeout(35):
            async with session.operation():
                if approval_token:
                    await session.approvals.consume(approval_token, session, action, request)
                    session._approved = True
                return await dispatch(session, action, request)
    except (BrowserError, PlaywrightError, OSError, ValueError, TimeoutError) as exc:
        return failure(session, action, exc)
