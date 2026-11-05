"""Budget enforcement — caps on steps, wall time, and repeated actions.

The budget tracker is injected into the agent loop and queried before
every action.  When a limit is exceeded the agent must stop or ask the
human for permission to continue.
"""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass, field


class BudgetExhaustedError(RuntimeError):
    """Raised when any budget limit is reached."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass
class BudgetTracker:
    """Tracks step count, wall time, and repeated-action frequency."""

    max_steps: int = 50
    max_wall_time_seconds: int = 600
    max_repeated_actions: int = 3

    # ── Internal state (mutable) ─────────────────────────────────────────
    steps: int = field(default=0, init=False)
    start_time: float = field(default=0.0, init=False)
    _action_counts: Counter = field(default_factory=Counter, init=False)

    def start(self) -> None:
        """Mark the beginning of a run."""
        self.steps = 0
        self.start_time = time.monotonic()
        self._action_counts.clear()

    def record_step(self, action_key: str) -> None:
        """Record that one step was taken.

        Parameters
        ----------
        action_key:
            A string that uniquely identifies the action (e.g.
            ``"shell_exec:ls -la"``).  Used to detect loops.
        """
        self.steps += 1
        self._action_counts[action_key] += 1

    # ── Checks ───────────────────────────────────────────────────────────

    def check(self, action_key: str | None = None) -> None:
        """Raise :class:`BudgetExhaustedError` if any limit is reached."""
        if self.steps >= self.max_steps:
            raise BudgetExhaustedError(f"Step limit reached ({self.max_steps} steps).")

        elapsed = time.monotonic() - self.start_time
        if elapsed >= self.max_wall_time_seconds:
            raise BudgetExhaustedError(
                f"Wall-time limit reached ({self.max_wall_time_seconds}s, "
                f"elapsed {elapsed:.0f}s)."
            )

        if action_key is not None and self._action_counts[action_key] >= self.max_repeated_actions:
            raise BudgetExhaustedError(
                f"Repeated-action limit reached: '{action_key}' attempted "
                f"{self._action_counts[action_key]} times."
            )

    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self.start_time

    @property
    def remaining_steps(self) -> int:
        return max(0, self.max_steps - self.steps)

    def summary(self) -> dict[str, object]:
        return {
            "steps": self.steps,
            "max_steps": self.max_steps,
            "elapsed_s": round(self.elapsed_seconds, 1),
            "max_time_s": self.max_wall_time_seconds,
            "top_actions": self._action_counts.most_common(5),
        }
