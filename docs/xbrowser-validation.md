# XBrowser v1 validation report

Validated on 8 November 2026. The remaining v1 implementation and defined validation
gates are complete. Browser sessions, references, nine actions, macros, trusted task
checks, sandbox network/approval controls, real-token budgets, marked screenshots,
and optional local vision analysis are implemented.

| Gate | Recorded result |
| --- | --- |
| Full regression suite | 230 passed in 116.10 seconds |
| Ruff checks on XBrowser and host integration | Passed; whitespace/diff checks passed |
| Original real-model fixture suite | 9/9 passed: navigation, reference form, five captures; three repetitions each |
| Optimized capture workflow | 3/3 passed; exactly three model calls per trial |
| Capture correctness | One macro, correct ten-action order, five valid 1280×720 viewport PNGs |
| Optimized capture latency | Median 76.1s; nearest-rank p95 99.9s |
| Docker deployment | All 15 checks passed; deployed source hashes match local source |
| Display and viewing | Headful Chromium, 1280×720 Xvfb, noVNC HTML and live RFB WebSocket bridge passed |
| Real vision fallback | Qwen interpreted a marked PNG from the Docker browser |
| Observation budgeting | Official checksum-pinned Qwen tokenizer; 700-token limit enforced, including multilingual fixtures |
| Default-policy restoration | Temporary local fixture exception removed; environment reads and unauthenticated execution blocked |

The model was the locally installed Qwen 3.5 9B Q4_K_M model at 8192 context and
temperature zero. Hardware was an RTX 3050 Laptop GPU with 6 GB VRAM, with partial
CPU offload. The default planner/executor/vision profile now matches those settings.
Timings are specific to this machine and these tasks.

## Evidence

- [Aggregate machine-readable results](../traces/xbrowser-validation/validation-summary.json)
- [Full regression output](../traces/xbrowser-validation/pytest-final.txt)
- [Real-model task measurements](../traces/xbrowser-validation/model-report.json)
- [Optimized capture measurements](../traces/xbrowser-validation/capture-optimized-report.json)
- [Docker/display/source checks](../traces/xbrowser-validation/deployment-report.json)
- [Real vision result and token usage](../traces/xbrowser-validation/vision-deployment-report.json)
- [Restored normal configuration](../traces/xbrowser-validation/production-report.json)

The initial model trial exposed completion markers following explanation; parsing
was corrected and its earlier failed report retained. A separate optimization run
recorded an Ollama HTTP 500 while validation workloads overlapped; its report was
retained, and the final isolated three-trial capture run passed. The one-off public
HTTPS transport check also demonstrated that an obsolete live-page body assertion
correctly fails even when navigation succeeds. Public sites are not used for the
repeatable model fixture suite.

These are measured results on three controlled scenarios, not evidence of a 100%
open-web success rate. The sensitive-action heuristic cannot infer every site's
business semantics; GET side effects and approved SYSTEM code remain explicit
trust boundaries. See [security and verification](xbrowser-security.md).

## Deployment and use

The sandbox is running at [the local API](http://localhost:9000/docs) and
[noVNC](http://localhost:6080/vnc.html), using the default scope and strict default
network policy. Temporary test-service exceptions are not left enabled. Docker's
startup was repaired by backing up its two stale temporary sockets through WSL;
no Docker factory reset was used.

Ordinary browser work needs no administration secret. Sensitive browser actions
use reviewed, single-use approvals. Shell/Python tools now require SYSTEM authority
and sandbox administration credentials; this intentionally closes their former
workspace-write bypass. Use `--allow-execution` only for a run explicitly trusted
to execute arbitrary code. Administrative secrets should stay outside the workspace.

For deterministic completion, supply trusted checks with `--acceptance checks.json`.
Without criteria, the application explicitly records model-asserted completion as
`not_evaluated`. The exact acceptance checks determine what has actually been proven.

## Reproduction

```powershell
.\.venv\Scripts\python -m pip install -e '.[dev,sandbox]'
$env:PLAYWRIGHT_BROWSERS_PATH = "$PWD\.venv\playwright-browsers"
.\.venv\Scripts\python -m playwright install chromium
.\.venv\Scripts\python -m sandbox.xbrowser.token_budget --download
.\.venv\Scripts\python -m pytest tests -q
.\.venv\Scripts\python -m eval.xbrowser --model qwen3.5:9b --repeats 3
.\.venv\Scripts\python -m eval.xbrowser --scenarios five_captures --repeats 3 --output traces/captures.json
docker compose up -d --build sandbox
```

The deployment smoke script requires the exact fixture origin described in its
module documentation. Run it in a separate validation scope, then restore the
normal Compose environment. `--vision-model qwen3.5:9b` adds the real vision check.
