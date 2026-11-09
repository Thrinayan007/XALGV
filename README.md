# XALGV — Local Autonomous Computer-Using Agent

A local-first, sandboxed, multimodal computer-using agent with dynamic planning, tool execution, browser automation, document intelligence, vision, translation, and custom-model integration.

**One-sentence description:** A local, sandboxed agent that observes a computer environment, plans, acts through tools, verifies results, and adapts — running entirely on your laptop with open-source models served through Ollama.

## Architecture

XBrowser includes persistent sessions and snapshot-scoped element targeting. See
[session architecture and setup](docs/xbrowser-foundation.md) and
[observations, references, and current limits](docs/xbrowser-observations.md).
The [execution architecture](docs/xbrowser-architecture.md) and
[bounded workflow API](docs/xbrowser-execution.md) cover the nine actions, macros,
agent progress and budgets. See the [v1 completion and validation report](docs/xbrowser-validation.md)
for the implemented scope, recorded results, and operating limits.
Trusted completion criteria, network controls, approvals, token budgeting, and
visual fallback are documented in [XBrowser security and verification](docs/xbrowser-security.md).

```
User Goal → Planner → [Observe → Decide → Permission Check → Act → Verify] → Done?
                                                                      ↑              |
                                                                      +-- Adapt/Re-plan ←-+

Host Machine:                          Docker Sandbox:
├── Ollama (GPU, model serving)        ├── FastAPI Tool Server (port 9000)
├── Agent Core (Python)                ├── Xvfb + Openbox (headless GUI)
├── Model Manager (VRAM scheduler)     ├── Firefox/Chromium (browser)
├── Memory (SQLite)                    ├── noVNC (port 6080, watch live)
└── connects over HTTP ───────────────→└── /workspace volume
```

## Quick Start

### 1. Prerequisites

- Python 3.11+
- Docker & Docker Compose
- [Ollama](https://ollama.ai) with `qwen3.5:9b-q4_K_M` (or another model selected with `--model`)

### 2. Install

```bash
pip install -e ".[dev]"
```

### 3. Start Services

```bash
# Start SearXNG and the sandbox
docker compose up -d

# Pull the default model
ollama pull qwen3.5:9b-q4_K_M
```

### 4. Check Health

```bash
python -m local_agent.main health
```

### 5. Run the Agent

```bash
python -m local_agent.main run --model qwen3.5:9b-q4_K_M "Create a Python hello-world project with tests"
```

Pass any installed Ollama model tag with `--model`. This overrides the agent's
planner, executor, router, vision, and translation roles for that run; the
configured embedding model is left unchanged.

### Local prompt and screenshot UI

Start the browser sandbox and Ollama, then run:

```powershell
.\venv\Scripts\python.exe -m local_agent.main ui
```

Open [http://127.0.0.1:7860](http://127.0.0.1:7860). Choose an installed Ollama
model, enter a task, and optionally attach a PNG or JPEG screenshot (up to 8 MiB)
to ask a question about its visible text or content. The model can also call the
`image_question` tool on images saved under `workspace/uploads/`. Runs and their
tool results appear on the page. The UI binds to localhost by default.

### 6. Run Evaluation

```bash
python -m local_agent.main eval
python -m local_agent.main eval --category shell
```

### 7. Web Search

```bash
python -m local_agent.main search "local agent tool calling" --mode on
```

### 8. Watch the Agent Live

Open http://localhost:6080 in your browser to see the sandbox desktop via noVNC.

## Project Structure

```
├── local_agent/                # Main package
│   ├── config.py              # Configuration system
│   ├── models.py              # Ollama model manager with VRAM scheduling
│   ├── main.py                # CLI entry point
│   ├── web_search.py          # SearXNG search integration
│   ├── core/                  # Agent core
│   │   ├── loop.py            # ReAct control loop
│   │   ├── planner.py         # LLM-based plan generation
│   │   ├── verifier.py        # Post-action verification
│   │   └── state.py           # Mutable agent state
│   ├── tools/                 # Tool system
│   │   ├── base.py            # Abstract Tool base class
│   │   ├── registry.py        # Tool registry with permission gating
│   │   ├── shell.py           # Shell execution tool
│   │   ├── file_ops.py        # File read/write/list tools
│   │   └── python_exec.py     # Python code execution tool
│   ├── memory/                # Memory subsystem
│   │   ├── store.py           # SQLite persistent store
│   │   ├── working.py         # Rolling context window
│   │   └── checkpoint.py      # Checkpoint and resume
│   ├── security/              # Security & safety
│   │   ├── permissions.py     # 5-tier permission system
│   │   └── budget.py          # Step/time/repeat budget enforcement
│   └── sandbox/               # Sandbox communication
│       └── client.py          # HTTP client for the tool server
├── sandbox/                   # Docker sandbox definition
│   ├── Dockerfile             # Debian-based Linux workstation
│   ├── tool_server.py         # FastAPI tool server
│   └── entrypoint.sh          # Starts Xvfb, Openbox, noVNC, tool server
├── eval/                      # Evaluation harness
│   ├── harness.py             # Task runner and reporting
│   └── tasks/                 # Task suites (JSON)
│       ├── shell_tasks.json   # 5 shell/filesystem tasks
│       ├── file_tasks.json    # 5 file operation tasks
│       └── python_tasks.json  # 5 Python coding tasks
├── tests/                     # Unit tests
├── searxng/                   # SearXNG config
├── compose.yaml               # Docker Compose for sandbox + SearXNG
└── pyproject.toml             # Project metadata
```

## Design Principles

| Principle | Meaning |
|-----------|---------|
| Dynamic decisions, fixed scaffolding | The loop, tool schemas, permissions, and budgets are fixed and tested |
| Observe before and after acting | Every action is followed by verification |
| Structure first, pixels second | DOM/accessibility trees before screenshots |
| External verification | Check outcomes with code, not LLM judgement |
| Sandbox by default | Agent never runs on the host |
| Local-first | Inference, memory, documents stay local |
| One model at a time | VRAM-aware scheduler swaps models |
| Measure everything | Eval harness runs after every change |

## Permission Tiers

| Tier | Level | Auto-approved | Examples |
|------|-------|---------------|----------|
| READ_ONLY | 0 | ✓ | Read files, list dirs, screenshots |
| WORKSPACE_WRITE | 1 | ✓ | Write/delete in /workspace |
| NETWORK | 2 | ✗ | Web search, fetch pages |
| SYSTEM | 3 | ✗ | Install packages, change config |
| DESTRUCTIVE | 4 | ✗ (always needs human) | rm -rf, drop tables |

## Running Tests

```bash
python -m pytest tests/ -v
```

## Roadmap

- [x] **Phase 0:** Environment, Ollama, tool-calling hello world
- [x] **Phase 1:** Core loop, sandbox, shell/file/Python tools, eval harness
- [ ] **Phase 2:** Browser automation (Playwright), SearXNG integration, RAG
- [ ] **Phase 3:** GUI desktop (Xvfb), VLM, LibreOffice
- [ ] **Phase 4:** Translation, speech, image generation, VRAM scheduler
- [ ] **Phase 5:** Skill library, memory, fine-tuning, red teaming, UI
