"""Mechanical action evidence and relevant browser state, separate from goal checks."""

import hashlib
import json
from collections import Counter


def signature(name: str, arguments: dict, scope=None) -> str:
    canonical = json.dumps([name, arguments, scope], sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return f"{name}:{hashlib.sha256(canonical.encode()).hexdigest()}"


class BrowserProgress:
    def __init__(self):
        self.scope = None
        self.state = None
        self.stalled = Counter()
        self.unproductive = Counter()

    def action_key(self, name, arguments):
        return signature(name, arguments, [self.scope, self.state] if name.startswith("browser_") else None)

    def stall_key(self, name, arguments):
        stable = {key: value for key, value in arguments.items() if key != "snapshot_id"}
        return signature(name, stable, self.scope)

    def reset_objective(self):
        self.stalled.clear()
        self.unproductive.clear()

    def record(self, name, arguments, result):
        if not name.startswith("browser_"):
            return {}
        metadata = result.metadata
        previous_scope = self.scope
        scope = [metadata.get("session_id"), metadata.get("generation"), metadata.get("tab_id")]
        if all(value is not None for value in scope):
            self.scope = scope
        if metadata.get("state_after"):
            self.state = metadata["state_after"]
        key = self.stall_key(name, arguments)
        evidence = metadata.get("action_verification", {})
        if name == "browser_scroll" and result.success:
            if evidence.get("moved") is False:
                self.stalled[key] += 1
            else:
                self.stalled[key] = 0
        meaningful = result.success and (
            (previous_scope is not None and self.scope != previous_scope)
            or any(evidence.get(field) is True for field in ("moved", "url_changed", "value_changed", "viewport_moved"))
        )
        if name in {"browser_click", "browser_scroll", "browser_press", "browser_open"}:
            self.unproductive[key] = 0 if meaningful else self.unproductive[key] + 1
        return {
            "action_success": result.success,
            "state_changed": metadata.get("state_changed"),
            "relevant_state_progress": meaningful,
            "goal_progress": "unverified",
        }
