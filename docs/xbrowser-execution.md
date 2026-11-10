# XBrowser actions and bounded workflows

The shared execution layer implements nine browser actions plus an optional macro
tool. The CLI and evaluation registries use this set by default. Legacy
`browser_navigate`/`browser_get_content` tool classes and HTTP endpoints remain
available to callers that still use them.

See [the architecture](xbrowser-architecture.md) for ownership, locks, verification
boundaries, and the completed validation gates.

## API and model surface

All routes use POST under `/tools/browser`. The model tool name is `browser_`
followed by the route name. Existing request arguments retain their meanings.

| Route | Main arguments | Evidence |
| --- | --- | --- |
| `/open` | `url`, `new_tab`, `wait_for` | Final URL, HTTP status, host match |
| `/observe` | `scope`, `text`, `max_elements`, `max_chars` | Snapshot refs and page state |
| `/click` | `ref` + `snapshot_id`, or selector/text/role | Dispatched and before/after state |
| `/type` | Target, `text`, `clear_first`, `press_enter` | Value match; skip submission on mismatch |
| `/press` | `key` | Keyboard dispatch and resulting state |
| `/scroll` | `direction`, `pixels`, optional target, `wait_ms` | Position, moved, four edges |
| `/tabs` | `action`, `tab`, `url` | Active tab and registry |
| `/wait` | `condition`, `value`, `timeout_ms`, `milliseconds` | Condition satisfied or timeout |
| `/screenshot` | `path`, `selector`, `full_page` | Confined PNG path, existence and size |
| `/run` | Typed `steps`, `timeout_ms`, host `action_budget` | Per-action results and partial artifacts |

Scroll targets support a selector or snapshot reference for a nested scroll
container. Untargeted scrolling affects the document. Directions include up/down,
left/right, and top/bottom. Wait conditions are visible `text`, visible `selector`,
network `idle`, or a bounded `ms` delay. No arbitrary JavaScript wait predicate is
accepted. `idle` is optional because continuously active pages may never satisfy it.

`browser_tools(client, include_macro=False)` exposes exactly nine core tools.
The default enables the tenth `browser_run` tool. Old tool wrappers remain
importable but are not duplicated in the default model schema list.

## A deterministic capture workflow

The client can send a single macro:

```python
steps = [{"tool": "open", "args": {"url": url}}]
for index in range(5):
    if index:
        steps.append({"tool": "scroll", "args": {"pixels": 650}})
    steps.append({"tool": "screenshot", "args": {"path": f"evidence/page-{index}.png"}})
result = await client.browser_run(steps)
```

The server validates the whole request before execution and limits it to 20
actions. Each step uses the same action dispatcher as its single-action endpoint.
There are no nested macros, shell commands, lifecycle operations, or eval steps.
The lock covers the entire sequence. A failure stops subsequent steps, preserving
completed results and artifacts. `data.failed_index` is zero-based;
`data.attempted_actions` includes a failed dispatched step.

The shared deadline includes lock waiting and startup. The maximum is 90 seconds,
default 60; each action is also bounded to 30 seconds. Single HTTP actions have a
35-second boundary. The agent clamps the macro deadline and action allowance to
its remaining run budget and counts sub-actions rather than model calls. A lost
result conservatively consumes the entire reserved allowance.

Timeouts report uncertain outcomes: the last operation may already have produced
a side effect. Observe/reconcile before choosing the next action. Earlier steps
are not rolled back or replayed. Client disconnection cancels remaining macro
steps; an already dispatched browser side effect cannot be undone by cancellation.

## Sandbox policy

`XBROWSER_ALLOWED_ACTIONS` optionally lists comma-separated permitted core actions.
For example, `open,observe,tabs,wait,scroll,screenshot` denies click/type/press at
the sandbox dispatcher. An empty value denies all core actions. Lifecycle API
access still assumes the trusted local host. Macro capabilities are checked both
before the first side effect and at every step; legacy content access requires
the observe capability. HTTP bodies cannot grant capabilities.

The capability policy is paired with domain/IP, redirect/subresource and
sensitive-action controls. Private targets are denied by default. See
[the security guide](xbrowser-security.md) for exact local exceptions and approvals.

## Agent behavior and evidence

A successful action stays on the current plan objective. `STEP_DONE: <evidence>`
triggers deterministic evaluation of any objective checks. The planner may propose
exact URL, hostname, or visible-text conditions. The run records the source of each
check separately. For important steps, supply trusted milestones in the acceptance
JSON as `"milestones": [{"objective": "Find the tutorial", "checks": [{"kind":
"url_host", "expected": "docs.python.org"}]}]`. Trusted milestones define the ordered
plan and survive replanning. Optional top-level `checks` still gate final `DONE:`.
Unchecked steps retain model-reported completion with successful action evidence.

Repeat signatures hash full canonical arguments and browser session/generation/
tab/state. Changed-state scrolling is allowed; repeated direct evidence of no
movement is independently bounded even if a dynamic page changes its fingerprint.
State capture includes bounded nested-container scroll positions.
Run history separates mechanical action success, relevant state change, and
objective verification. Stale or obscured targets require fresh observation.
An uncertain browser mutation locks the executor to inspection until the run
passes trusted final checks or stops for human review. A partially executed macro
cannot be replayed as the same sequence by changing its timeout.

Older successful observation payloads are compacted to snapshot references.
Errors, screenshots, and other action results stay in working memory subject to
the existing overall context limit. SQLite stores full tool output, structured
result metadata, and artifact paths. An additive `steps.result_metadata` migration
preserves existing run records. Action results report
`task_verification=not_evaluated`; the separate `/verify` endpoint and host task
verifier report whether the supplied criteria passed or failed.

The suite covers the five-screenshot workflow with real Chromium, partial failures,
schema/capability denials, lock waiting, cancellation/disconnection, new action
mechanics, host budget/progress behavior, and evidence persistence. Docker/Xvfb,
noVNC, actual tokenizer limits and local-model performance have separate recorded
results in the validation report.
