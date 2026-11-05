"""Working memory — manages the rolling context window for the agent.

The working memory is responsible for:
* Maintaining the conversation messages list within the token budget.
* Compacting long observations into summaries.
* Inserting the current plan and scratchpad into context.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class WorkingMemory:
    """Rolling context window that stays within a token budget.

    This is a simplified token estimator that counts characters ÷ 4
    as a rough approximation.  Replace with a proper tokeniser when
    the model is known.
    """

    max_tokens: int = 8192
    system_prompt: str = ""

    # ── Internal state ───────────────────────────────────────────────────
    _messages: list[dict[str, Any]] = field(default_factory=list, init=False)
    _plan: list[str] = field(default_factory=list, init=False)
    _scratchpad: str = field(default="", init=False)

    # ── Token estimation ─────────────────────────────────────────────────

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        """Rough token count: chars / 4."""
        return max(1, len(text) // 4)

    def _total_tokens(self) -> int:
        total = self._estimate_tokens(self.system_prompt)
        for msg in self._messages:
            total += self._estimate_tokens(str(msg.get("content", "")))
        return total

    # ── Context building ─────────────────────────────────────────────────

    def set_plan(self, plan: list[str]) -> None:
        self._plan = list(plan)

    def set_scratchpad(self, text: str) -> None:
        self._scratchpad = text

    def add_message(self, role: str, content: str, **extra: Any) -> None:
        """Append a message, compacting old messages if over budget."""
        msg: dict[str, Any] = {"role": role, "content": content, **extra}
        self._messages.append(msg)
        self._compact_if_needed()

    def add_tool_result(self, tool_call_id: str, content: str) -> None:
        """Append a tool result message."""
        self._messages.append({
            "role": "tool",
            "content": self._truncate(content, max_chars=4000),
            "tool_call_id": tool_call_id,
        })
        self._compact_if_needed()

    def get_messages(self) -> list[dict[str, Any]]:
        """Return the full message list including the system prompt."""
        system: dict[str, Any] = {"role": "system", "content": self._build_system()}
        return [system] + list(self._messages)

    def clear(self) -> None:
        self._messages.clear()
        self._plan.clear()
        self._scratchpad = ""

    @property
    def message_count(self) -> int:
        return len(self._messages)

    # ── Internal ─────────────────────────────────────────────────────────

    def _build_system(self) -> str:
        parts = [self.system_prompt]
        if self._plan:
            plan_text = "\n".join(f"  {i+1}. {step}" for i, step in enumerate(self._plan))
            parts.append(f"\n\n## Current Plan\n{plan_text}")
        if self._scratchpad:
            parts.append(f"\n\n## Scratchpad\n{self._scratchpad}")
        return "\n".join(parts)

    def _compact_if_needed(self) -> None:
        """Drop oldest non-system messages when over the token budget."""
        while self._total_tokens() > self.max_tokens and len(self._messages) > 2:
            # Keep the first message (usually the goal) and the last few.
            removed = self._messages.pop(1)
            # Insert a brief summary of what was dropped.
            if self._messages and self._messages[0].get("role") == "user":
                pass  # keep the goal message
            summary_note = f"[Earlier message removed for context management: {removed.get('role', '?')}]"
            # Don't insert summary if the next message is already a summary note.
            if not any("[Earlier message removed" in str(m.get("content", "")) for m in self._messages[:2]):
                self._messages.insert(1, {"role": "system", "content": summary_note})

    @staticmethod
    def _truncate(text: str, max_chars: int = 4000) -> str:
        if len(text) <= max_chars:
            return text
        half = max_chars // 2
        return text[:half] + f"\n…[{len(text) - max_chars} chars truncated]…\n" + text[-half:]
