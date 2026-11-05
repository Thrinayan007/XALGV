"""Tool registry — central catalogue of available tools.

The registry:
* Holds all registered :class:`Tool` instances.
* Filters tools by permission tier so the model only sees what it may use.
* Serialises tool definitions into the OpenAI-compatible format that Ollama expects.
* Dispatches execution requests and runs post-action verification.
"""

from __future__ import annotations

import logging
from typing import Any

from local_agent.security.permissions import PermissionDeniedError, PermissionPolicy, PermissionTier
from local_agent.tools.base import Tool, ToolResult, VerificationResult

logger = logging.getLogger(__name__)


class ToolRegistry:
    """Stores and dispatches agent tools, respecting permission tiers."""

    def __init__(self, policy: PermissionPolicy | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        self._policy = policy or PermissionPolicy()

    # ── Registration ─────────────────────────────────────────────────────

    def register(self, tool: Tool) -> None:
        """Register a tool.  Raises if a tool with the same name exists."""
        if tool.name in self._tools:
            raise ValueError(f"Tool '{tool.name}' is already registered.")
        self._tools[tool.name] = tool
        logger.debug("Registered tool: %s (tier=%s)", tool.name, tool.permission_tier.name)

    def register_many(self, *tools: Tool) -> None:
        for tool in tools:
            self.register(tool)

    # ── Lookup ───────────────────────────────────────────────────────────

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def list_tools(self, *, max_tier: PermissionTier | None = None) -> list[Tool]:
        """Return tools up to *max_tier* (inclusive).  None → all tools."""
        if max_tier is None:
            return list(self._tools.values())
        return [t for t in self._tools.values() if t.permission_tier <= max_tier]

    def get_tool_definitions(self, *, max_tier: PermissionTier | None = None) -> list[dict[str, Any]]:
        """Return OpenAI-style tool definitions for tools the agent may see."""
        return [t.to_openai_tool() for t in self.list_tools(max_tier=max_tier)]

    @property
    def names(self) -> list[str]:
        return list(self._tools)

    # ── Execution ────────────────────────────────────────────────────────

    async def execute(
        self,
        name: str,
        params: dict[str, Any],
        *,
        human_approved: bool = False,
    ) -> ToolResult:
        """Execute a tool by name, checking permissions first."""
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(success=False, output="", error=f"Unknown tool: {name}")

        # Permission gate.
        if not self._policy.is_allowed(tool.permission_tier, human_approved):
            raise PermissionDeniedError(name, tool.permission_tier)

        try:
            result = await tool.execute(params)
        except Exception as exc:
            logger.exception("Tool %s raised an exception", name)
            result = ToolResult(success=False, output="", error=f"{type(exc).__name__}: {exc}")

        return result

    async def execute_and_verify(
        self,
        name: str,
        params: dict[str, Any],
        *,
        human_approved: bool = False,
    ) -> tuple[ToolResult, VerificationResult]:
        """Execute a tool and run its verifier."""
        result = await self.execute(name, params, human_approved=human_approved)
        tool = self._tools.get(name)
        if tool is None:
            from local_agent.tools.base import VerificationStatus
            verification = VerificationResult(
                status=VerificationStatus.SKIPPED,
                message="Tool not found; skipping verification.",
            )
        else:
            try:
                verification = await tool.verify(params, result)
            except Exception as exc:
                from local_agent.tools.base import VerificationStatus
                logger.exception("Verifier for %s raised an exception", name)
                verification = VerificationResult(
                    status=VerificationStatus.FAILED,
                    message=f"Verifier error: {exc}",
                )

        return result, verification
