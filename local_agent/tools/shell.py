"""Shell execution tool — runs commands inside the sandbox."""

from __future__ import annotations

from typing import Any

from local_agent.security.permissions import PermissionTier
from local_agent.tools.base import Tool, ToolResult, VerificationResult, VerificationStatus


class ShellExecTool(Tool):
    """Execute a shell command inside the sandbox and return stdout/stderr."""

    def __init__(self, sandbox_client: Any = None) -> None:
        self._sandbox = sandbox_client

    @property
    def name(self) -> str:
        return "shell_exec"

    @property
    def description(self) -> str:
        return (
            "Execute a shell command in the sandbox Linux environment. "
            "Returns stdout, stderr, and exit code. Use for running programs, "
            "installing packages, git operations, and system inspection."
        )

    @property
    def permission_tier(self) -> PermissionTier:
        return PermissionTier.WORKSPACE_WRITE

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute.",
                },
                "working_dir": {
                    "type": "string",
                    "description": "Working directory (default: /workspace).",
                    "default": "/workspace",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default: 30).",
                    "default": 30,
                    "minimum": 1,
                    "maximum": 300,
                },
            },
            "required": ["command"],
            "additionalProperties": False,
        }

    async def execute(self, params: dict[str, Any]) -> ToolResult:
        command = params["command"]
        working_dir = params.get("working_dir", "/workspace")
        timeout = params.get("timeout", 30)

        if self._sandbox is None:
            return ToolResult(
                success=False,
                output="",
                error="Sandbox client not configured.  Cannot execute shell commands.",
            )

        return await self._sandbox.execute_shell(
            command=command,
            working_dir=working_dir,
            timeout=timeout,
        )

    async def verify(self, params: dict[str, Any], result: ToolResult) -> VerificationResult:
        if result.exit_code == 0:
            return VerificationResult(
                status=VerificationStatus.PASSED,
                message=f"Command exited with code 0.",
                evidence={"exit_code": result.exit_code},
            )
        return VerificationResult(
            status=VerificationStatus.FAILED,
            message=f"Command exited with code {result.exit_code}.",
            evidence={"exit_code": result.exit_code, "error": result.error},
        )
