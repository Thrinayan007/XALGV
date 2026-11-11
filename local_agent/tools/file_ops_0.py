"""File operation tools — read, write, list, and delete files in the sandbox."""

from __future__ import annotations

from typing import Any

from local_agent.security.permissions import PermissionTier
from local_agent.tools.base import Tool, ToolResult, VerificationResult, VerificationStatus


class ReadFileTool(Tool):
    """Read the contents of a file in the sandbox workspace."""

    def __init__(self, sandbox_client: Any = None) -> None:
        self._sandbox = sandbox_client

    @property
    def name(self) -> str:
        return "read_file"

    @property
    def description(self) -> str:
        return "Read the contents of a file.  Returns the text content or an error if the file does not exist."

    @property
    def permission_tier(self) -> PermissionTier:
        return PermissionTier.READ_ONLY

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute or workspace-relative path to read.",
                },
                "max_lines": {
                    "type": "integer",
                    "description": "Maximum number of lines to return (default: 200).",
                    "default": 200,
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        }

    async def execute(self, params: dict[str, Any]) -> ToolResult:
        if self._sandbox is None:
            return ToolResult(success=False, output="", error="Sandbox not configured.")
        return await self._sandbox.read_file(params["path"], max_lines=params.get("max_lines", 200))

    async def verify(self, params: dict[str, Any], result: ToolResult) -> VerificationResult:
        if result.success:
            return VerificationResult(
                status=VerificationStatus.PASSED,
                message="File read successfully.",
                evidence={"length": len(result.output)},
            )
        return VerificationResult(
            status=VerificationStatus.FAILED,
            message=f"File read failed: {result.error}",
        )


class WriteFileTool(Tool):
    """Write or overwrite a file in the sandbox workspace."""

    def __init__(self, sandbox_client: Any = None) -> None:
        self._sandbox = sandbox_client

    @property
    def name(self) -> str:
        return "write_file"

    @property
    def description(self) -> str:
        return "Write content to a file.  Creates parent directories if needed.  Overwrites existing files."

    @property
    def permission_tier(self) -> PermissionTier:
        return PermissionTier.WORKSPACE_WRITE

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute or workspace-relative path to write.",
                },
                "content": {
                    "type": "string",
                    "description": "The full text content to write.",
                },
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        }

    async def execute(self, params: dict[str, Any]) -> ToolResult:
        if self._sandbox is None:
            return ToolResult(success=False, output="", error="Sandbox not configured.")
        return await self._sandbox.write_file(params["path"], params["content"])

    async def verify(self, params: dict[str, Any], result: ToolResult) -> VerificationResult:
        # Verify by reading back and checking the file exists.
        if not result.success:
            return VerificationResult(status=VerificationStatus.FAILED, message=f"Write failed: {result.error}")
        if self._sandbox is None:
            return VerificationResult(status=VerificationStatus.SKIPPED, message="Cannot verify without sandbox.")
        read_result = await self._sandbox.read_file(params["path"], max_lines=1)
        if read_result.success:
            return VerificationResult(
                status=VerificationStatus.PASSED,
                message="File exists after write.",
            )
        return VerificationResult(
            status=VerificationStatus.FAILED,
            message="File not found after write.",
        )


class ListFilesTool(Tool):
    """List files and directories in a path inside the sandbox."""

    def __init__(self, sandbox_client: Any = None) -> None:
        self._sandbox = sandbox_client

    @property
    def name(self) -> str:
        return "list_files"

    @property
    def description(self) -> str:
        return "List files and directories at a given path.  Returns names, types, and sizes."

    @property
    def permission_tier(self) -> PermissionTier:
        return PermissionTier.READ_ONLY

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory path to list (default: /workspace).",
                    "default": "/workspace",
                },
            },
            "required": [],
            "additionalProperties": False,
        }

    async def execute(self, params: dict[str, Any]) -> ToolResult:
        if self._sandbox is None:
            return ToolResult(success=False, output="", error="Sandbox not configured.")
        return await self._sandbox.list_files(params.get("path", "/workspace"))
