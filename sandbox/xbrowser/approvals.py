"""Single-use approvals issued only through a trusted, authenticated control path."""

import hashlib
import json
import secrets
import time

from .errors import BrowserError
from .state import capture_state


def request_digest(action, request):
    return hashlib.sha256(json.dumps([action, request.model_dump()], sort_keys=True).encode()).hexdigest()


class ApprovalStore:
    def __init__(self):
        self._grants = {}

    async def scope(self, session):
        return [session.session_id, session.generation, session.tabs.active,
                (await capture_state(session.tabs.page()))["fingerprint"]]

    async def issue(self, session, action, request):
        now = time.monotonic()
        self._grants = {key: value for key, value in self._grants.items() if value[0] > now}
        if len(self._grants) >= 100:
            raise BrowserError("APPROVAL_LIMIT", "Too many outstanding approvals")
        token = secrets.token_urlsafe(32)
        self._grants[token] = (now + 120, await self.scope(session), request_digest(action, request))
        return token

    async def consume(self, token, session, action, request):
        grant = self._grants.pop(token, None)
        if not grant or grant[0] < time.monotonic() or grant[1] != await self.scope(session) or grant[2] != request_digest(action, request):
            raise BrowserError("APPROVAL_INVALID", "Approval is expired, consumed, or does not match the reviewed action/state")


SENSITIVE = """el => {
    const form = el?.form || el?.closest?.('form');
    const roots = [el, form].filter(Boolean);
    const selector = 'input[type=password],input[type=file],[autocomplete^="cc-"],[autocomplete="transaction-amount"],[data-xbrowser-sensitive]';
    return roots.some(root => root.matches?.(selector) || root.querySelector?.(selector)) ||
        /^(pay|purchase|buy|place order|donate|delete account)\b/i.test((el?.innerText || el?.getAttribute?.('aria-label') || '').trim());
}"""


async def guard_target(session, target):
    if await target.evaluate("el => el.matches('input[type=file]')"):
        raise BrowserError("UPLOAD_DISABLED", "File uploads are disabled")
    if await target.evaluate(SENSITIVE) and not session._approved:
        raise BrowserError("HUMAN_APPROVAL_REQUIRED", "This credential, payment, upload or sensitive form action requires a reviewed approval")


async def guard_key(session, page, key):
    modifiers = key.split("+")
    base = modifiers[-1]
    document_keys = {"Enter", "Tab", "Escape", "Space", "Backspace", "Delete", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Home", "End", "PageUp", "PageDown", "NumpadEnter"}
    safe = (len(modifiers) == 1 and (len(base) == 1 or base in document_keys)) or (
        modifiers[:-1] == ["Shift"] and (len(base) == 1 or base in document_keys)
    ) or (modifiers[:-1] in (["Control"], ["Meta"]) and base.lower() in {"a", "c", "x", "v", "f", "backspace", "delete", "home", "end", "enter"})
    if not safe:
        raise BrowserError("KEY_DENIED", "Browser chrome, developer tools, file dialogs and system shortcuts are disabled")
    for frame in page.frames:
        if not session._approved and key not in {"Escape", "Tab", "Shift+Tab"} and await frame.evaluate(f"() => ({SENSITIVE})(document.activeElement)"):
            raise BrowserError("HUMAN_APPROVAL_REQUIRED", "Keyboard interaction with a sensitive form requires a reviewed approval")
