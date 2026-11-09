"""Hold workspace and browser ownership through independent task verification."""

import asyncio
import math
from contextlib import AsyncExitStack, asynccontextmanager
from contextvars import ContextVar

from local_agent.sandbox.client import BrowserLeaseUnavailable

from .base import AgentRole
from .inference_scheduler import resource_lock, token_budget


class ResourceUnavailable(RuntimeError):
    pass


class TaskResources:
    def __init__(self, config, sandbox):
        self._workspace = config.workspace_dir.resolve()
        # All processes using the same workspace coordinate even with different data directories.
        self._directory = self._workspace / ".xbrowser" / "agent-leases"
        self._sandbox = sandbox
        self._owner = ContextVar(f"task_resources_{id(self)}", default=None)

    def holds(self, task):
        return self._owner.get() == (task.run_id, task.task_id)

    @asynccontextmanager
    async def acquire(self, task, *, seconds: float, remaining_tokens: int | None = None):
        owner = f"{task.run_id}/{task.task_id}"
        async with AsyncExitStack() as stack:
            try:
                async with asyncio.timeout(min(seconds, task.max_wall_time_seconds)):
                    await stack.enter_async_context(resource_lock(self._directory, owner))
                    if task.agent == AgentRole.BROWSER:
                        ttl = min(seconds, task.max_wall_time_seconds * task.max_attempts)
                        await stack.enter_async_context(
                            self._sandbox.browser_lease(owner, min(3600, max(1, math.ceil(ttl) + 2))),
                        )
            except (TimeoutError, BrowserLeaseUnavailable) as exc:
                raise ResourceUnavailable(str(exc) or "Timed out waiting for task resources") from exc
            context = self._owner.set((task.run_id, task.task_id))
            try:
                with token_budget(task.max_model_tokens if remaining_tokens is None else remaining_tokens):
                    yield
            finally:
                self._owner.reset(context)
