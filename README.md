# XALGV — Local Autonomous Computer-Using Agent

A local-first, sandboxed, multimodal computer-using agent with dynamic planning, tool execution, browser automation, document intelligence, vision, translation, and custom-model integration.

**One-sentence description:** A local, sandboxed agent that observes a computer environment, plans, acts through tools, verifies results, and adapts — running entirely on your laptop with open-source models served through Ollama.

## Architecture

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
- [Ollama](https://ollama.ai) with `qwen3:8b` (or configure another model)

### 2. Install

```bash
pip install -e ".[dev]"
```

### 3. Start Services

```bash
# Start SearXNG and the sandbox
docker compose up -d

# Pull the default model
ollama pull qwen3:8b
```

### 4. Check Health

```bash
python -m local_agent.main health
```

### 5. Run the Agent

```bash
python -m local_agent.main run "Create a Python hello-world project with tests"
```

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

## Grounded Web Research

For a research task, the agent can navigate to a source, extract its readable
text and metadata, identify linked PDFs, and assess transparent quality signals
(author, publication date, canonical URL, length, and outbound references).
Quality scores are heuristics, not factual-accuracy guarantees. Public PDFs are
downloaded only into the sandbox workspace (25 MB maximum), then ingested with
`ingest_document` and retrieved through `query_documents` with source/page
citations.

The agent never solves or bypasses CAPTCHAs. It pauses for human completion
when one is detected. Credentials are supplied only by environment-variable
references configured in the sandbox; passwords must never be included in a
prompt or a tool call. Logging in and downloading PDFs require elevated
permission, while form submission always requires explicit human approval and
the per-action confirmation phrase `I_CONFIRM_SUBMIT`.

After changing the sandbox, rebuild it before use:

```bash
docker compose up -d --build sandbox
```

## Roadmap

- [x] **Phase 0:** Environment, Ollama, tool-calling hello world
- [x] **Phase 1:** Core loop, sandbox, shell/file/Python tools, eval harness
- [ ] **Phase 2:** Browser automation (Playwright), SearXNG integration, RAG
- [ ] **Phase 3:** GUI desktop (Xvfb), VLM, LibreOffice
- [ ] **Phase 4:** Translation, speech, image generation, VRAM scheduler
- [ ] **Phase 5:** Skill library, memory, fine-tuning, red teaming, UI
