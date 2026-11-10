"""Bounded sequential execution, partial evidence, and no automatic replay."""

import asyncio
import json
from contextlib import suppress

from playwright.async_api import Error as PlaywrightError

from .controller import dispatch, failure
from .errors import BrowserError
from .schemas import BrowserResponse


async def run_connected(session, request, connection):
    """FastAPI does not automatically cancel handlers when a client disconnects."""
    task = asyncio.create_task(run(session, request))
    try:
        while not task.done():
            await asyncio.wait({task}, timeout=0.2)
            if not task.done() and await connection.is_disconnected():
                raise asyncio.CancelledError("Macro client disconnected; remaining steps cancelled")
        return await task
    finally:
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def run(session, request):
    results: list[BrowserResponse] = []
    current = None
    failed_index = None
    try:
        # Preflight capabilities for the entire macro before its first side effect.
        for step in request.steps:
            session.policy.check(step.tool)
        async with asyncio.timeout(request.timeout_ms / 1000):
            async with session.operation():
                for index, step in enumerate(request.steps):
                    current = step.tool
                    failed_index = index
                    try:
                        async with asyncio.timeout(30):
                            result = await dispatch(session, step.tool, step.args)
                    except (BrowserError, PlaywrightError, OSError, ValueError, TimeoutError) as exc:
                        result = failure(session, step.tool, exc)
                    results.append(result)
                    current = None
                    if not result.success:
                        break
                else:
                    failed_index = None
    except (BrowserError, PlaywrightError, OSError, ValueError, TimeoutError) as exc:
        if current is not None:
            results.append(failure(session, current, exc))
        else:
            # Failure before dispatch (policy/lock/startup) consumes no sub-actions.
            result = failure(session, "run", exc)
            result.data = {"results": [], "attempted_actions": 0, "failed_index": None}
            return result
    success = failed_index is None
    failed = results[-1] if not success else None
    # Keep per-action evidence in the text returned to the model. The full
    # structured results remain in ``data``, but working memory sends only
    # ``output`` to the model. A bare "scroll: ok" hides movement and edge
    # state, and hides observations bundled into a macro.
    lines = []
    for index, item in enumerate(results):
        status = "ok" if item.success else (item.error_code or "failed")
        line = f"{index + 1}. {item.action}: {status}"
        if item.action_verification:
            line += "\n   Evidence: " + json.dumps(item.action_verification, ensure_ascii=False, sort_keys=True)
        if item.action == "observe" and item.output:
            observation = item.output
            if len(observation) > 2200:
                observation = observation[:2100] + "\n…[observation shortened]…"
            line += "\n   " + observation
        if item.error:
            line += "\n   Error: " + item.error[:500]
        lines.append(line)
    output = "\n".join(lines)
    artifacts = [path for item in results for path in item.artifacts]
    if artifacts:
        output += "\nArtifacts:\n" + "\n".join(artifacts)
    return BrowserResponse(
        success=success, action="run", output=output,
        error=failed.error if failed else None, error_code=failed.error_code if failed else None,
        session_id=session.session_id, generation=session.generation, tab_id=session.tabs.active,
        restarted=any(item.restarted for item in results), artifacts=artifacts,
        state_before=results[0].state_before if results else None,
        state_after=results[-1].state_after if results else None,
        state_changed=bool(results and results[-1].state_after is not None and results[0].state_before != results[-1].state_after),
        data={"results": [item.model_dump() for item in results], "attempted_actions": len(results),
              "failed_index": failed_index, "completed_actions": sum(item.success for item in results),
              "outcome_uncertain": bool(failed and failed.data.get("outcome_uncertain"))},
        action_verification={"sequence_completed": success, "artifacts_created": len(artifacts)},
    )
