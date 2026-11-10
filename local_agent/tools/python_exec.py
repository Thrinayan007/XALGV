"""Python execution tool — runs Python code inside the sandbox."""

from __future__ import annotations

import uuid
from typing import Any

from local_agent.security.permissions import PermissionTier
from local_agent.tools.base import Tool, ToolResult, VerificationResult, VerificationStatus


class PythonExecTool(Tool):
    """Execute a Python script inside the sandbox and return the output."""

    def __init__(self, sandbox_client: Any = None) -> None:
        self._sandbox = sandbox_client

    @property
    def name(self) -> str:
        return "python_exec"

    @property
    def description(self) -> str:
        return (
            "Execute Python code in the sandbox.  The code is written to a "
            "temporary file and run with ``python3``.  Use for data processing, "
            "calculations, file manipulation, and testing."
        )

    @property
    def permission_tier(self) -> PermissionTier:
        return PermissionTier.SYSTEM

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "The Python source code to execute.",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default: 30).",
                    "default": 30,
                    "minimum": 1,
                    "maximum": 120,
                },
            },
            "required": ["code"],
            "additionalProperties": False,
        }

    async def execute(self, params: dict[str, Any]) -> ToolResult:
        code = params["code"]
        timeout = params.get("timeout", 30)

        if self._sandbox is None:
            return ToolResult(success=False, output="", error="Sandbox not configured.")

        # Write code to a temp file and execute it.
        script_path = f"/workspace/.agent_scripts/{uuid.uuid4().hex}.py"
        write_result = await self._sandbox.write_file(script_path, code)
        if not write_result.success:
            return ToolResult(success=False, output="", error=f"Failed to write script: {write_result.error}")

        return await self._sandbox.execute_shell(
            command=f"python3 {script_path}",
            working_dir="/workspace",
            timeout=timeout,
        )

    async def verify(self, params: dict[str, Any], result: ToolResult) -> VerificationResult:
        if result.exit_code == 0:
            return VerificationResult(
                status=VerificationStatus.PASSED,
                message="Python script exited successfully.",
                evidence={"exit_code": 0},
            )
        return VerificationResult(
            status=VerificationStatus.FAILED,
            message=f"Python script failed with exit code {result.exit_code}.",
            evidence={"exit_code": result.exit_code, "error": result.error},
        )
