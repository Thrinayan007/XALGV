# Phase 0 single-agent baseline

Measured on November 9, 2026, before introducing specialist execution. The
source was the existing XBrowser working tree based on
`4d0cccdf9a615e40dc48a94cda0f70bc824c8e3c`, including the pending browser
improvements. This is a working-tree baseline. The source identifiers are:

- SHA-256 of `git diff --binary HEAD -- local_agent sandbox compose.yaml tests pyproject.toml eval`
  on the measured checkout: `7af660a01c718ad5866e0e88a126dab2a988f89a61e4951bc4e0ffdde9c55659`.
- SHA-256 of the then-untracked `local_agent/core/recovery.py`:
  `4fdde5f2f7ee3056a97e7536ed87ac39a9e07b8d7fc6bf61426211c06c5f72d3`.

The test host used Windows, Python 3.13.2, pytest 9.2.0, Playwright 1.64.1 with
Chromium Headless Shell 156.0.8078.4, Ollama 0.40.15, and an NVIDIA RTX 3050
Laptop GPU with 6,144 MiB VRAM. The model workload used `qwen3.5:9b` with
8,192 context tokens and temperature 0. Trials ran in order through the
unchanged single-agent `AgentLoop` against a loopback fixture, with a fresh
Chromium profile per trial and a shared Ollama service. The regression suite
and the beginning of the model evaluation overlapped on this host, so treat
the elapsed times as observed timings rather than isolated latency targets.

## Regression results

| Suite | Result | Wall time |
| --- | ---: | ---: |
| All pre-existing tests, including XBrowser Chromium fixtures | 240 passed | 143.43 s |
| New Phase 0 contract tests | 24 passed | 0.81 s |

The new contract files are additive. They do not change the single-agent run,
tool dispatch, Ollama integration, or browser server paths exercised by the baseline.

## Live single-agent evaluation

Each trial had deterministic acceptance checks that the harness reran after
the agent finished. Navigation checks the exact URL and fixture content.
The form trial also requires reference-based typing and clicking. The
five-capture trial checks all five 1280 × 720 PNGs and the exact macro sequence.

| Scenario | Result | Time | Model calls | Prompt tokens | Generated tokens |
| --- | --- | ---: | ---: | ---: | ---: |
| Navigation and observation | Passed | 73.49 s | 5 | 10,244 | 351 |
| Reference-based form interaction | Passed | 130.42 s | 12 | 33,075 | 979 |
| Five viewport captures in one macro | Passed | 73.70 s | 3 | 6,589 | 579 |

All three trials passed. The median elapsed time was 73.70 seconds. Across the
trials, the agent recovered from one rejected macro payload (HTTP 422). Ollama
reported 3,490,950,674 bytes of model VRAM residency at the end of the run,
approximately 3.25 GiB. That is a point-in-time reading. There was one trial per
scenario, so the results establish a comparison point for these fixtures.

The raw report contains timing, prompt and generation tokens, tool selection,
and independent verification for each trial.
[Machine-readable report](../traces/multi-agent-baseline/single-agent-model.json)

## Reproduction

From the repo root with Ollama running and `qwen3.5:9b` installed:

```powershell
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path (Get-Location) '.cache\playwright'
.\.venv\Scripts\python -m playwright install --only-shell chromium
.\.venv\Scripts\python -m pytest tests --ignore=tests/agents -q --tb=short --durations=10
.\.venv\Scripts\python -m pytest tests/agents -q
.\.venv\Scripts\python -m eval.xbrowser --model qwen3.5:9b --repeats 1 --timeout 240 --output .cache/multi-agent-baseline/single-agent-model.json
```

For the eventual release comparison, run multiple trials for each scenario and
separate cold and warm timings. Compare pass rate before latency. Measure peak
RAM and VRAM separately from resident model size. Multi-agent handoff failures
and scheduling overhead have no single-agent counterpart.
