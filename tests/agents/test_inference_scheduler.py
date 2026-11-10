import asyncio
from types import SimpleNamespace

import pytest

from local_agent.agents.inference_scheduler import InferenceLimitError, InferenceScheduler, token_budget


async def test_cancelled_caller_cannot_release_active_inference_early(tmp_path):
    first = InferenceScheduler(tmp_path, "http://localhost:11434")
    second = InferenceScheduler(tmp_path, "http://127.0.0.1:11434")
    started = asyncio.Event()
    finished = asyncio.Event()
    other_entered = asyncio.Event()

    async def active():
        started.set()
        await finished.wait()

    async def other():
        other_entered.set()

    request = asyncio.create_task(first.submit(active))
    await asyncio.wait_for(started.wait(), 2)
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    next_request = asyncio.create_task(second.submit(other))
    try:
        await asyncio.sleep(0.12)
        assert not other_entered.is_set()
    finally:
        finished.set()
        await first.drain()
        await asyncio.wait_for(next_request, 2)
    assert other_entered.is_set()


async def test_queue_timeout_cancels_waiter_without_dispatch(tmp_path):
    scheduler = InferenceScheduler(tmp_path, "ollama", queue_timeout=0.05)
    started = asyncio.Event()
    finish = asyncio.Event()

    async def active():
        started.set()
        await finish.wait()

    async def queued():
        pytest.fail("Expired request was dispatched")

    pending = asyncio.create_task(scheduler.submit(active))
    await asyncio.wait_for(started.wait(), 2)
    try:
        with pytest.raises(TimeoutError, match="queue"):
            await scheduler.submit(queued)
    finally:
        finish.set()
        await pending
        await scheduler.drain()
    assert any(metric["status"] == "cancelled" for metric in scheduler.metrics)


async def test_parent_and_child_token_budgets_are_reserved_and_reconciled(tmp_path):
    scheduler = InferenceScheduler(tmp_path, "ollama")
    calls = 0

    async def inference():
        nonlocal calls
        calls += 1
        return SimpleNamespace(prompt_eval_count=25, eval_count=15)

    with token_budget(150) as parent:
        with token_budget(110) as child:
            await scheduler.submit(inference, context_tokens=75, output_tokens=20)
            assert (parent.remaining, child.remaining) == (110, 70)
            with pytest.raises(InferenceLimitError):
                await scheduler.submit(inference, context_tokens=75, output_tokens=20)
        with token_budget(110):
            await scheduler.submit(inference, context_tokens=75, output_tokens=20)
            with pytest.raises(InferenceLimitError):
                await scheduler.submit(inference, context_tokens=75, output_tokens=20)
    assert calls == 2
