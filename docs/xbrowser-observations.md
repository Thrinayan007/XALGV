# XBrowser observations and references

The second increment adds read-only element discovery, compact observations,
snapshot-scoped targeting, and deterministic click/type evidence. The existing
selector and text APIs remain available. `browser_observe` is registered in both
the normal CLI and evaluation tool registries.

## Using the API

POST `/tools/browser/observe` with an empty object for the default viewport view.
Options are `scope` (`viewport` or `page`), `text` (default false), `max_elements`
(1–100, default 30), and `max_chars` (800–8000, default 2400).

The response contains compact `output`, a top-level `snapshot_id` and
`state_after`, and structured `data.elements`, `data.scroll`, and counts/warnings.
The host preserves these fields in `ToolResult.metadata`. Frame IDs in each
element identify the originating frame; references automatically target it.

Use a returned reference and snapshot together:

```json
{"ref": 4, "snapshot_id": "s12-<controller-prefix>"}
```

Pass that object to `/tools/browser/click`, or add `text` to call
`/tools/browser/type`. Type retains `clear_first` and `press_enter` options. The
client methods accept the same keyword arguments. The host tool schemas now accept
either references or legacy selectors; type always requires `text`.

## Reference validity

References own exact Playwright element handles in server memory. Discovery does
not add attributes, globals, or event listeners to the page. Snapshot IDs include a
controller identifier to avoid reuse after server restart. Each snapshot is bound
to the session, browser generation, and tab. A new observation supersedes that
tab's previous snapshot; navigation of any of its frames invalidates it as well.

Before dispatch, the browser checks that the original element is still connected,
visible, enabled, uncovered, and has the expected identity (role, accessible-name
inputs, tag, type, ID, link/form target, and inline click handler). It also checks
that containing frames remain visible and uncovered. Playwright performs its own
actionability checks at execution time. A replacement element is never selected
using an old reference. There is no automatic retry of a dispatched action.

`STALE_REF` means observe again. `INVALID_REF` means the index was not reported in
the selected snapshot. `TARGET_NOT_ACTIONABLE` means the target or its frame is
disabled or occluded. Unrelated clocks/advertisements do not invalidate handles;
changing the target's identity does. Ordinary field-value updates are not identity
changes, allowing observe → type → click without an extra observation.

## Discovery and bounds

The implementation handles ordinary controls, common interactive ARIA roles,
open shadow roots, and accessible same-origin and cross-origin Playwright frames.
It filters hidden and covered controls, records disabled/checked state, and omits
password values. Native labels, `aria-labelledby`, `aria-label`, text, image alt
text, and common naming fallbacks supply labels. This is a practical subset of
accessible-name computation, not a complete implementation of the WAI algorithm.
Closed shadow roots are not traversed. Rotated/clipped/custom controls can require
the legacy locator fallback; coverage is sampled rather than exhaustively proven.

Discovery bounds traversal to 12,000 visited elements per frame, at most 16 frames,
and 120 retained candidates. It keeps at most eight tab snapshots. Output contains
only indices actually reported to the caller; undisplayed handles are disposed.
New observations and shutdown dispose previous handles. Limits appear as warnings
instead of pretending the page was fully indexed. Above/below counts describe
discovered frame-local candidates and are hints, not exhaustive page totals.

The default rendered output has a hard 2400-character limit. Text is opt-in and
shares that budget. Labels and URLs are quoted so website newlines do not create
fake observation fields. The response labels page data as untrusted, and the
executor prompt forbids treating page content as instructions or authorization.
`token_estimate` uses characters divided by four. With the checksum-pinned Qwen
tokenizer (included in Docker), `token_count` measures and enforces the 700-token
ceiling. Local runs without the asset explicitly retain the estimate label.

Click/type results include before/after state fingerprints. Fingerprints cover
bounded document text, non-password form state, scroll/URL/title, open shadow roots,
and up to 16 frames. They are evidence of change, not proof of task completion.
Clicks without immediate visible changes produce a warning. Type verifies the
resulting value and skips submission if the value does not match. An action that
completed while its page closed/navigated reports that subsequent state capture
was unavailable; the action is not replayed.

## Validation and remaining work

The Chromium fixtures cover labels, password redaction, covered/hidden/disabled
elements, DOM replacement, unrelated page updates, superseded snapshots, tab and
generation isolation, shadow DOM, same/cross-origin frames, and observation limits.
The API integration test runs observe → type → click through the host tool registry,
HTTP client, sandbox FastAPI app, and real Chromium. Run them with the setup in
[the foundation guide](xbrowser-foundation.md).

The [execution layer](xbrowser-execution.md) now provides the nine-action model API,
bounded macros, observation compaction, state-aware repeat detection, and explicit
plan-step progress. Trusted task acceptance, domain/IP and redirect policy,
sensitive-form approvals, marked screenshots and optional local vision analysis
are implemented. Docker/Xvfb and real-model measurements are recorded in the
validation report.
