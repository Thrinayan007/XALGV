"""Structured repeat signatures and direct evidence of stalled scrolling."""

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

    def action_key(self, name, arguments):
        return signature(name, arguments, [self.scope, self.state] if name.startswith("browser_") else None)

    def stall_key(self, name, arguments):
        return signature(name, arguments, self.scope)

    def record(self, name, arguments, result):
        if not name.startswith("browser_"):
            return
        metadata = result.metadata
        self.scope = [metadata.get("session_id"), metadata.get("generation"), metadata.get("tab_id")]
        if metadata.get("state_after"):
            self.state = metadata["state_after"]
        key = self.stall_key(name, arguments)
        if name == "browser_scroll" and result.success:
            if metadata.get("action_verification", {}).get("moved") is False:
                self.stalled[key] += 1
            else:
                self.stalled[key] = 0
