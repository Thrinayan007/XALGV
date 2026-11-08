# XBrowser security and trusted completion

XBrowser now enforces browser policy inside the sandbox, including individual
macro steps. These controls complement the host tool registry. They do not turn
arbitrary web applications into semantically read-only services.

## Network boundary

Chromium uses a browser-owned HTTP/CONNECT proxy. The proxy validates every
destination's domain, port, and DNS answers, then connects to a validated literal
IP rather than resolving the hostname again. TLS remains end to end. This closes
the check/connect DNS-rebinding gap and applies to redirects and subresources,
including redirects that Playwright does not independently route.

Private, loopback, metadata, reserved, multicast, and non-global addresses are
blocked by default. Encoded IPv4 forms, mapped IPv6, and standard IPv4 translation
forms are checked after resolution. Mixed public/private DNS answers fail closed.
Public destinations use ports 80/443. Browser service workers, WebSockets, WebRTC,
and QUIC are disabled to avoid alternate browser egress paths. The proxy bounds
active connections and request-header sizes. Tabs are limited to twelve.

Trusted server configuration:

| Setting | Meaning |
| --- | --- |
| `XBROWSER_ALLOWED_DOMAINS` | Comma-separated exact domains or explicit `*.domain` patterns; empty permits public domains |
| `XBROWSER_DENIED_DOMAINS` | Denials take precedence |
| `XBROWSER_LOCAL_ORIGINS` | Exact HTTP(S) origins for specifically trusted local services; default empty |
| `XBROWSER_ALLOWED_ACTIONS` | Optional core action capability list; an empty configured value denies all |
| `XBROWSER_ADMIN_TOKEN` | Enables authenticated, trusted approval issuance; unset disables it |

Local exceptions deliberately grant access to that origin and must be narrowly
configured. They cannot be supplied in browser tool arguments. The deployment
test temporarily uses only `http://127.0.0.1:8765` in a separate validation scope.
Production configuration is restored afterward.

## Sensitive interactions

Credential/payment controls and sensitive form interactions require a reviewed
approval. Non-GET/HEAD/OPTIONS browser requests are blocked unless an approved
action is currently executing. File uploads and downloads remain disabled.
Browser chrome, developer-tool, file-dialog, and system keyboard shortcuts are
blocked; document navigation/editing keys remain available. Password and common
payment values are omitted from structured observations.

Host applications can call `SandboxClient.browser_authorize(tool, args, admin_token)`
only after the person has reviewed that exact action. The `/authorize` endpoint
requires `X-XBrowser-Admin`. Its single-use token expires after 120 seconds and is
bound to the normalized arguments, session, generation, tab, and page fingerprint.
Use `client.use_browser_approval(token)` for the next click/type/press request.
An altered, replayed, expired, or stale grant fails closed. Model tool schemas
contain no approval-issuance method or administrative secret. Macros cannot carry
an approval blanket for a sequence.

Sensitive-control recognition is conservative and heuristic; HTTP method controls
provide an independent network boundary. Some poorly designed websites mutate
state through GET requests. Domain policy, target review, and acceptance criteria
are still needed; this implementation does not claim to infer every transaction's
business meaning. Native file choosers are intercepted without supplying files.

## Profiles, files, and media

Profiles remain in `.xbrowser/<scope>/profile`. They are excluded from Git and
document ingestion. Ordinary sandbox file APIs are confined to the workspace and
cannot read/write/list profiles or process-environment files outside that root.
Shell and Python execution now require the SYSTEM host tier and a sandbox-side
`X-Sandbox-Admin` credential. They default to disabled in the sandbox. Configure
matching `SANDBOX_ADMIN_TOKEN` (container) and `XALGV_SANDBOX_ADMIN_TOKEN` (host),
then explicitly opt in with `xalgv run ... --allow-execution` or supply a trusted
SYSTEM policy through the API. Python scripts use unique workspace staging paths.
Approved arbitrary execution is a separate trust boundary; browser-only controls
do not confine code that the person explicitly authorized at SYSTEM level.
Do not put administrative secrets in the model-readable workspace.

Artifact paths are resolved before use; symlinks cannot escape the workspace or
point into protected profiles. Screenshots use unique names or exclusive creation.
The image-transfer endpoint accepts only bounded workspace PNGs, limits dimensions,
and downsizes before sending pixels to the configured local vision model. Marked
screenshots are drawn onto bitmap copies; the page DOM is not annotated.

All website text, diagnostics, and vision output remain untrusted page data.
Neither content nor model confidence can mint approval or satisfy a task check.

## Trusted task acceptance

Supply criteria through the trusted caller:

```python
loop = AgentLoop(config, models, registry, memory,
                 task_verifier=TaskVerifier(client))
record = await loop.run(goal, acceptance_checks=[
    {"kind": "url", "expected": "https://example.com/"},
    {"kind": "artifact", "path": "evidence/page.png", "png": True,
     "width": 1280, "height": 720},
    {"kind": "artifact", "path": "report.md", "contains": "Required findings"},
])
```

Alternatively, save the checks as a JSON list (or `{"checks": [...]}`) and run
`xalgv run "your goal" --acceptance checks.json`. Supported checks cover exact
active URL, visible text, tab count, file existence/minimum size, UTF-8 contents,
SHA-256, PNG decoding, and PNG dimensions. All checks must pass.

The loop copies the trusted criteria before execution. A model's `DONE:` triggers
the checker rather than overriding it. Failed completion claims allow bounded
repair attempts and eventually fail. SQLite records the criteria, checker evidence,
action metadata, and full outputs. Runs without supplied criteria are explicitly
labelled `not_evaluated`/`model_assertion`; they are not called deterministically
verified. Criteria check the properties explicitly supplied, not report quality
that nobody specified.

## Token budget and visual fallback

The Docker image installs the official Qwen 3.5 tokenizer with its SHA-256 pinned
in `sandbox/xbrowser/tokenizer_config.json`. Observations keep only reported refs,
remove optional text/extra elements as necessary, and stay below 700 actual tokens.
For local development, install the sandbox extras and run:

```powershell
.\.venv\Scripts\python -m sandbox.xbrowser.token_budget --download
```

Without a tokenizer asset, local observations explicitly report a character
estimate instead of claiming a measured token cap. `XBROWSER_TOKENIZER_PATH` can
point to the checksum-matching asset. The shipped tokenizer targets Qwen 3.5;
changing model families requires a corresponding tokenizer configuration update.

`browser_screenshot(marks=true)` returns a numbered PNG and snapshot refs.
`browser_screenshot(describe=true)` additionally asks the configured local vision
role to interpret a confined screenshot. It preserves artifacts on inference
failure. Vision text does not grant authorization or replace deterministic checks.
The default vision role uses the same Qwen 3.5 9B model as the executor.
Planner, executor, and vision defaults share the measured 8192-token context and
temperature zero profile, avoiding model reloads just to change roles.
