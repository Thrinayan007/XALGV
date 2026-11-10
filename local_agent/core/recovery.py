"""Deterministic recovery gates for model-selected browser actions."""

from .browser_progress import signature

MUTATIONS = ("click", "type", "press")


class RecoveryManager:
    def __init__(self):
        self.needs_observation = False
        self.uncertain_mutation = False
        self.partial_macros = set()

    @staticmethod
    def read_only(name, args):
        return name in {"browser_observe", "browser_screenshot", "browser_wait"} or (
            name == "browser_tabs" and args.get("action", "list") == "list"
        )

    def restriction(self, name, args):
        if self.uncertain_mutation and not self.read_only(name, args):
            return "A mutation may have completed. Inspect browser state and verify the outcome with trusted checks or request human review."
        if self.needs_observation and not self.read_only(name, args):
            return "Call browser_observe before taking another action."
        if name == "browser_run" and signature(name, {"steps": args.get("steps")}) in self.partial_macros:
            return "This macro already executed some steps. Continue from its partial results."
        return None

    def record(self, name, args, result):
        if not name.startswith("browser_"):
            return None, None
        metadata = result.metadata
        data = metadata.get("data", {})
        if result.success:
            if name == "browser_observe":
                self.needs_observation = False
            return None, None
        code = metadata.get("error_code")
        if name == "browser_run" and data.get("completed_actions", 0):
            self.partial_macros.add(signature(name, {"steps": args.get("steps")}))
        if code in {"HUMAN_APPROVAL_REQUIRED", "APPROVAL_INVALID"}:
            return "human", "A reviewed browser approval is required."
        if code in {"NAVIGATION_DENIED", "ACTION_DENIED", "KEY_DENIED", "UPLOAD_DISABLED"}:
            return "stop", "Sandbox policy blocked the action. Stop this route."
        if data.get("outcome_uncertain") or code in {"ACTION_TIMEOUT", "SUBMISSION_UNCERTAIN", "BROWSER_CRASHED", "SESSION_LOST"}:
            steps = args.get("steps", []) if name == "browser_run" else []
            failed_index = data.get("failed_index")
            if isinstance(steps, list) and type(failed_index) is int and 0 <= failed_index < len(steps):
                steps = steps[failed_index:failed_index + 1]
            mutation = name.removeprefix("browser_") in MUTATIONS or (
                isinstance(steps, list) and any(isinstance(step, dict) and step.get("tool") in MUTATIONS for step in steps)
            )
            if mutation:
                self.uncertain_mutation = True
            self.needs_observation = True
            return "inspect", "Observe before proceeding. Browser observation starts a fresh session after a crash. Verify any possible submission."
        if code in {"STALE_REF", "INVALID_REF", "AMBIGUOUS_TARGET", "TARGET_MISMATCH",
                    "SEMANTIC_TARGET_NOT_FOUND", "TARGET_NOT_ACTIONABLE"}:
            self.needs_observation = not bool(data.get("observation") or metadata.get("snapshot_id"))
            hint = ("Inspect overlays or observe with scope=page to find a scroll container and bring the target into view." if code == "TARGET_NOT_ACTIONABLE"
                    else "Use the fresh observation or call browser_observe to resolve the target.")
            return "inspect", hint
        if code == "VALUE_MISMATCH":
            self.needs_observation = True
            return "inspect", "Observe the field before changing it again. The requested value was not verified."
        return None, None
