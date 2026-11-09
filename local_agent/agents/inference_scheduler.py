"""Bounded inference admission across coroutines and cooperating XALGV processes."""

import asyncio
import hashlib
import sqlite3
import time
from collections.abc import Callable
from contextlib import asynccontextmanager, contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4


@asynccontextmanager
async def resource_lock(directory: Path, key: str):
    """An OS-backed SQLite lock, released when its owning process exits."""
    directory.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(key.encode()).hexdigest()
    connection = sqlite3.connect(directory / f"{digest}.db", timeout=0, isolation_level=None)
    try:
        while True:
            try:
                connection.execute("BEGIN IMMEDIATE")
                break
            except sqlite3.OperationalError as exc:
                if (exc.sqlite_errorcode & 255) not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                    raise
                await asyncio.sleep(0.05)
        yield
    finally:
        connection.close()


class InferenceLimitError(RuntimeError):
    pass


@dataclass
class TokenBudget:
    remaining: int
    account: Callable[[int], None] | None = None


_budgets: ContextVar[tuple[TokenBudget, ...]] = ContextVar("inference_budgets", default=())


def budgeted() -> bool:
    return bool(_budgets.get())


@contextmanager
def token_budget(limit: int, account=None):
    budget = TokenBudget(limit, account)
    token = _budgets.set((*_budgets.get(), budget))
    try:
        yield budget
    finally:
        _budgets.reset(token)


class InferenceScheduler:
    def __init__(self, directory: Path, endpoint: str, *, max_pending: int = 16, queue_timeout: float = 120):
        self.directory = directory
        self.key = "ollama:" + endpoint.rstrip("/").lower().replace("://localhost:", "://127.0.0.1:")
        self.max_pending = max_pending
        self.queue_timeout = queue_timeout
        self._lock = asyncio.Lock()
        self._pending = 0
        self._active = set()
        self.metrics: list[dict] = []

    async def _run(self, call, *, context_tokens: int, output_tokens: int, label: str):
        request_id = uuid4().hex
        queued = time.monotonic()
        budgets = _budgets.get()
        started = None
        status = "failed"
        actual = None
        try:
            async with self._lock, resource_lock(self.directory, self.key):
                started = time.monotonic()
                reserve = context_tokens + output_tokens
                if any(budget.remaining < reserve for budget in budgets):
                    raise InferenceLimitError("Insufficient token budget for another model request")
                for budget in budgets:
                    if budget.account:
                        budget.account(reserve)
                    budget.remaining -= reserve
                response = await call()
                actual = getattr(response, "prompt_eval_count", 0) + getattr(response, "eval_count", 0)
                for budget in budgets:
                    refund = max(0, reserve - actual)
                    if budget.account:
                        budget.account(-refund)
                    budget.remaining += refund
                if budgets and actual > reserve:
                    for budget in budgets:
                        if budget.account:
                            budget.account(budget.remaining)
                        budget.remaining = 0
                    raise InferenceLimitError("Backend exceeded its reserved token envelope")
                status = "completed"
                return response
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        finally:
            now = time.monotonic()
            self.metrics.append({"request_id": request_id, "role": label, "status": status, "actual_tokens": actual,
                                 "queue_seconds": (started or now) - queued,
                                 "execution_seconds": now - started if started is not None else 0})
            del self.metrics[:-256]

    async def submit(self, call, *, context_tokens: int = 0, output_tokens: int = 0, label: str = "inference"):
        # Only the queue wait is cancellable by the caller. An admitted HTTP
        # request retains ownership until the backend returns or times out.
        if self._pending >= self.max_pending:
            raise InferenceLimitError("Inference queue is full")
        self._pending += 1
        admitted = asyncio.Event()

        async def invoke():
            admitted.set()
            return await call()

        task = asyncio.create_task(self._run(invoke, context_tokens=context_tokens, output_tokens=output_tokens, label=label))
        self._active.add(task)
        task.add_done_callback(self._finished)
        ready = asyncio.create_task(admitted.wait())
        try:
            done, _ = await asyncio.wait({task, ready}, timeout=self.queue_timeout, return_when=asyncio.FIRST_COMPLETED)
            if not done:
                raise TimeoutError("Inference queue deadline expired")
            return await asyncio.shield(task)
        finally:
            ready.cancel()
            if not admitted.is_set() and not task.done():
                task.cancel()
            with suppress(asyncio.CancelledError):
                await ready

    def _finished(self, task):
        self._active.discard(task)
        self._pending -= 1
        if not task.cancelled():
            task.exception()

    async def drain(self):
        if self._active:
            await asyncio.gather(*tuple(self._active), return_exceptions=True)
