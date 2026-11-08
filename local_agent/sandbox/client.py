
"""Sandbox client — communicates with the FastAPI tool server inside Docker.

The sandbox client is the *only* way the agent executes actions.  All
tool implementations delegate to this client, which in turn calls
the tool server running inside the Docker container.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Literal

import httpx

from local_agent.tools.base import ToolResult

logger = logging.getLogger(__name__)


class SandboxClient:
    """HTTP client for the sandbox tool server."""

    def __init__(self, base_url: str = "http://localhost:9000", *, admin_token: str | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._browser_approval_token = None
        self._admin_token = admin_token if admin_token is not None else os.environ.get("XALGV_SANDBOX_ADMIN_TOKEN")
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(connect=5, read=120, write=10, pool=5),
        )

    # ── Shell ────────────────────────────────────────────────────────────

    async def execute_shell(
        self,
        command: str,
        working_dir: str = "/workspace",
        timeout: int = 30,
    ) -> ToolResult:
        """Execute a shell command in the sandbox."""
        return await self._post("/tools/shell", {
            "command": command,
            "working_dir": working_dir,
            "timeout": timeout,
        }, headers={"X-Sandbox-Admin": self._admin_token} if self._admin_token else None)

    # ── Files ────────────────────────────────────────────────────────────

    async def read_file(self, path: str, *, max_lines: int = 200) -> ToolResult:
        """Read file contents from the sandbox."""
        return await self._post("/tools/read_file", {
            "path": path,
            "max_lines": max_lines,
        })

    async def write_file(self, path: str, content: str, append: bool = False) -> ToolResult:
        """Write (or append to) a file in the sandbox."""
        return await self._post("/tools/write_file", {
            "path": path,
            "content": content,
            "append": append,
        })

    async def list_files(self, path: str = "/workspace") -> ToolResult:
        """List files in a directory in the sandbox."""
        return await self._post("/tools/list_files", {"path": path})

    # ── Screenshot (for later phases) ────────────────────────────────────

    async def screenshot(self) -> ToolResult:
        """Take a screenshot of the sandbox desktop."""
        return await self._post("/tools/screenshot", {})

    # ── Browser automation ───────────────────────────────────────────────

    async def browser_verify(self, checks: list[dict[str, Any]]) -> ToolResult:
        """Trusted caller checks; deliberately absent from the model tool registry."""
        return await self._post("/tools/browser/verify", {"checks": checks})

    async def browser_image(self, path: str) -> ToolResult:
        return await self._post("/tools/browser/image", {"path": path})

    async def browser_authorize(self, tool: str, args: dict[str, Any], admin_token: str) -> ToolResult:
        """Call only after human review; neither this method nor its secret is model-exposed."""
        return await self._post("/tools/browser/authorize", {"tool": tool, "args": args},
                                headers={"X-XBrowser-Admin": admin_token})

    def use_browser_approval(self, token: str):
        self._browser_approval_token = token

    async def browser_press(self, key: str) -> ToolResult:
        return await self._post("/tools/browser/press", {"key": key})

    async def browser_scroll(
        self, direction: str = "down", pixels: int = 600, wait_ms: int = 100,
        *, selector: str | None = None, ref: int | None = None, snapshot_id: str | None = None,
    ) -> ToolResult:
        return await self._post("/tools/browser/scroll", {
            "direction": direction, "pixels": pixels, "wait_ms": wait_ms,
            **{key: value for key, value in {"selector": selector, "ref": ref, "snapshot_id": snapshot_id}.items()
               if value is not None},
        })

    async def browser_wait(
        self, condition: str, value: str | None = None, timeout_ms: int = 5000, milliseconds: int = 400,
    ) -> ToolResult:
        return await self._post("/tools/browser/wait", {
            "condition": condition, "value": value, "timeout_ms": timeout_ms, "milliseconds": milliseconds,
        })

    async def browser_run(self, steps: list[dict[str, Any]], timeout_ms: int = 60000, action_budget: int = 20) -> ToolResult:
        return await self._post("/tools/browser/run", {
            "steps": steps, "timeout_ms": timeout_ms, "action_budget": action_budget,
        })

    async def browser_observe(
        self, *, scope: Literal["viewport", "page"] = "viewport", text: bool = False,
        max_elements: int = 30, max_chars: int = 2400,
    ) -> ToolResult:
        return await self._post("/tools/browser/observe", {
            "scope": scope, "text": text, "max_elements": max_elements, "max_chars": max_chars,
        })

    async def browser_session(
        self, action: Literal["start", "stop", "restart", "health"] = "start",
    ) -> ToolResult:
        """Control the sandbox's selected persistent browser scope."""
        return await self._post("/tools/browser/session", {"action": action})

    async def browser_open(
        self, url: str, *, new_tab: bool = False,
        wait_for: Literal["load", "domcontentloaded", "networkidle", "commit"] = "load",
    ) -> ToolResult:
        return await self._post("/tools/browser/open", {
            "url": url, "new_tab": new_tab, "wait_for": wait_for,
        })

    async def browser_tabs(
        self, action: Literal["list", "new", "switch", "close", "back", "forward", "reload"] = "list",
        *, tab: str | None = None, url: str | None = None,
    ) -> ToolResult:
        payload: dict[str, Any] = {"action": action}
        if tab is not None:
            payload["tab"] = tab
        if url is not None:
            payload["url"] = url
        return await self._post("/tools/browser/tabs", payload)

    async def browser_navigate(self, url: str, wait_for: str = "load") -> ToolResult:
        """Navigate the browser to a URL."""
        return await self._post("/tools/browser/navigate", {
            "url": url,
            "wait_for": wait_for,
        })

    async def browser_get_content(
        self, mode: str = "a11y", selector: str | None = None
    ) -> ToolResult:
        """Get page content (accessibility tree, text, or HTML)."""
        payload: dict[str, Any] = {"mode": mode}
        if selector:
            payload["selector"] = selector
        return await self._post("/tools/browser/get_content", payload)

    async def browser_click(
        self,
        selector: str | None = None,
        text: str | None = None,
        role: str | None = None,
        *, ref: int | None = None, snapshot_id: str | None = None,
    ) -> ToolResult:
        """Click an element on the page."""
        payload: dict[str, Any] = {}
        if selector:
            payload["selector"] = selector
        if text:
            payload["text"] = text
        if role:
            payload["role"] = role
        if ref is not None:
            payload["ref"] = ref
        if snapshot_id is not None:
            payload["snapshot_id"] = snapshot_id
        return await self._post("/tools/browser/click", payload)

    async def browser_type(
        self,
        selector: str | None = None,
        text: str = "",
        clear_first: bool = True,
        press_enter: bool = False,
        *, ref: int | None = None, snapshot_id: str | None = None,
    ) -> ToolResult:
        """Type text into an input element."""
        payload: dict[str, Any] = {
            "text": text,
            "clear_first": clear_first,
            "press_enter": press_enter,
        }
        if selector is not None:
            payload["selector"] = selector
        if ref is not None:
            payload["ref"] = ref
        if snapshot_id is not None:
            payload["snapshot_id"] = snapshot_id
        return await self._post("/tools/browser/type", payload)

    async def browser_screenshot(
        self, full_page: bool = False, selector: str | None = None, *, path: str | None = None, marks: bool = False,
    ) -> ToolResult:
        """Take a screenshot of the browser page."""
        payload: dict[str, Any] = {"full_page": full_page}
        if marks:
            payload["marks"] = True
        if selector:
            payload["selector"] = selector
        if path is not None:
            payload["path"] = path
        return await self._post("/tools/browser/screenshot", payload)

    # ── Health ───────────────────────────────────────────────────────────

    async def health_check(self) -> bool:
        try:
            resp = await self._client.get("/health")
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    async def close(self) -> None:
        await self._client.aclose()

    # ── Internal ─────────────────────────────────────────────────────────

    async def _post(self, endpoint: str, payload: dict[str, Any], *, headers=None) -> ToolResult:
        """POST to the sandbox tool server and parse the response."""
        try:
            if self._browser_approval_token and endpoint in {"/tools/browser/click", "/tools/browser/type", "/tools/browser/press"}:
                headers = {**(headers or {}), "X-XBrowser-Approval": self._browser_approval_token}
                self._browser_approval_token = None
            resp = await self._client.post(endpoint, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            return ToolResult(
                success=data.get("success", False),
                output=data.get("output", ""),
                error=data.get("error"),
                exit_code=data.get("exit_code"),
                artifacts=data.get("artifacts", []),
                metadata={
                    **data.get("metadata", {}),
                    **{key: value for key, value in data.items() if key not in {
                        "success", "output", "error", "exit_code", "artifacts", "metadata",
                    }},
                },
            )
        except httpx.TimeoutException:
            return ToolResult(success=False, output="", error="Sandbox request timed out.")
        except httpx.HTTPStatusError as exc:
            return ToolResult(
                success=False, output="",
                error=f"Sandbox returned HTTP {exc.response.status_code}.",
            )
        except httpx.HTTPError as exc:
            return ToolResult(
                success=False, output="",
                error=f"Cannot reach sandbox: {exc}",
            )

