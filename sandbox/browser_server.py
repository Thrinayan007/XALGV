"""Thin browser API with legacy compatibility over the XBrowser session."""

from __future__ import annotations

import logging
import os
import secrets
from contextlib import asynccontextmanager

from fastapi import APIRouter, Header, HTTPException
from fastapi import Request as Connection
from playwright.async_api import Error as PlaywrightError
from pydantic import TypeAdapter

if __package__:  # Package imports in tests; flat imports in the Docker image.
    from .xbrowser import macros
    from .xbrowser.controller import execute
    from .xbrowser.controller import failure as action_failure
    from .xbrowser.errors import BrowserError
    from .xbrowser.images import image_data
    from .xbrowser.safety import BrowserPolicy
    from .xbrowser.schemas import (
        ApprovalRequest,
        BrowserResponse,
        BrowserStep,
        ClickRequest,
        ImageRequest,
        ObserveRequest,
        OpenRequest,
        PressRequest,
        Request,
        RunRequest,
        ScreenshotRequest,
        ScrollRequest,
        SessionRequest,
        TabsRequest,
        TypeRequest,
        VerificationRequest,
        WaitRequest,
    )
    from .xbrowser.session import BrowserSession
    from .xbrowser.verification import verify as verify_task
else:
    from xbrowser import macros
    from xbrowser.controller import execute
    from xbrowser.controller import failure as action_failure
    from xbrowser.errors import BrowserError
    from xbrowser.images import image_data
    from xbrowser.safety import BrowserPolicy
    from xbrowser.schemas import (
        ApprovalRequest,
        BrowserResponse,
        BrowserStep,
        ClickRequest,
        ImageRequest,
        ObserveRequest,
        OpenRequest,
        PressRequest,
        Request,
        RunRequest,
        ScreenshotRequest,
        ScrollRequest,
        SessionRequest,
        TabsRequest,
        TypeRequest,
        VerificationRequest,
        WaitRequest,
    )
    from xbrowser.session import BrowserSession
    from xbrowser.verification import verify as verify_task

logger = logging.getLogger(__name__)
session = BrowserSession(
    os.environ.get("XBROWSER_WORKSPACE", "/workspace"),
    scope=os.environ.get("XBROWSER_SCOPE", "default"),
    headless=os.environ.get("XBROWSER_HEADLESS", "0") == "1",
)

session.policy = BrowserPolicy.from_env()


@asynccontextmanager
async def lifespan(_app):
    try:
        yield
    finally:
        await session.control("stop")


router = APIRouter(prefix="/tools/browser", tags=["browser"], lifespan=lifespan)


class ContentRequest(Request):
    mode: str = "a11y"
    selector: str | None = None


def failure(action: str, exc: Exception):
    logger.warning("Browser %s failed: %s", action, exc)
    return action_failure(session, action, exc)


@router.post("/session", response_model=BrowserResponse)
async def session_control(req: SessionRequest):
    try:
        return await session.control(req.action)
    except (BrowserError, PlaywrightError, OSError, ValueError) as exc:
        return failure(f"session_{req.action}", exc)


@router.post("/open", response_model=BrowserResponse)
@router.post("/navigate", response_model=BrowserResponse)
async def navigate(req: OpenRequest):
    return await execute(session, "open", req)


@router.post("/tabs", response_model=BrowserResponse)
async def tabs(req: TabsRequest):
    return await execute(session, "tabs", req)


@router.post("/get_content", response_model=BrowserResponse)
async def get_content(req: ContentRequest):
    try:
        session.policy.check("observe")
        async with session.operation() as page:
            target = page.locator(req.selector or "body")
            if req.mode == "a11y":
                output = await target.aria_snapshot(timeout=5000)
            elif req.mode == "text":
                output = await target.inner_text(timeout=5000)
            elif req.mode == "html":
                if req.selector:
                    output = await target.inner_html(timeout=5000)
                else:
                    output = await page.evaluate("""() => {
                        const clone = document.body.cloneNode(true);
                        clone.querySelectorAll('script, style, noscript, svg, img').forEach(el => el.remove());
                        return clone.innerHTML.replace(/\\s+/g, ' ').trim();
                    }""")
            else:
                raise BrowserError("INVALID_MODE", f"Unknown content mode: {req.mode}")
            result = await session.describe("get_content")
            result.output = output[:20000]
            return result
    except (BrowserError, PlaywrightError, OSError, ValueError) as exc:
        return failure("get_content", exc)


@router.post("/observe", response_model=BrowserResponse)
async def observe(req: ObserveRequest):
    return await execute(session, "observe", req)


@router.post("/click", response_model=BrowserResponse)
async def click(req: ClickRequest, x_xbrowser_approval: str | None = Header(default=None)):
    return await execute(session, "click", req, approval_token=x_xbrowser_approval)


@router.post("/type", response_model=BrowserResponse)
async def type_text(req: TypeRequest, x_xbrowser_approval: str | None = Header(default=None)):
    return await execute(session, "type", req, approval_token=x_xbrowser_approval)


@router.post("/screenshot", response_model=BrowserResponse)
async def browser_screenshot(req: ScreenshotRequest):
    return await execute(session, "screenshot", req)


@router.post("/scroll", response_model=BrowserResponse)
async def scroll(req: ScrollRequest):
    return await execute(session, "scroll", req)


@router.post("/press", response_model=BrowserResponse)
async def press(req: PressRequest, x_xbrowser_approval: str | None = Header(default=None)):
    return await execute(session, "press", req, approval_token=x_xbrowser_approval)


@router.post("/wait", response_model=BrowserResponse)
async def wait(req: WaitRequest):
    return await execute(session, "wait", req)


@router.post("/run", response_model=BrowserResponse)
async def run(req: RunRequest, connection: Connection):
    return await macros.run_connected(session, req, connection)


@router.post("/authorize", response_model=BrowserResponse)
async def authorize(req: ApprovalRequest, x_xbrowser_admin: str = Header(default="")):
    secret = os.environ.get("XBROWSER_ADMIN_TOKEN", "")
    if not secret or not secrets.compare_digest(secret, x_xbrowser_admin):
        raise HTTPException(status_code=403, detail="Trusted approval control is disabled or unauthorized")
    try:
        step = TypeAdapter(BrowserStep).validate_python({"tool": req.tool, "args": req.args})
        async with session.operation():
            token = await session.approvals.issue(session, step.tool, step.args)
            result = await session.describe("authorize")
            result.data = {"approval_token": token, "expires_in_seconds": 120}
            return result
    except (BrowserError, PlaywrightError, ValueError) as exc:
        return failure("authorize", exc)


@router.post("/verify", response_model=BrowserResponse)
async def verify(req: VerificationRequest):
    try:
        session.policy.check("observe")
        return await verify_task(session, req)
    except (BrowserError, PlaywrightError, OSError, ValueError) as exc:
        return failure("verify", exc)


@router.post("/image", response_model=BrowserResponse)
async def image(req: ImageRequest):
    try:
        session.policy.check("screenshot")
        return BrowserResponse(success=True, action="image", data=image_data(session, req.path))
    except (BrowserError, OSError, ValueError) as exc:
        return failure("image", exc)
