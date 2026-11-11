# XALGV

XALGV is a local computer-use agent powered by Ollama. It can browse with Chromium, capture evidence, and run verified workflows with Browser, Coding, Research, and Document specialists. The Python agent runs on the host. Docker supplies the browser sandbox and SearXNG search service.

## Setup on Windows

Install Python 3.11 or newer, Docker Desktop with Compose, and [Ollama](https://ollama.com/). Start Docker Desktop and Ollama. If Ollama is not already running, run `ollama serve` in a separate terminal.

From the repository root in PowerShell:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
ollama pull qwen3.5:9b
docker compose up -d --build
docker compose ps
.\.venv\Scripts\python -m local_agent.main health
```

On Linux or macOS, create the virtual environment with `python3 -m venv .venv` and use `./.venv/bin/python` in place of the Windows Python path.

The full requirements install includes runtime, development, browser test, RAG, and sandbox Python dependencies. Host dependency versions are maintained in `pyproject.toml`. The Docker image installs its own Python and system packages. To install just the host runtime, use `python -m pip install -e .` with the virtual environment active.

## Run a single agent

```powershell
$shot = "browser-$(Get-Date -Format yyyyMMdd-HHmmss).png"
.\.venv\Scripts\python -m local_agent.main run "Open https://example.com, save a viewport screenshot to /workspace/evidence/$shot, and describe the page using browser_screenshot with describe=true."
```

The sandbox's `/workspace` maps to this checkout's `workspace/` directory. Look for the screenshot under `workspace/evidence/` and watch the browser through [noVNC](http://127.0.0.1:6080/vnc.html?autoconnect=true&path=websockify).

Use `run --acceptance checks.json` when completion must pass deterministic checks. Shell and Python execution require `--allow-execution` and matching host and sandbox admin tokens. Keep credentials out of the workspace. See the [security guide](docs/xbrowser-security.md) for configuration.

## Run a multi-agent workflow

The caller supplies the trusted tasks, output paths, permissions, and verification criteria in a JSON contract. The bundled form example needs the local fixture setup in the [workflow guide](docs/multi-agent-workflows.md).

```powershell
.\.venv\Scripts\python -m local_agent.main multi-run "Inspect the form and generate a verified Python schema" --contract examples/form-schema-workflow.json
.\.venv\Scripts\python -m local_agent.main multi-inspect form_demo
```

Use a fresh run ID and matching artifact paths for a new execution. To continue an interrupted run within its original deadline, use `multi-resume <run_id>`. Review uncertain effects before confirming them. Workflow outputs are under `workspace/artifacts/<run_id>/`, and the journal is under `data/agent-runs/`.

## Configure models and services

- Change the model tags and context lengths in `DEFAULT_MODEL_ROLES` in [local_agent/config.py](local_agent/config.py). Pull any new Ollama tag before running. The planner and executor use the same tag by default to reduce reloads.
- The default local endpoints are Ollama on port **11434**, the sandbox API on **9000**, noVNC on **6080**, and SearXNG on **8080**. Set `XALGV_OLLAMA_URL`, `XALGV_SANDBOX_URL`, or `XALGV_SEARXNG_URL` to use different endpoints.
- If `ollama serve` reports that port 11434 is already in use, check whether the existing instance is healthy with `ollama list`.

## Test and benchmark

Install local headless Chromium for the fixture tests. The Docker sandbox already ships with Chromium.

```powershell
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path (Get-Location) '.cache\playwright'
.\.venv\Scripts\python -m playwright install --only-shell chromium
.\.venv\Scripts\python -m pytest tests -q
.\.venv\Scripts\python -m eval.multi_agent --repeats 3
```

The last command runs the paired live Qwen evaluation and writes its report under `.cache/multi-agent-release/`. See the [release guide](docs/multi-agent-release.md) for the fixture, measurements, and qualification criteria.

## Repository map

| Path | Purpose |
| --- | --- |
| `local_agent/core/` | Single-agent planning, execution, and verification |
| `local_agent/agents/` | Specialist runtime, orchestration, resource leases, and recovery |
| `sandbox/xbrowser/` | Chromium sessions, browser actions, network policy, and evidence |
| `examples/` | Trusted workflow contracts |
| `eval/` and `tests/` | Benchmarks and regression tests |
| `docs/` | [Browser architecture](docs/xbrowser-architecture.md) and [multi-agent design](docs/multi-agent-architecture.md) |

## Web UI Dashboard

Start the agent web interface locally:

```powershell
.\.venv\Scripts\python -m local_agent.ui
```

Open [http://localhost:7860](http://localhost:7860) to interact with the agent through a web interface, attach images, and monitor runs.

Stop the Docker services with `docker compose down`.
