"""Base classes and data types for the tool system.

Every tool the agent can invoke inherits from :class:`Tool` and provides:
* A JSON-schema description of its parameters.
* An ``execute`` method that performs the action.
* An optional ``verify`` method that confirms the action succeeded.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from local_agent.security.permissions import PermissionTier


# ── Result types ─────────────────────────────────────────────────────────

class VerificationStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class ToolResult:
    """The outcome of a single tool execution."""

    success: bool
    output: str
    error: str | None = None
    exit_code: int | None = None
    artifacts: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def truncated(self, max_chars: int = 4000) -> str:
        """Return output truncated to *max_chars* for context management."""
        if len(self.output) <= max_chars:
            return self.output
        half = max_chars // 2
        return self.output[:half] + f"\n\n… [{len(self.output) - max_chars} chars truncated] …\n\n" + self.output[-half:]


@dataclass
class VerificationResult:
    """The outcome of verifying a tool action."""

    status: VerificationStatus
    message: str
    evidence: dict[str, Any] | None = None


# ── Tool base class ──────────────────────────────────────────────────────

class Tool(ABC):
    """Abstract base for every tool the agent can invoke."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique machine-readable name, e.g. ``shell_exec``."""

    @property
    @abstractmethod
    def description(self) -> str:
        """One-line description shown to the model."""

    @property
    @abstractmethod
    def permission_tier(self) -> PermissionTier:
        """The minimum permission tier needed to invoke this tool."""

    @property
    @abstractmethod
    def parameters_schema(self) -> dict[str, Any]:
        """JSON Schema (``object`` type) for the tool's parameters."""

    @abstractmethod
    async def execute(self, params: dict[str, Any]) -> ToolResult:
        """Execute the tool action and return the result."""

    async def verify(self, params: dict[str, Any], result: ToolResult) -> VerificationResult:
        """Verify that the tool action achieved the intended effect.

        The default implementation skips verification.  Tools should override
        this to provide external checks (e.g. file exists, exit code is 0).
        """
        return VerificationResult(
            status=VerificationStatus.SKIPPED,
            message="No verifier defined for this tool.",
        )

    def to_openai_tool(self) -> dict[str, Any]:
        """Serialise to the OpenAI-style tool definition expected by Ollama."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters_schema,
            },
        }
