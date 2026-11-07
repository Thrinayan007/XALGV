
"""Sandbox client — communicates with the FastAPI tool server inside Docker.

The sandbox client is the *only* way the agent executes actions.  All
tool implementations delegate to this client, which in turn calls
the tool server running inside the Docker container.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from local_agent.tools.base import ToolResult

logger = logging.getLogger(__name__)


class SandboxClient:
    """HTTP client for the sandbox tool server."""

    def __init__(self, base_url: str = "http://localhost:9000") -> None:
        self._base_url = base_url.rstrip("/")
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
        })

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
    ) -> ToolResult:
        """Click an element on the page."""
        payload: dict[str, Any] = {}
        if selector:
            payload["selector"] = selector
        if text:
            payload["text"] = text
        if role:
            payload["role"] = role
        return await self._post("/tools/browser/click", payload)

    async def browser_type(
        self,
        selector: str,
        text: str,
        clear_first: bool = True,
        press_enter: bool = False,
    ) -> ToolResult:
        """Type text into an input element."""
        return await self._post("/tools/browser/type", {
            "selector": selector,
            "text": text,
            "clear_first": clear_first,
            "press_enter": press_enter,
        })

    async def browser_screenshot(
        self, full_page: bool = False, selector: str | None = None
    ) -> ToolResult:
        """Take a screenshot of the browser page."""
        payload: dict[str, Any] = {"full_page": full_page}
        if selector:
            payload["selector"] = selector
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

    async def _post(self, endpoint: str, payload: dict[str, Any]) -> ToolResult:
        """POST to the sandbox tool server and parse the response."""
        try:
            resp = await self._client.post(endpoint, json=payload)
            resp.raise_for_status()
            data = resp.json()
            return ToolResult(
                success=data.get("success", False),
                output=data.get("output", ""),
                error=data.get("error"),
                exit_code=data.get("exit_code"),
                artifacts=data.get("artifacts", []),
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

