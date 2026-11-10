"""Single-use approvals issued only through a trusted, authenticated control path."""

import asyncio
import hashlib
import json
import secrets
import time
from dataclasses import dataclass, field

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

from .errors import BrowserError
from .schemas import NetworkWrite
from .state import capture_state


def request_digest(action, request):
    return hashlib.sha256(json.dumps([action, request.model_dump()], sort_keys=True).encode()).hexdigest()


@dataclass
class ActionApproval:
    page: Page
    network_write: NetworkWrite | None = None
    write_finished: asyncio.Event = field(default_factory=asyncio.Event)
    write_started: bool = False
    armed: bool = False
    write_error: BrowserError | None = None

    def take_write(self, request):
        write = self.network_write
        if (not self.armed or write is None or request.method != write.method or request.url != write.url or
                request.headers.get("content-type") != "application/x-www-form-urlencoded" or
                not request.is_navigation_request()):
            return False
        try:
            if request.frame is not self.page.main_frame or request.redirected_from:
                return False
        except PlaywrightError:
            return False
        body = request.post_data_buffer or b""
        if len(body) > 65536 or hashlib.sha256(body).hexdigest() != write.body_sha256:
            return False
        self.network_write = None
        self.write_started = True
        return True


class ApprovalStore:
    def __init__(self):
        self._grants = {}

    async def scope(self, session):
        page = session.tabs.page()
        focus = await page.evaluate("""() => {
            let el = document.activeElement;
            while (el?.shadowRoot?.activeElement) el = el.shadowRoot.activeElement;
            return el && [el.tagName, el.id, el.getAttribute('name'), el.getAttribute('src')];
        }""")
        return [session.session_id, session.generation, session.tabs.active,
                (await capture_state(page))["fingerprint"], focus]

    async def issue(self, session, action, request, network_write=None):
        if network_write is not None:
            await session.policy.resolve(network_write.url)
        now = time.monotonic()
        self._grants = {key: value for key, value in self._grants.items() if value[0] > now}
        if len(self._grants) >= 100:
            raise BrowserError("APPROVAL_LIMIT", "Too many outstanding approvals")
        token = secrets.token_urlsafe(32)
        self._grants[token] = (now + 120, await self.scope(session), request_digest(action, request), network_write)
        return token

    async def consume(self, token, session, action, request):
        grant = self._grants.pop(token, None)
        if not grant or grant[0] < time.monotonic() or grant[1] != await self.scope(session) or grant[2] != request_digest(action, request):
            raise BrowserError("APPROVAL_INVALID", "Approval is expired, consumed, or does not match the reviewed action/state")
        return ActionApproval(session.tabs.page(), grant[3])


SENSITIVE = r"""el => {
    const control = el?.closest?.('button,a,[role=button],[role=link],[role=menuitem]') || el;
    const form = control?.form || el?.closest?.('form');
    const roots = [control, form].filter(Boolean);
    const selector = 'input[type=password],input[type=file],[autocomplete^="cc-"],[autocomplete="transaction-amount"],[data-xbrowser-sensitive]';
    return roots.some(root => root.matches?.(selector) || root.querySelector?.(selector)) ||
        /^(pay|purchase|buy|place order|donate|delete|transfer funds|send money)\b/i.test((control?.getAttribute?.('aria-label') || control?.innerText || '').trim());
}"""

SUBMITS_FORM = """el => {
    const control = el.closest('button,input');
    return !!control?.form && ['submit','image'].includes(control.type);
}"""


async def guard_target(session, target, *, submits=False, click=False):
    if await target.evaluate("el => el.matches('input[type=file]')"):
        raise BrowserError("UPLOAD_DISABLED", "File uploads are disabled")
    if session._approval is None and (
        session.policy.review_interactions or await target.evaluate(SENSITIVE)
        or (click and await target.evaluate(SUBMITS_FORM))
        or (submits and await target.evaluate("el => !!(el.form || el.closest('form'))"))
    ):
        raise BrowserError("HUMAN_APPROVAL_REQUIRED", "This interaction or form submission requires a reviewed approval")


async def guard_key(session, page, key):
    modifiers = key.split("+")
    base = modifiers[-1]
    document_keys = {"Enter", "Tab", "Escape", "Space", "Backspace", "Delete", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Home", "End", "PageUp", "PageDown", "NumpadEnter"}
    safe = (len(modifiers) == 1 and (len(base) == 1 or base in document_keys)) or (
        modifiers[:-1] == ["Shift"] and (len(base) == 1 or base in document_keys)
    ) or (modifiers[:-1] in (["Control"], ["Meta"]) and base.lower() in {"a", "c", "x", "v", "f", "backspace", "delete", "home", "end", "enter"})
    if not safe:
        raise BrowserError("KEY_DENIED", "Browser chrome, developer tools, file dialogs and system shortcuts are disabled")
    if session._approval is not None or key in {"Escape", "Tab", "Shift+Tab"}:
        return
    if session.policy.review_interactions:
        raise BrowserError("HUMAN_APPROVAL_REQUIRED", "Keyboard interaction requires a reviewed approval")
    for frame in page.frames:
        if await frame.evaluate(
            f"enter => {{ const el = document.activeElement; return ({SENSITIVE})(el) || "
            f"({SUBMITS_FORM})(el) || (enter && !!(el.form || el.closest('form'))); }}",
            base in {"Enter", "NumpadEnter"},
        ):
            raise BrowserError("HUMAN_APPROVAL_REQUIRED", "Keyboard interaction with a sensitive form requires a reviewed approval")
