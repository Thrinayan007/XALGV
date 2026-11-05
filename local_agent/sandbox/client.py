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

    async def write_file(self, path: str, content: str) -> ToolResult:
        """Write a file in the sandbox."""
        return await self._post("/tools/write_file", {
            "path": path,
            "content": content,
        })

    async def list_files(self, path: str = "/workspace") -> ToolResult:
        """List files in a directory in the sandbox."""
        return await self._post("/tools/list_files", {"path": path})

    # ── Screenshot (for later phases) ────────────────────────────────────

    async def screenshot(self) -> ToolResult:
        """Take a screenshot of the sandbox desktop."""
        return await self._post("/tools/screenshot", {})

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
