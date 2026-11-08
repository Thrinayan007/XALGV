"""Trusted task acceptance, independent of model tool arguments."""

from local_agent.tools.base import VerificationResult, VerificationStatus


class TaskVerifier:
    def __init__(self, sandbox_client):
        self._sandbox = sandbox_client

    async def verify(self, checks):
        result = await self._sandbox.browser_verify(checks)
        passed = result.success and result.metadata.get("task_verification") == "passed"
        return VerificationResult(
            status=VerificationStatus.PASSED if passed else VerificationStatus.FAILED,
            message="Trusted acceptance criteria passed." if passed else result.error or "Task checks failed.",
            evidence=result.metadata.get("data", {}),
        )
