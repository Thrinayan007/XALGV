# Browser and Coding workflows

`multi-run` is the opt-in entry point for the initial sequential workflow
runtime. The single-agent `run` command is still available.

```powershell
.\.venv\Scripts\python -m local_agent.main multi-run "Inspect the form, generate a Python schema, and test it" --contract examples/form-schema-workflow.json
```

The example contract uses the local fixture at
`http://127.0.0.1:8765/first.html`, as seen **from Chromium's sandbox**.
To use the example with the default Docker deployment, copy
`tests/xbrowser/fixtures/first.html` into `workspace/workflow-fixture`, set
`XBROWSER_LOCAL_ORIGINS=http://127.0.0.1:8765` for the test deployment, and start
the fixture server inside the sandbox:

```powershell
New-Item -ItemType Directory -Force workspace/workflow-fixture
Copy-Item tests/xbrowser/fixtures/first.html workspace/workflow-fixture/
$env:XBROWSER_LOCAL_ORIGINS = 'http://127.0.0.1:8765'
docker compose up -d sandbox
docker compose exec -d sandbox python3 -m http.server 8765 --bind 127.0.0.1 --directory /workspace/workflow-fixture
```

Start Ollama and install the configured model before running the workflow.
Use a new run ID for each invocation. For the example, replace `form_demo`
throughout a copy of the contract so its artifact paths match the new ID.
After the fixture test, restore your normal local-origin setting and recreate
the sandbox to remove the fixture server.

## Contract and execution

The caller defines task slots, specialist assignments, output paths, required
capabilities, budgets, and acceptance checks in trusted JSON. An optional
`tool_allowlist` narrows a task below the specialist's default permissions. The planner adds
approaches and dependencies within those slots. It cannot replace checks,
change permissions, or expand the task list. A malformed initial plan gets
one correction. After a known failure, one bounded replan can adjust
unstarted work while retaining existing outcomes and required dependencies.

A task's `bindings` declare which upstream output it consumes. The scheduler
adds the producer dependency even if the model omits it, then fills in the
artifact hash from the verified producer's manifest. It checks that the
artifact is intact before dispatch. The Coding adapter checks the hash again
when it reads the artifact. Files are scoped to declared paths.

The Browser specialist uses the managed Chromium session and can export
observed form fields to a declared JSON file. Coding reads verified inputs and
writes declared text files. For the example, `validate_python_schema` parses
the generated Python module and compares its literal `SCHEMA` dictionary with
the exported fields. The trusted `python_schema` acceptance check separately
tests the expected names and types. This is a static test, so the workflow
can validate generated Python without executing it or granting SYSTEM access.

Task results, independent verification, and final checks are printed and
stored under `data/agent-runs/<run_id>/workflow.json`. The separate
`data/agent-runs/workflows.sqlite3` journal records admitted graphs, transitions,
attempts, verification, artifact manifests, handoffs, and resource scopes.
Each task attempt has a private SQLite memory file. Outputs live under
`workspace/artifacts/<run_id>`.
A failed prerequisite blocks its dependents. An interrupted attempt with
possible side effects is recorded as outcome unknown. Automatic retry is
limited to a failed attempt that recorded zero tool actions and still has a
retry allowance.

Inspect the journal without starting Ollama or Chromium:

```powershell
.\.venv\Scripts\python -m local_agent.main multi-inspect form_demo
```

## Scheduling and resource ownership

Each workflow dispatches one task at a time. Workflows can share a sandbox.
Browser tasks take an exclusive, expiring lease on the sandbox API session
and hold it through independent verification. Requests from other API clients
receive HTTP 423 while the lease is held. Lease tokens stay on the control
path and are absent from the journal. The sandbox drains in-flight browser
requests before changing owners. The lease serializes use of the existing
browser profile. Use separate sandbox scopes when workflows require separate
cookies or login state. Interactive noVNC input sits outside the API lease.

Ollama loading, model changes, chat, and embedding share an inference slot
among XALGV processes that use the same data directory and endpoint. A
bounded queue limits waiters. Cancelling an admitted caller keeps the slot
occupied until the backend request returns or reaches its transport timeout.
Other applications that call Ollama directly are outside this coordination.

`max_model_tokens` can be set at workflow and task level. Before a request,
the scheduler reserves the configured context plus the generation cap.
Successful requests reconcile against reported token usage. Failed or
interrupted requests with missing usage retain the reservation. The existing
step and wall-time grants still apply. Workspace locks use the shared
workspace and task identity, while tool dispatch restricts each task to its
declared files.

The coordinator reads upstream manifests from the verified SQLite artifact
index. Workers see a compact list of producer IDs, input names, and file paths
and read selected files through the checksum-checking tool. The journal
retains interrupted states for inspection and recovery.

## Resume and reconcile

```powershell
.\.venv\Scripts\python -m local_agent.main multi-inspect form_demo
.\.venv\Scripts\python -m local_agent.main multi-resume form_demo
```

Resume restores the admitted graph and original deadline, token charges, and
attempt limits. It preserves verified completions and rechecks artifact hashes
before handoff. An abandoned dispatch or verification becomes
`outcome_unknown`; its dependents wait. The coordinator may retry a failed
attempt within its grant when the recorded tool count is zero. External
effects with an uncertain outcome require review.

After independently confirming that a specific uncertain task produced its
intended effects, ask the verifier to reconcile that task against its original
checks:

```powershell
.\.venv\Scripts\python -m local_agent.main multi-resume form_demo --confirm-outcome inspect
```

The command verifies the current evidence under a task lease and can admit
pending dependents. It never redispatches the uncertain attempt. A failed check
leaves the task uncertain. Journals created before durable deadlines were
introduced remain inspectable. Resume requires a recorded, unexpired deadline.
The SQLite journal is the authoritative result after resume.

## Research and Document specialists

Research uses the configured local SearXNG endpoint to export an indexed
source manifest. Each record includes its provider rank, title, URL, excerpt,
and retrieval time. This is source discovery. Browser tasks handle page
interaction when the workflow needs it. Document tasks can read verified
dependencies and write Markdown under declared paths. The Markdown verifier
checks parsed headings and links against the verified source manifest and
rejects raw HTML and embedded images. Citation checks verify provenance and
document structure; verifying factual interpretation remains a separate task.

See [release evaluation](multi-agent-release.md) for the four-specialist
fixture and comparison with single-agent execution.

Rebuild the sandbox image to install the browser lease API:

```powershell
docker compose up -d --build
```

## Regression fixture

The integration test serves the page locally, uses the real XBrowser API and
Chromium, and scripts model replies to exercise permission and handoff
boundaries deterministically.

One live `qwen3.5:9b` trial with the example contract, a local fixture, and
headless Chromium passed both task checks and final verification in 90.37
seconds with eight model calls, including planning. An earlier trial with
the full browser toolkit timed out after repeatedly placing the standalone
export tool inside a macro. The example now grants only the two browser
tools it needs, and macro preflight rejects unsupported steps locally.

```powershell
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path (Get-Location) '.cache\playwright'
.\.venv\Scripts\python -m pytest tests/agents tests/xbrowser/test_multi_agent.py -q
```
