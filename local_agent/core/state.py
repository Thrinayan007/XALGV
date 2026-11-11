"""Agent state — the mutable state object that flows through the control loop."""

from __future__ import annotations

from dataclasses import dataclass, field

from local_agent.memory.store import RunStatus, StepRecord


@dataclass
class AgentState:
    """Mutable state for a single agent run.

    This object is the central truth during execution.  It is updated
    after every step and used to build the checkpoint.
    """

    run_id: str
    goal: str
    status: RunStatus = RunStatus.PLANNING
    plan: list[str] = field(default_factory=list)
    current_step: int = 0
    history: list[StepRecord] = field(default_factory=list)
    scratchpad: str = ""
    last_error: str | None = None
    retry_count: int = 0
    successful_actions_current_step: int = 0

    # ── Helpers ──────────────────────────────────────────────────────────

    def advance(self) -> None:
        """Move to the next step."""
        self.current_step += 1
        self.retry_count = 0
        self.last_error = None
        self.successful_actions_current_step = 0

    def record(self, step: StepRecord) -> None:
        """Append a step to the history."""
        self.history.append(step)

    def mark_retry(self, error: str) -> None:
        self.retry_count += 1
        self.last_error = error

    @property
    def is_terminal(self) -> bool:
        return self.status in (
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.KILLED,
        )

    @property
    def current_plan_step(self) -> str | None:
        if 0 <= self.current_step < len(self.plan):
            return self.plan[self.current_step]
        return None

    def summary_for_model(self) -> str:
        """Return a compact text summary for inclusion in context."""
        lines = [
            f"Goal: {self.goal}",
            f"Status: {self.status.value}",
            f"Step: {self.current_step + 1}/{len(self.plan) if self.plan else '?'}",
        ]
        if self.current_plan_step:
            lines.append(f"Current task: {self.current_plan_step}")
        if self.last_error:
            lines.append(f"Last error: {self.last_error}")
        if self.scratchpad:
            lines.append(f"Notes: {self.scratchpad}")
        # Include recent history (last 3 steps).
        recent = self.history[-3:] if self.history else []
        if recent:
            lines.append("Recent actions:")
            for s in recent:
                status_icon = "✓" if s.result_success else "✗" if s.result_success is False else "?"
                lines.append(f"  {status_icon} [{s.action_type}] {s.tool_name or ''}: {s.decision[:80]}")
        return "\n".join(lines)
