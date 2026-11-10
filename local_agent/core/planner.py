"""Planner — generates and revises multi-step plans via the LLM.

The planner asks the model to decompose a goal into short, concrete
steps.  Plans are kept deliberately short (≤ 8 steps by default) to
maximise reliability on small models.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from local_agent.core.state import AgentState
from local_agent.models import ModelManager

logger = logging.getLogger(__name__)

PLAN_SYSTEM_PROMPT = """\
You are a planning module for an autonomous computer-using agent.
You operate inside a sandboxed Linux environment. Use ONLY the available tools
listed for this run. Unlisted tools and external programs are not available.

Design principle: "Structure first, pixels second."  Prefer the
browser_observe refs over guessed selectors. Capture screenshots when the goal
requires evidence; group deterministic open/scroll/screenshot steps in browser_run.

Given a goal and the current state, produce a concise plan of at most 8 objectives.
Each objective may need several tool actions. Keep each description under 160 characters.
Never omit screenshots or other deliverables explicitly requested by the user.
When a URL and screenshot paths are already supplied, use ONE objective: run a
single browser_run containing open, screenshot, then alternating scroll/screenshot.
Do not add observation or separate navigation objectives to that known sequence.
Browser screenshots MUST use browser_screenshot or browser_run. Never start a
second Playwright browser through shell/Python. "Viewport" means full_page=false;
do not scroll first unless the user requested another position. To describe what
is visible, use browser_screenshot with describe=true or browser_observe with text=true.
Do not plan Python/OCR or installation for normal page descriptions.

Office deliverables:
- Use office_create_document to create formatted Word (.docx) documents with headings, text, and tables.
- Use office_create_spreadsheet to create Excel (.xlsx) spreadsheets with sheets, data, and formulas.
- Use office_convert_to_pdf to export documents to PDF via LibreOffice.
- Use office_inspect to verify generated documents, sheets, and word counts.

Desktop GUI automation:
- Use gui_launch_app to launch desktop applications (e.g. 'terminal', 'xterm', 'feh', 'libreoffice').
- Use gui_list_windows to find open window titles, IDs, and positions on the virtual desktop.
- Use gui_click to click buttons or interface coordinates (0-1280, 0-720).
- Use gui_type to enter text into the active window.
- Use gui_hotkey to trigger keyboard shortcuts (e.g. 'Return', 'ctrl+c', 'Alt+F4').
- Use gui_screenshot to observe the full virtual desktop.

Respond with ONLY a JSON object:
{
  "plan": [{"objective": "Find the tutorial", "success_conditions": {"url_host": "docs.python.org", "page_text_contains": "Python Tutorial"}, "max_retries": 3}],
  "reasoning": "Brief explanation of the approach"
}
Use success_conditions only when you can name an observable outcome for the objective.
Supported keys are url (exact), url_host (exact hostname), url_contains, and page_text_contains.
Omit conditions when the outcome needs human judgment. Never use generic page changes as proof.
"""

REPLAN_SYSTEM_PROMPT = """\
You are a re-planning module.  The current plan has hit a problem.
Given the goal, what has been done so far, and the error, produce a
REVISED plan for the remaining work.  Keep it short (at most 6 new steps).

Respond with ONLY a JSON object:
{
  "plan": [{"objective": "next step", "success_conditions": {"url": "https://example.org/result"}}],
  "reasoning": "Why the plan changed"
}
Use the same conditions as the original planner: url, url_host, url_contains, page_text_contains.
"""


@dataclass(frozen=True)
class PlanObjective:
    objective: str
    checks: tuple[dict, ...] = ()
    max_retries: int | None = None

    @classmethod
    def parse(cls, step):
        if isinstance(step, str) and step.strip():
            return cls(step.strip()[:160])
        if not isinstance(step, dict) or not isinstance(step.get("objective"), str) or not step["objective"].strip():
            return None
        kinds = {"url": "url", "url_host": "url_host", "url_contains": "url_contains", "page_text_contains": "text"}
        conditions = step.get("success_conditions")
        checks = tuple(
            {"kind": kinds[key], "expected": value.strip()}
            for key, value in conditions.items()
            if key in kinds and isinstance(value, str) and 0 < len(value.strip()) <= 500
        ) if isinstance(conditions, dict) else ()
        retries = step.get("max_retries")
        retries = retries if type(retries) is int and 1 <= retries <= 3 else None
        return cls(step["objective"].strip()[:160], checks, retries)


class Planner:
    """Generates and revises plans via the model manager."""

    def __init__(self, model_manager: ModelManager, max_plan_steps: int = 16, *, tool_names=None) -> None:
        self._model = model_manager
        self._max_steps = max_plan_steps
        self._tool_names = tool_names

    def _with_tools(self, prompt):
        if self._tool_names is not None:
            prompt += "\nAvailable tools for this run: " + ", ".join(self._tool_names)
        return prompt

    async def create_plan(self, goal: str) -> tuple[list[str | PlanObjective], str]:
        """Generate an initial plan for *goal*.

        Returns ``(plan_steps, reasoning)``.
        """
        messages = [
            {"role": "system", "content": self._with_tools(PLAN_SYSTEM_PROMPT)},
            {"role": "user", "content": f"Goal: {goal}"},
        ]
        response = await self._model.chat("planner", messages, max_tokens=1024)
        return self._parse_plan(response.content)

    async def replan(self, state: AgentState) -> tuple[list[str | PlanObjective], str]:
        """Revise the plan based on current state and errors.

        Returns ``(new_remaining_steps, reasoning)``.
        """
        context = state.summary_for_model()
        messages = [
            {"role": "system", "content": self._with_tools(REPLAN_SYSTEM_PROMPT)},
            {"role": "user", "content": (
                f"Goal: {state.goal}\n\n"
                f"Current state:\n{context}\n\n"
                f"Error: {state.last_error or 'Plan step failed or was unclear.'}"
            )},
        ]
        response = await self._model.chat("planner", messages, max_tokens=1024)
        return self._parse_plan(response.content)

    def _parse_plan(self, content: str) -> tuple[list[str | PlanObjective], str]:
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
                    valid_plan = []
                    for step in plan:
                        objective = PlanObjective.parse(step)
                        if objective:
                            valid_plan.append(step.strip() if isinstance(step, str) else objective)

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
