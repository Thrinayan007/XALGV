"""Permission tier system.

Every tool declares one of five tiers.  The agent's current permission level
gates which tools it can invoke.  Tier 4 (``DESTRUCTIVE``) *always* requires
explicit human approval at runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


class PermissionTier(IntEnum):
    """Ordered permission levels — higher is more dangerous."""

    READ_ONLY = 0        # read files, list dirs, read env, screenshots
    WORKSPACE_WRITE = 1  # write/delete inside the workspace directory
    NETWORK = 2          # outbound HTTP (search, fetch pages)
    SYSTEM = 3           # install packages, change config, spawn processes
    DESTRUCTIVE = 4      # rm -rf, drop tables, irreversible host actions


# Human-readable descriptions for display / audit.
TIER_LABELS: dict[PermissionTier, str] = {
    PermissionTier.READ_ONLY: "Read-only access (files, env, screenshots)",
    PermissionTier.WORKSPACE_WRITE: "Write inside the agent workspace",
    PermissionTier.NETWORK: "Outbound network access (search, fetch)",
    PermissionTier.SYSTEM: "System-level changes (install, config)",
    PermissionTier.DESTRUCTIVE: "Destructive / irreversible actions",
}


@dataclass
class PermissionPolicy:
    """Controls which tiers the agent may use automatically vs. with approval."""

    auto_approve_up_to: PermissionTier = PermissionTier.WORKSPACE_WRITE
    require_approval: set[PermissionTier] | None = None  # None → only DESTRUCTIVE

    def __post_init__(self) -> None:
        if self.require_approval is None:
            self.require_approval = {PermissionTier.DESTRUCTIVE}

    def is_allowed(self, tier: PermissionTier, human_approved: bool = False) -> bool:
        """Return *True* if the given tier is permitted right now."""
        if tier in self.require_approval:
            return human_approved
        return tier <= self.auto_approve_up_to

    def needs_approval(self, tier: PermissionTier) -> bool:
        """Return *True* if invoking a tool at this tier requires human sign-off."""
        return tier in self.require_approval or tier > self.auto_approve_up_to


class PermissionDeniedError(PermissionError):
    """Raised when a tool invocation is blocked by the permission policy."""

    def __init__(self, tool_name: str, tier: PermissionTier) -> None:
        self.tool_name = tool_name
        self.tier = tier
        super().__init__(
            f"Tool '{tool_name}' requires {TIER_LABELS.get(tier, tier.name)} "
            f"permission — human approval needed."
        )
