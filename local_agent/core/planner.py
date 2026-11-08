"""Planner — generates and revises multi-step plans via the LLM.

The planner asks the model to decompose a goal into short, concrete
steps.  Plans are kept deliberately short (≤ 8 steps by default) to
maximise reliability on small models.
"""

from __future__ import annotations

import json
import logging
import re

from local_agent.core.state import AgentState
from local_agent.models import ModelManager

logger = logging.getLogger(__name__)

PLAN_SYSTEM_PROMPT = """\
You are a planning module for an autonomous computer-using agent.
You operate inside a sandboxed Linux environment with these tools:

File & Shell:
  shell_exec, read_file, write_file, list_files, python_exec

Web & Search:
  web_search — search the public web via SearXNG

Browser (Playwright inside sandbox):
  browser_open, browser_observe, browser_click, browser_type, browser_press,
  browser_scroll, browser_tabs, browser_wait, browser_screenshot
  browser_run — up to 20 deterministic actions in one bounded macro

Document Intelligence (RAG):
  ingest_document — parse and embed a document (PDF, DOCX, HTML, TXT)
  query_documents — semantic search over ingested documents with citations

Design principle: "Structure first, pixels second."  Prefer the
browser_observe refs over guessed selectors. Capture screenshots when the goal
requires evidence; group deterministic open/scroll/screenshot steps in browser_run.

Given a goal and the current state, produce a concise plan of at most 8 objectives.
Each objective may need several tool actions. Keep each description under 160 characters.
Never omit screenshots or other deliverables explicitly requested by the user.
When a URL and screenshot paths are already supplied, use ONE objective: run a
single browser_run containing open, screenshot, then alternating scroll/screenshot.
Do not add observation or separate navigation objectives to that known sequence.

Respond with ONLY a JSON object:
{
  "plan": ["step 1 description", "step 2 description", ...],
  "reasoning": "Brief explanation of the approach"
}
"""

REPLAN_SYSTEM_PROMPT = """\
You are a re-planning module.  The current plan has hit a problem.
Given the goal, what has been done so far, and the error, produce a
REVISED plan for the remaining work.  Keep it short (at most 6 new steps).

Respond with ONLY a JSON object:
{
  "plan": ["next step 1", "next step 2", ...],
  "reasoning": "Why the plan changed"
}
"""


class Planner:
    """Generates and revises plans via the model manager."""

    def __init__(self, model_manager: ModelManager, max_plan_steps: int = 16) -> None:
        self._model = model_manager
        self._max_steps = max_plan_steps

    async def create_plan(self, goal: str) -> tuple[list[str], str]:
        """Generate an initial plan for *goal*.

        Returns ``(plan_steps, reasoning)``.
        """
        messages = [
            {"role": "system", "content": PLAN_SYSTEM_PROMPT},
            {"role": "user", "content": f"Goal: {goal}"},
        ]
        response = await self._model.chat("planner", messages, max_tokens=1024)
        return self._parse_plan(response.content)

    async def replan(self, state: AgentState) -> tuple[list[str], str]:
        """Revise the plan based on current state and errors.

        Returns ``(new_remaining_steps, reasoning)``.
        """
        context = state.summary_for_model()
        messages = [
            {"role": "system", "content": REPLAN_SYSTEM_PROMPT},
            {"role": "user", "content": (
                f"Goal: {state.goal}\n\n"
                f"Current state:\n{context}\n\n"
                f"Error: {state.last_error or 'Plan step failed or was unclear.'}"
            )},
        ]
        response = await self._model.chat("planner", messages, max_tokens=1024)
        return self._parse_plan(response.content)

    def _parse_plan(self, content: str) -> tuple[list[str], str]:
        """Extract a valid, non-empty plan from the model response."""

        text = (content or "").strip()

        # Handle Markdown code fences.
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            if text.rstrip().endswith("```"):
                text = text.rstrip()[:-3]
            text = text.strip()

        # First attempt: JSON response.
        try:
            data = json.loads(text)

            if isinstance(data, dict):
                plan = data.get("plan", [])
                reasoning = data.get("reasoning", "")

                if isinstance(plan, list):
                    valid_plan = [
                        step.strip()
                        for step in plan
                        if isinstance(step, str) and step.strip()
                    ]

                    if valid_plan:
                        return valid_plan[:self._max_steps], str(reasoning or "")

        except (json.JSONDecodeError, AttributeError, TypeError):
            pass

        # Second attempt: numbered-list response.
        logger.warning("Could not parse planner JSON; trying numbered-list parsing.")

        plan = []

        # for line in text.splitlines():
        #     stripped = line.strip().lstrip("0123456789.-) ")

        #     if stripped and not stripped.startswith(("{", '"', "}")):
        #         plan.append(stripped)
        for line in text.splitlines():
            match = re.match(
                r"^\s*(?:\d+[.)]|[-*])\s+(.+?)\s*$",
                line
            )

            if match:
                step = match.group(1).strip()
                if step:
                    plan.append(step)
        if plan:
            return plan[:self._max_steps], "Parsed from numbered-list fallback."

        # Never silently convert invalid output into a fake plan.
        raise ValueError(
            "Planner returned an empty or invalid plan. "
            "Expected a non-empty JSON plan or numbered list."
        )
