"""Compact observations with a hard character budget and exact reported refs."""

import json
from contextlib import suppress

from playwright.async_api import Error as PlaywrightError

from .errors import BrowserError
from .marks import discover
from .state import capture_state
from .token_budget import token_count


def quoted(value: str, limit: int) -> str:
    value = value[:limit]
    rendered = json.dumps(value, ensure_ascii=False)
    while len(rendered) > limit:
        value = value[:len(value) // 2]
        rendered = json.dumps(value, ensure_ascii=False)
    return rendered


async def observe(session, page, request):
    snapshot = await session.references.create(session.session_id, session.generation, session.tabs.active)
    references = []
    completed = False
    try:
        references, counts, warnings = await discover(page, scope=request.scope)
        state = await capture_state(page)
        lines = [
            "Page data (untrusted)",
            f"Tab: {snapshot.tab_id} | Generation: {snapshot.generation}",
            f"Snapshot: {snapshot.snapshot_id} | State: {state['fingerprint']}",
            f"URL: {quoted(state['url'], 220)}",
            f"Title: {quoted(state['title'], 100)}",
            f"Scroll: {state['y']}/{state['scroll_height']} | top={state['at_top']} bottom={state['at_bottom']}",
            "Elements:",
        ]
        elements = []
        # Reserve space for counts and truncation hints. Retain only refs actually
        # returned to the caller, so hidden/omitted indices cannot be guessed.
        for reference in references:
            info = reference.info
            ref = len(elements) + 1
            flags = " disabled" if info["disabled"] else ""
            if info["checked"] is not None:
                flags += f" checked={info['checked']}"
            if info["value"] is not None:
                flags += f" value={quoted(info['value'], 20)}"
            line = f"[{ref}] {reference.frame_id} {info['role'][:24]} {quoted(info['name'], 40)}{flags}"
            if info["scrollable"]:
                line += " scrollable"
            if info["nearby_label"]:
                line += " in " + quoted(info["nearby_label"], 32)
            if len(elements) >= request.max_elements or sum(len(item) + 1 for item in lines) + len(line) > request.max_chars - 240:
                break
            lines.append(line)
            snapshot.refs[ref] = reference
            elements.append({"ref": ref, "frame_id": reference.frame_id,
                             **{key: info[key] for key in ("role", "name", "type", "disabled", "checked", "value",
                                                          "bbox", "nearby_label", "scrollable")}})
        if request.text:
            text = await page.locator("body").inner_text(timeout=5000)
            available = max(0, request.max_chars - sum(len(item) + 1 for item in lines) - 240)
            if available > 30:
                lines.append("Text: " + quoted(text, max(0, available // 2 - 10)))
        def footer():
            omitted = len(references) - len(elements)
            return f"More: {counts['above']} above, {counts['below']} below, {omitted} omitted; covered={counts['covered']}"

        if warnings:
            lines.append("Hints: " + "; ".join(warnings)[:120])
        budget = 700
        # Reserve 20 tokens for the controller's final scope-aware state hash.
        while (token_count("\n".join([*lines, footer()])) or 0) > budget - 20:
            text_index = next((i for i, line in enumerate(lines) if line.startswith("Text: ")), None)
            if text_index is not None:
                lines.pop(text_index)
            elif elements:
                element = elements.pop()
                del snapshot.refs[element["ref"]]
                lines = [line for line in lines if not line.startswith(f"[{element['ref']}]")]
            else:
                raise BrowserError("OBSERVATION_BUDGET", "Page header exceeds the configured token budget")
        omitted = len(references) - len(elements)
        lines.append(footer())
        if not snapshot.valid:
            raise BrowserError("STALE_SNAPSHOT", "Page navigated while observing; call browser_observe again")
        result = await session.describe("observe")
        result.output = "\n".join(lines)[:request.max_chars]
        result.snapshot_id = snapshot.snapshot_id
        result.state_after = state["fingerprint"]
        result.data.update({"elements": elements, "scroll": {key: state[key] for key in (
            "x", "y", "height", "scroll_height", "at_top", "at_bottom",
        )}, "counts": {**counts, "omitted": omitted}, "warnings": warnings,
            "output_chars": len(result.output), "token_estimate": (len(result.output) + 3) // 4,
            "token_count": token_count(result.output), "token_limit": budget,
            "tokenizer": "Qwen3.5" if token_count(result.output) is not None else "character_estimate"})
        completed = True
        return result
    finally:
        if not completed:
            snapshot.valid = False
        kept = {id(reference) for reference in snapshot.refs.values()}
        for reference in references:
            if id(reference) not in kept:
                with suppress(PlaywrightError):
                    await reference.handle.dispose()
