"""Recognize an explicit terminal marker following optional model explanation."""

import re


def terminal_response(content: str) -> str:
    matches = list(re.finditer(r"(?m)^(?:DONE|STEP_DONE|BLOCKED):[^\n]*", content, flags=re.IGNORECASE))
    if not matches:
        return content
    marker = matches[-1]
    # A quoted/code example is not a terminal instruction. Deterministic task
    # checks still decide whether an accepted DONE actually completed the goal.
    if content[:marker.start()].count("```") % 2:
        return content
    return content[marker.start():].strip()
