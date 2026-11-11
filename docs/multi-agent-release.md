# Multi-agent release evaluation

The implementation covers the blueprint's Phases 0–7. The release evaluator
compares the existing single-agent loop against Research, Browser, Coding, and
Document specialists on the same controlled workload. It checks the source
manifest, browser navigation, captured form fields, Python schema, and cited
Markdown report. The fixture search server supplies repeatable results through
the actual SearXNG client, and XBrowser runs real Chromium.

## Measured paired smoke

Measured with `qwen3.5:9b` at 8,192 context tokens on the development machine.

| Mode | Verified | Wall time | Model calls | Total tokens | Sampled board VRAM |
| --- | --- | ---: | ---: | ---: | ---: |
| Single agent | Yes | 156.98 s | 8 | 21,129 | 4,321 MiB |
| Four specialists | Yes | 168.98 s | 14 | 21,757 | 4,375 MiB |

The four-specialist run took 7.6% longer on this pair. Its sampled host
process RSS peaked at approximately 133 MiB. Host RSS was unavailable for
the single-agent sample. RSS excludes the separately running Ollama process,
and VRAM is sampled board use rather than exclusive model allocation.
The tests and model trials overlapped on the host, so these timings are a
comparison point for the fixture. The final artifact verifier rechecked the
outputs from both live runs offline.

The smoke passes verification in both modes. The repeated-run release gate
requires at least three trials per mode, at least 90% multi-agent success,
success at least as high as the paired baseline, and median verified latency
no greater than 1.25 times the baseline. The recorded smoke falls short of
the sample-size requirement. The evaluator reports this separately from
fixture success.

[Measured smoke report](../traces/multi-agent-release/smoke-report.json)

## Reproduce the release checks

From the repository root with the configured Ollama model available:

```powershell
.\.venv\Scripts\python -m pip install -e '.[dev,sandbox]'
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path (Get-Location) '.cache\playwright'
.\.venv\Scripts\python -m playwright install --only-shell chromium
.\.venv\Scripts\python -m pytest tests -q
.\.venv\Scripts\python -m eval.multi_agent --repeats 3 --output .cache/multi-agent-release/report.json
```

The release suite includes resource contention and cancellation, workspace
scope enforcement, crash reconciliation, verified handoff, provenance
validation, and a deterministic four-specialist workflow. The live evaluator
alternates mode order across pairs, retains failed trials, and returns a
nonzero status if the repeat-run gate fails. Its measurements cover this
fixture workload. Production browsing and research depend on the configured
sandbox and search service.

The [workflow guide](multi-agent-workflows.md) covers operating the module
and reviewing interrupted runs.
