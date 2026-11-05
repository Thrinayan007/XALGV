"""Verifier — post-action verification and LLM-judge fallback.

Verification order:
1. The tool's own ``verify()`` method (programmatic — preferred).
2. An LLM judge (last resort, used only when the tool has no verifier).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from local_agent.models import ModelManager
from local_agent.tools.base import VerificationResult, VerificationStatus

logger = logging.getLogger(__name__)

JUDGE_SYSTEM_PROMPT = """\
You are a verification judge.  Given:
- The agent's intended action
- The tool output

Decide whether the action succeeded.  Respond with ONLY a JSON object:
{
  "passed": true or false,
  "message": "Brief explanation"
}
"""


class Verifier:
    """Runs post-action verification, with an LLM judge as fallback."""

    def __init__(self, model_manager: ModelManager) -> None:
        self._model = model_manager

    async def llm_judge(
        self,
        action_description: str,
        tool_output: str,
    ) -> VerificationResult:
        """Ask the LLM to judge whether an action succeeded.

        This is the *last resort* — prefer programmatic verification.
        """
        messages = [
            {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
            {"role": "user", "content": (
                f"Intended action: {action_description}\n\n"
                f"Tool output (truncated):\n{tool_output[:2000]}"
            )},
        ]
        try:
            response = await self._model.chat("executor", messages, max_tokens=256)
            return self._parse_judgement(response.content)
        except Exception as exc:
            logger.exception("LLM judge failed")
            return VerificationResult(
                status=VerificationStatus.FAILED,
                message=f"LLM judge error: {exc}",
            )

    def _parse_judgement(self, content: str) -> VerificationResult:
        try:
            text = content.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1] if "\n" in text else text[3:]
                if text.endswith("```"):
                    text = text[:-3]
                text = text.strip()
            data = json.loads(text)
            passed = bool(data.get("passed", False))
            message = str(data.get("message", ""))
            return VerificationResult(
                status=VerificationStatus.PASSED if passed else VerificationStatus.FAILED,
                message=message,
            )
        except (json.JSONDecodeError, AttributeError):
            logger.warning("Could not parse LLM judge response: %s", content[:200])
            return VerificationResult(
                status=VerificationStatus.FAILED,
                message=f"Could not parse judge response: {content[:200]}",
            )
