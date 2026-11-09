# XALGV multi-agent architecture

Phase 0 defines the interfaces for one orchestrator and four specialist roles.
Phases 1 and 2 implement the Browser and Coding adapters and sequential graph
execution. Phase 3 adds inference coordination and resource leases. Phase 4
adds a versioned workflow journal and verified artifact handoffs. Phase 5
adds bounded recovery and operator-reviewed reconciliation. Phase 6 adds
Research and Document, and Phase 7 adds the paired evaluation and release
gates. See
[the workflow guide](multi-agent-workflows.md) for the opt-in command and its
current operational boundaries.

## Contracts and lifecycle

The version 1 Python contracts live in `local_agent.agents.base`. The same
models generate JSON Schema via `model_json_schema()`. Inbound JSON is parsed
with `model_validate_json()`. In-process callers pass typed values. Unknown
fields, implicit numeric coercions, and unsupported versions are rejected.
Collections within the contracts are immutable.

| Boundary | Contract and authority |
| --- | --- |
| Trusted configuration → registry | `AgentDefinition` contains the role, system prompt, tool allowlist, and ceilings for steps and context. Configuration supplies definitions. |
| Coordinator → specialist | `AgentTask` contains stable run/task IDs, assigned role, objective, dependencies, verified artifact references, declared outputs, step and time limits, attempt limit, and typed acceptance checks. The coordinator authenticates and admits the task before dispatch. |
| Specialist → coordinator | `AgentResult` binds a claimed outcome, output manifest, counters, and an optional error code to a role and attempt. `matches()` checks task identity and declared output paths. The runtime records usage and hashes independently. |
| Verifier → coordinator | `VerificationReport` binds one outcome per check to a run, task, attempt, and hash of the complete ordered check list. `accepts()` checks that binding and requires every check to pass. The coordinator accepts reports only from its verifier. |
| Coordinator → final caller | The workflow result includes the original goal, final status, accepted outputs, unmet requirements, and separate workflow verification. Workflow criteria come from the trusted caller. |

The coordinator owns transitions. `TaskState.allows()` validates lifecycle
edges. It supplies no permission to perform them.

| State | Entry rule |
| --- | --- |
| Pending | Admitted to a validated graph. |
| Ready | Every dependency is verified complete, and retry and policy gates permit execution. |
| Running | The scheduler reserved resources, assigned the next attempt, and persisted dispatch intent. |
| Verifying | The attempt reported success, or reconciliation found evidence worth checking. |
| Completed | An authenticated verifier report passed every required check for the active attempt. Persist the report and completion together before releasing dependents. |
| Blocked | A dependency, policy gate, or human decision prevents execution. The coordinator can reassess after the blocker changes. |
| Failed or cancelled | Terminal. Retain attempts, partial artifacts, and error evidence. |
| Outcome unknown | An interrupted operation may have produced an external effect. Move to verification or fail after reconciliation. Direct retry is forbidden. |

A recoverable running or verifying attempt can return to ready only after
the coordinator records a safe retry decision and confirms the attempt limit,
remaining budget, and absence of unresolved side effects. Replanning changes
only unstarted work. It preserves verified completions, prior failures, original
requirements, and the effective budget. A worker can suggest work through its
result summary. Only the coordinator can create tasks, at delegation depth one.

## Interfaces for later phases

| Component | Input → output and failure behavior |
| --- | --- |
| Registry | Trusted definitions → one immutable definition per role. Reject duplicate and unknown roles. |
| Runtime | Admitted task, attempt ID, effective tool set, leased resources, and isolated context → `AgentResult`. Adapt the current `AgentLoop`. Propagate cancellation and report uncertain side effects explicitly. |
| Task graph | Bounded task list and workflow budget → admitted DAG. Reject duplicate IDs, cross-run edges, cycles, missing dependencies, missing roles, conflicting outputs, and work outside the grant before dispatch. Initial cap is 32 tasks. |
| Scheduler | Admitted DAG and current records → next ready attempt or a blocked/terminal state. Dispatch one attempt at a time in v1. Reserve budgets atomically with the attempt before handing work to the runtime. |
| Inference scheduler | Run/task/attempt identity, role, messages, output limit, and deadline → model response and measured usage. One queue owns both loading and inference. Expired or cancelled work releases its place. |
| Resource manager | Resource identity, run/task/attempt owner, deadline, and requested mode → exclusive lease or denial. Release by matching lease ID. Fence stale owners and reconcile interrupted operations before reassigning resources. |
| Checkpoint store | Graph revision, attempt identity, transition, consumed budget, verification and resource ownership → durable SQLite record. Resume by reconciling pending attempts and artifacts before dispatch. |
| Handoff | Verified manifest entries and bounded summaries → dependency inputs. Verify the producer, path, size, checksum and run on handoff. Upstream text remains untrusted. |

## Permission and resource ownership

The effective tool set is the intersection of trusted user permissions, the run
grant, the agent definition, and sandbox policy. Compute it before advertising
tool schemas and enforce it again at dispatch. Delegation never raises the
permission tier. Existing browser approvals and SYSTEM credentials stay on
trusted control paths. The table defines intended role defaults for the later
runtime rather than enabling them in the current CLI.

| Role | Default work | Extra authority |
| --- | --- | --- |
| Browser | XBrowser navigation, observation, interaction, and evidence capture. | Holds one exclusive browser session lease. Risky operations retain sandbox approval requirements. |
| Coding | Read approved inputs and write declared source and reports. | Shell and Python execution require explicit SYSTEM permission and sandbox authorization. |
| Research | Search and read approved sources and emit provenance. | Browser interaction requires a separate Browser task. |
| Document | Read verified inputs and write declared deliverables. | Rendering and external publication need separately configured capabilities. |

Only the lease owner can change tabs or browser state. The verifier may read the
same session while the lease is still held, before another attempt can mutate
it. The run budget owns tool steps, time, and tokens. Child reservations can
reduce their grant, and actual usage is reconciled against the parent. Model
accounting comes from the scheduler and tool accounting comes from dispatch.

Task outputs use `artifacts/<run_id>/<task_id>/<file>`. Each expected output
has a trusted artifact check. Dependencies receive only verified artifact
references and scoped read access. The contracts validate path syntax and logical
ownership. The runtime must also enforce resolved-path confinement, reject
symlink escapes and writes to dependency artifacts, and verify manifests against
the filesystem. String validation supplies no filesystem isolation. Likewise,
working memory is private to the attempt. The coordinator receives a structured
result, not the worker's conversation history.

## Compatibility and rollout

1. Keep `xalgv run`, `--acceptance`, existing tool names, approval paths, and
   current SQLite tables working as they do today. Phase 0 adds importable
   contracts and measurements without changing these entry points.
2. Reuse `AgentLoop`, `ToolRegistry`, `ModelManager`, `TaskVerifier`, and the
   XBrowser sandbox in the specialist adapter. Keep per-attempt context and tool
   registries separate. Task acceptance payloads use existing verifier formats.
3. Add orchestration through an explicit opt-in entry point once the Browser
   and Coding adapters pass isolation tests. Persist orchestration state in
   additive tables and leave existing single-agent run records readable.
4. Compare both modes against the same fixtures, criteria, model, context,
   hardware, and cold/warm load conditions. Retain failures and distinguish
   action success, task verification, and workflow verification in reports.

See [the Phase 0 baseline](multi-agent-baseline.md) for the comparison point.
