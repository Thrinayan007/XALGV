# XBrowser Foundation

This first increment implements session ownership, persistence, tabs, and screenshot
evidence. Chromium remains headful in Docker on `DISPLAY=:99`, visible through
noVNC. Tests use headless Chromium with local HTTP fixtures.

## How this fits XALGV

The host runs the planner/executor through `local_agent/core/loop.py`. Tools in
`local_agent/tools/` pass through the registry's permission policy, execute using
`local_agent/sandbox/client.py`, and return `ToolResult` plus action verification.
Working memory bounds conversation history; budgets bound the execution loop.
The core verifier falls back to an LLM when a tool has no verifier.

The sandbox runs a FastAPI tool server independently of the host. Its browser
router now delegates lifecycle and navigation to `BrowserSession`. Existing
content/click/type endpoints use the same context, active tab, and operation lock.
The browser owns all Playwright objects; neither the model nor the client sees them.
The Docker image copies the new `xbrowser` package beside the tool server.

The old screenshot request accidentally declared scroll fields and referenced
nonexistent `selector` and `full_page` attributes. It now has a validated screenshot
contract. Accessibility content uses Playwright locator ARIA snapshots rather than
the removed `page.accessibility` interface.

## Scope and lifecycle

One sandbox process owns one selected session scope. Configure these environment
variables before starting the server:

| Variable | Default | Meaning |
| --- | --- | --- |
| `XBROWSER_WORKSPACE` | `/workspace` | Artifact and profile root |
| `XBROWSER_SCOPE` | `default` | Explicit persistent scope; letters, digits, `_`, `-` |
| `XBROWSER_HEADLESS` | `0` | Set to `1` for tests or a display-free server |

The profile is `<workspace>/.xbrowser/<scope>/profile`. Reusing a scope intentionally
retains cookies and local storage; use separate scopes for separate users or runs.
Only one process/worker may own a profile. This increment does not multiplex
independent scopes through a shared API server. The host must select a dedicated
sandbox/scope when it needs isolation across runs. Profiles are excluded from Git;
future document ingestion must explicitly exclude `.xbrowser` as well.

`session_id` identifies the controller lifetime; `generation` increments on every
Chromium launch. Tab IDs monotonically increase across launches within that
controller. A restarted server gets a new session ID. Consumers must use the tuple
`(session_id, generation, tab_id)` and discard earlier state after a restart.
Unexpected context closure or a renderer crash causes recovery before the next
operation, reported by `restarted=true`. Interrupted actions are never replayed.
Health does not launch Chromium. Server shutdown closes the context and driver.

All operations, including legacy endpoints, serialize on one lock. Popups become
active when registered. Closing the active tab selects the most recently registered
remaining tab. Closing the last tab leaves an empty registry until the next page
operation creates a new blank tab. No stale tab ID is repurposed.

## API

All browser routes use POST under `/tools/browser`:

| Route | Request |
| --- | --- |
| `/session` | `{"action":"start"}`; also `stop`, `restart`, `health` |
| `/open` | `{"url":"https://example.com","new_tab":false,"wait_for":"load"}` |
| `/tabs` | `{"action":"list"}`; `new`, `switch`, `close`, `back`, `forward`, `reload` |
| `/screenshot` | `{"path":"evidence/page.png","full_page":false,"selector":null}` |

`switch` and `close` require `tab`. `new` optionally accepts `url`. History actions
optionally accept a target `tab`, which also becomes active. `/navigate`,
`/get_content`, `/click`, and `/type` remain available to existing agent tools.
`/scroll` returns actual movement evidence. Request schema errors return HTTP 422;
browser failures return `success=false` with `error` and `error_code`.

Responses retain `success`, `output`, `error`, and `artifacts`. New fields include
session/generation identity, active tab ID, restart notification, and a structured
`data.tabs` registry. The host client preserves these fields in `ToolResult.metadata`.
New client methods are `browser_session`, `browser_open`, and `browser_tabs`.
Existing model tool names remain unchanged during this foundation increment.

`action_verification` carries mechanical evidence: final navigation URL and HTTP
status, selected tab, or screenshot existence and size. `task_verification` remains
`not_evaluated`: completing a browser command does not establish that the user's
research, report, or other objective has been completed. Objective checks belong
in the [trusted task verifier](xbrowser-security.md), which is now integrated.

## Current safeguards and boundaries

Explicit navigation accepts HTTP(S) without embedded credentials, plus
`about:blank`. Screenshots require PNG paths confined to the workspace, outside
browser profiles. Default names are unique; explicit existing files fail without
overwriting. The browser rejects downloads, dismisses dialogs (including prompts
and confirmations), and retains a bounded diagnostic event buffer. There is no
model-exposed JavaScript evaluation endpoint.

Domain/IP policies, pinned-IP proxying, redirect/subresource restrictions,
sensitive-form approvals, and ingestion exclusions are now implemented. Private
network access defaults to denied; tests opt into exact fixture origins. Browser
API access assumes the trusted local host, matching Compose's loopback-bound
ports. See [security and trusted completion](xbrowser-security.md) for boundaries.

Marks, snapshot-scoped references, and compact observations are now implemented;
see [the observation guide](xbrowser-observations.md). The
[execution layer](xbrowser-execution.md) adds bounded macros, state-aware repeat
detection, and explicit plan-step progress. Trusted task acceptance checks,
deployment, tokenizer, and local-model evaluation are covered by the validation report.

## Validation

Install the development and sandbox extras and Chromium:

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e '.[dev,sandbox]'
$env:PLAYWRIGHT_BROWSERS_PATH = "$PWD\.venv\playwright-browsers"
.\.venv\Scripts\python -m playwright install chromium
.\.venv\Scripts\python -m pytest tests/xbrowser -q
.\.venv\Scripts\python -m pytest tests --ignore=tests/xbrowser -q
```

Browser tests require Chromium and fail if it cannot launch; they do not silently
skip integration coverage. Tests cover local navigation, stable tabs, popups,
persistent and isolated storage, restart recovery, locking/cancellation, dialog
dismissal, artifacts, and legacy compatibility. The end-to-end test calls the
actual sandbox FastAPI app through the agent's HTTP client using ASGI transport.
It uses real Chromium but does not exercise Docker networking or Xvfb/noVNC.

For container validation, start Docker Desktop, then rebuild with
`docker compose up -d --build sandbox`. Use one Uvicorn worker per selected profile.
