"""One explicitly scoped persistent context, serialized at the operation boundary.

The sandbox owns this object. Never share its profile with another process.
No mutating browser action is automatically replayed after a failure.
"""

from __future__ import annotations

import asyncio
import os
import re
import uuid
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError

from .approvals import ApprovalStore
from .errors import BrowserError
from .marks import ReferenceRegistry
from .proxy import EgressProxy
from .safety import BrowserPolicy
from .schemas import BrowserResponse
from .tabs import TabRegistry


class BrowserSession:
    def __init__(self, workspace: Path | str = "/workspace", *, scope: str = "default", headless: bool = False):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", scope):
            raise ValueError("scope must contain 1–64 letters, digits, underscores or hyphens")
        self.workspace = Path(workspace).resolve()
        self.profile = self.workspace / ".xbrowser" / scope / "profile"
        self.headless = headless
        self.session_id = uuid.uuid4().hex
        self.generation = 0
        self.tabs = TabRegistry()
        self.references = ReferenceRegistry()
        self.policy = BrowserPolicy()
        self.approvals = ApprovalStore()
        self._approved = False
        self._proxy = None
        self._navigation_error = None
        self.max_tabs = 12
        self.events = deque(maxlen=20)
        self._lock = asyncio.Lock()
        self._playwright = None
        self._context = None
        self._dead = False
        self._restarted = False
        self._profile_lock = None

    def _claim_profile(self):
        if os.name != "posix":
            return
        import fcntl
        owner = (self.profile.parent / "controller.lock").open("a")
        try:
            fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            owner.close()
            raise BrowserError("PROFILE_IN_USE", "Another controller owns this browser scope") from exc
        self._profile_lock = owner
        wanted = os.fsencode(f"--user-data-dir={self.profile}")
        for process in Path("/proc").iterdir():
            if not process.name.isdigit():
                continue
            try:
                if wanted in (process / "cmdline").read_bytes().split(b"\0"):
                    raise BrowserError("PROFILE_IN_USE", "A running Chromium process owns this profile")
            except (OSError, PermissionError):
                continue
        # Only Chromium's ephemeral singleton symlinks are recovered. Cookies,
        # history and all other persistent profile data remain intact.
        for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
            link = self.profile / name
            if link.is_symlink():
                link.unlink()

    @property
    def running(self):
        return self._context is not None and not self._dead

    async def _start(self):
        if self.running:
            return
        recovering = self._context is not None
        await self._stop()
        from playwright.async_api import async_playwright

        if not self.profile.resolve().is_relative_to(self.workspace):
            raise BrowserError("INVALID_PROFILE", "Browser profile must remain inside the workspace")
        self.profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self._claim_profile()
            self._playwright = await async_playwright().start()
            self._proxy = EgressProxy(self.policy, self.events)
            proxy_url = await self._proxy.start()
            self._context = await self._playwright.chromium.launch_persistent_context(
                str(self.profile), headless=self.headless,
                viewport={"width": 1280, "height": 720},
                accept_downloads=False,
                service_workers="block",
                proxy={"server": proxy_url, "bypass": "<-loopback>"},
                args=["--disable-dev-shm-usage", "--disable-quic", "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"],
            )
            await self._context.route("**/*", self._guard_request)
            await self._context.add_init_script("""for (const name of ['RTCPeerConnection','webkitRTCPeerConnection']) {
                Object.defineProperty(globalThis,name,{value:undefined,writable:false,configurable:false});
            }""")
            await self._context.route_web_socket("**/*", lambda socket: socket.close(code=1008, reason="WebSockets disabled by browser policy"))
            self.generation += 1
            self._dead = False
            self._restarted = recovering
            self._context.on("close", self._on_closed)
            self._context.on("page", self._register_page)
            for page in self._context.pages:
                self._register_page(page)
            if not self._context.pages:
                await self._context.new_page()
        except BaseException:
            await self._stop()
            raise

    async def _guard_request(self, route):
        request = route.request
        try:
            await self.policy.resolve(request.url)
            if request.method not in {"GET", "HEAD", "OPTIONS"} and not self._approved:
                raise BrowserError("HUMAN_APPROVAL_REQUIRED", "Network writes require a reviewed action approval")
            await route.continue_()
        except BrowserError as exc:
            self.events.append({"type": "request_denied", "code": exc.code})
            if request.is_navigation_request():
                self._navigation_error = exc
            await route.abort("blockedbyclient")

    def _on_closed(self, *_):
        self._dead = True
        self.tabs.clear()

    def _register_page(self, page):
        tab_id = self.tabs.register(page)
        if self.tabs.count > self.max_tabs:
            self.events.append({"type": "tab_limit", "limit": self.max_tabs})
            asyncio.create_task(page.close())
        page.on("framenavigated", lambda *_: self.references.invalidate(tab_id))
        page.on("close", lambda *_: self.references.invalidate(tab_id))
        # Dismiss prompts and confirmations: never approve a site's proposed action.
        page.on("dialog", self._dialog)
        # Interception suppresses native file dialogs; no tool is allowed to set files.
        page.on("filechooser", lambda *_: self.events.append({"type": "upload_blocked"}))
        page.on("pageerror", lambda error: self.events.append({"type": "pageerror", "message": str(error)[:500]}))
        page.on("console", self._console)
        page.on("requestfailed", lambda request: self.events.append({
            "type": "requestfailed", "url": request.url[:500], "message": request.failure,
        }))
        page.on("crash", self._on_crash)

    def _console(self, message):
        if message.type == "error":
            self.events.append({"type": "console", "message": message.text[:500]})

    def _on_crash(self, *_):
        self.events.append({"type": "crash", "generation": self.generation})
        self._dead = True  # Restart before the next action; never replay the interrupted action.

    async def _dialog(self, dialog):
        self.events.append({"type": "dialog", "kind": dialog.type, "message": dialog.message[:500]})
        await dialog.dismiss()

    async def _stop(self):
        await self.references.clear()
        context, playwright = self._context, self._playwright
        self._context = self._playwright = None
        try:
            if context is not None:
                await context.close()
        finally:
            try:
                if playwright is not None:
                    await playwright.stop()
            finally:
                self.tabs.clear()
                self._dead = False
                try:
                    if self._proxy:
                        await self._proxy.stop()
                        self._proxy = None
                finally:
                    if self._profile_lock:
                        self._profile_lock.close()
                        self._profile_lock = None

    @asynccontextmanager
    async def operation(self):
        """Legacy and foundation actions share this exact lock and active page."""
        async with self._lock:
            self._restarted = False
            self._navigation_error = None
            self._approved = False
            await self._start()
            if self.tabs.active is None:
                await self._context.new_page()
            try:
                yield self.tabs.page()
            finally:
                self._approved = False

    @staticmethod
    def validate_url(url: str):
        parsed = urlsplit(url)
        if url == "about:blank":
            return
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise BrowserError("INVALID_URL", "Only HTTP(S) URLs without embedded credentials and about:blank are allowed")

    def artifact_path(self, path: str) -> Path:
        target = Path(path)
        if not target.is_absolute():
            target = self.workspace / target
        target = target.resolve()
        if (not target.is_relative_to(self.workspace) or target.is_relative_to((self.workspace / ".xbrowser").resolve()) or
                target.is_relative_to(self.profile.resolve())):
            raise BrowserError("INVALID_PATH", "Artifacts must stay inside the workspace and outside browser profiles")
        return target

    def screenshot_target(self, path: str | None) -> Path:
        target = self.artifact_path(path or str(Path("screenshots") / f"browser-{uuid.uuid4().hex}.png"))
        if target.suffix.lower() != ".png":
            raise BrowserError("INVALID_PATH", "Screenshots must use a .png extension")
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    async def capture(self, page, *, path=None, full_page=False, selector=None, marks=False, annotations=None) -> Path:
        target = self.screenshot_target(path)
        # Exclusive creation prevents accidental overwrite, including duplicate requests.
        try:
            with target.open("xb") as stream:
                try:
                    if selector:
                        content = await page.locator(selector).screenshot(timeout=5000, type="png")
                    else:
                        content = await page.screenshot(full_page=full_page, type="png", timeout=10000)
                    if marks:
                        import io

                        from PIL import Image, ImageDraw
                        picture = Image.open(io.BytesIO(content)).convert("RGB")
                        draw = ImageDraw.Draw(picture)
                        offset = await page.evaluate("() => [scrollX,scrollY]") if full_page else [0, 0]
                        for item in annotations or []:
                            box = item["bbox"]
                            x, y = box["x"] + offset[0], box["y"] + offset[1]
                            draw.rectangle((x, y, x + box["width"], y + box["height"]), outline="#ffb000", width=2)
                            label = str(item["ref"])
                            draw.rectangle((x, y, x + 8 * len(label) + 5, y + 15), fill="#ffb000")
                            draw.text((x + 2, y + 1), label, fill="black")
                        picture.save(stream, format="PNG")
                    else:
                        stream.write(content)
                except BaseException:
                    stream.close()
                    target.unlink(missing_ok=True)
                    raise
        except FileExistsError as exc:
            raise BrowserError("ARTIFACT_EXISTS", f"Screenshot already exists: {target}") from exc
        return target

    async def describe(self, action: str, *, artifacts=None, evidence=None) -> BrowserResponse:
        tabs = await self.tabs.snapshot()
        data = {"running": self.running, "tabs": [tab.model_dump() for tab in tabs], "events": list(self.events)}
        output = "\n".join(f"{tab.tab_id}{'*' if tab.active else ''}: {tab.title} | {tab.url}" for tab in tabs)
        return BrowserResponse(
            success=True, action=action, output=output, session_id=self.session_id,
            generation=self.generation, tab_id=self.tabs.active, restarted=self._restarted,
            data=data, artifacts=artifacts or [], action_verification=evidence or {},
        )

    async def control(self, action: str) -> BrowserResponse:
        async with self._lock:
            self._restarted = False
            if action == "stop":
                await self._stop()
            elif action == "restart":
                await self._stop()
                await self._start()
                self._restarted = True
            elif action == "start":
                await self._start()
            elif action != "health":
                raise BrowserError("INVALID_ACTION", f"Unknown session action: {action}")
            return await self.describe(f"session_{action}", evidence={"running": self.running})

    async def open(self, url: str, *, new_tab=False, wait_for="load"):
        self.policy.check("open")
        self.validate_url(url)
        async with self.operation() as page:
            return await self._open(page, url, new_tab=new_tab, wait_for=wait_for)

    async def _open(self, page, url: str, *, new_tab=False, wait_for="load"):
        self.validate_url(url)
        if url != "about:blank":
            await self.policy.resolve(url)
        if new_tab:
            if self.tabs.count >= self.max_tabs:
                raise BrowserError("TAB_LIMIT", "Browser tab limit reached")
            page = await self._context.new_page()
        try:
            response = await page.goto(url, wait_until=wait_for, timeout=30000)
        except PlaywrightError:
            if self._navigation_error:
                raise self._navigation_error from None
            raise
        if page.url != "about:blank":
            await self.policy.resolve(page.url)
        result = await self.describe("open", evidence={
            "requested_url": url, "final_url": page.url,
            "status": response.status if response else None,
            "host_matches": urlsplit(url).hostname == urlsplit(page.url).hostname,
        })
        if response and response.status >= 400:
            result.success = False
            result.error_code = "HTTP_ERROR"
            result.error = f"Navigation returned HTTP {response.status}"
        return result

    async def tab_action(self, action: str, *, tab=None, url=None):
        self.policy.check("tabs")
        async with self.operation() as page:
            return await self._tab_action(page, action, tab=tab, url=url)

    async def _tab_action(self, page, action: str, *, tab=None, url=None):
        if url is not None:
            self.validate_url(url)
        if action == "new":
            if self.tabs.count >= self.max_tabs:
                raise BrowserError("TAB_LIMIT", "Browser tab limit reached")
            page = await self._context.new_page()
            if url:
                await self._open(page, url)
        elif action == "switch":
            page = self.tabs.page(tab)
            await page.bring_to_front()
            self.tabs.active = tab
        elif action == "close":
            await self.tabs.page(tab).close()
            if self.tabs.active is not None:
                await self.tabs.page().bring_to_front()
        elif action in {"back", "forward", "reload"}:
            if tab is not None:
                page = self.tabs.page(tab)
                await page.bring_to_front()
                self.tabs.active = tab
            method = {"back": page.go_back, "forward": page.go_forward, "reload": page.reload}[action]
            await method(timeout=30000)
        elif action != "list":
            raise BrowserError("INVALID_ACTION", f"Unknown tab action: {action}")
        return await self.describe(f"tabs_{action}", evidence={"active_tab": self.tabs.active})
