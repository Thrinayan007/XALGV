"""XALGV — Local Autonomous Computer-Using Agent.

Main CLI entry point.  Provides commands for:
* ``run``  — Execute the agent with a goal.
* ``eval`` — Run the evaluation harness.
* ``search`` — Perform a web search (Module 1).
* ``health`` — Check connectivity to Ollama and the sandbox.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from local_agent.config import AgentConfig

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("xalgv")


# ── Run command ──────────────────────────────────────────────────────────

async def cmd_run(args: argparse.Namespace) -> int:
    """Run the agent with a goal."""
    from local_agent.core.loop import AgentLoop
    from local_agent.core.task_verification import TaskVerifier
    from local_agent.memory.store import MemoryStore
    from local_agent.models import ModelManager
    from local_agent.sandbox.client import SandboxClient
    from local_agent.security.permissions import PermissionPolicy, PermissionTier
    from local_agent.tools.browser import browser_tools
    from local_agent.tools.file_ops import ListFilesTool, ReadFileTool, WriteFileTool
    from local_agent.tools.image_question import ImageQuestionTool
    from local_agent.tools.office import office_tools
    from local_agent.tools.python_exec import PythonExecTool
    from local_agent.tools.rag import IngestDocumentTool, QueryDocumentsTool
    from local_agent.tools.registry import ToolRegistry
    from local_agent.tools.shell import ShellExecTool
    from local_agent.tools.web_search import WebSearchTool

    config = AgentConfig.from_env()
    model_override = getattr(args, "model", None)
    if model_override:
        config.use_generation_model(model_override)
        logger.info("Using Ollama model %s for agent generation roles", model_override)
    config.ensure_dirs()

    sandbox = SandboxClient(config.sandbox_url)
    model_mgr = ModelManager(config)
    memory = MemoryStore(config.db_path)

    # Initialise RAG pipeline (optional — degrades gracefully).
    rag_pipeline = None
    try:
        from local_agent.rag import RAGPipeline, VectorStore
        rag_pipeline = RAGPipeline(
            model_manager=model_mgr,
            vector_store=VectorStore(config.data_dir / "vectordb"),
        )
    except Exception:  # noqa: BLE001 - Optional RAG can fail during import or initialization.
        logger.warning("RAG pipeline unavailable — document tools will return errors.")

    # Build tool registry.
    allow_execution = getattr(args, "allow_execution", False)
    if allow_execution and not sandbox._admin_token:
        raise ValueError("--allow-execution requires XALGV_SANDBOX_ADMIN_TOKEN and matching SANDBOX_ADMIN_TOKEN in the container")
    policy = PermissionPolicy(auto_approve_up_to=PermissionTier.SYSTEM if allow_execution else PermissionTier.NETWORK)
    registry = ToolRegistry(policy)
    registry.register_many(
        # Phase 1 — file & shell.
        ShellExecTool(sandbox),
        ReadFileTool(sandbox),
        WriteFileTool(sandbox),
        ListFilesTool(sandbox),
        PythonExecTool(sandbox),
        WebSearchTool(
            base_url=config.searxng_base_url,
            trace_dir=str(config.trace_dir),
        ),
        # Phase 2 — browser automation.
        *browser_tools(sandbox, vision_model=model_mgr),
        ImageQuestionTool(config.workspace_dir, model_mgr, sandbox),
        # Phase 2 — RAG / document intelligence.
        IngestDocumentTool(rag_pipeline),
        QueryDocumentsTool(rag_pipeline),
        # Phase 3 — Office automation.
        *office_tools(sandbox),
    )

    loop = AgentLoop(config, model_mgr, registry, memory, task_verifier=TaskVerifier(sandbox))

    try:
        acceptance_path = getattr(args, "acceptance", None)
        checks = None
        milestones = None
        if acceptance_path:
            from pathlib import Path
            data = json.loads(Path(acceptance_path).read_text(encoding="utf-8"))
            checks = data.get("checks") if isinstance(data, dict) else data
            milestones = data.get("milestones") if isinstance(data, dict) else None
            if checks is None and milestones is None:
                raise ValueError("Acceptance file must contain checks or milestones")
        result = await loop.run(args.goal, acceptance_checks=checks, milestones=milestones)
        print(f"\n{'='*60}")
        print(f"Run ID:  {result.run_id}")
        print(f"Status:  {result.status.value}")
        print(f"Task checks: {result.metadata.get('task_verification', {}).get('status', 'not_evaluated')}")
        print(f"Steps:   {len(result.steps)}")
        print(f"Summary: {result.summary}")
        print(f"{'='*60}")
        return 0 if result.status.value == "completed" else 1
    finally:
        await sandbox.close()
        await model_mgr.close()


# ── Eval command ─────────────────────────────────────────────────────────

async def cmd_eval(args: argparse.Namespace) -> int:
    """Run the evaluation harness."""
    from eval.harness import EvalHarness
    from local_agent.core.loop import AgentLoop
    from local_agent.core.task_verification import TaskVerifier
    from local_agent.memory.store import MemoryStore
    from local_agent.models import ModelManager
    from local_agent.sandbox.client import SandboxClient
    from local_agent.security.permissions import PermissionPolicy, PermissionTier
    from local_agent.tools.browser import browser_tools
    from local_agent.tools.file_ops import ListFilesTool, ReadFileTool, WriteFileTool
    from local_agent.tools.image_question import ImageQuestionTool
    from local_agent.tools.office import office_tools
    from local_agent.tools.python_exec import PythonExecTool
    from local_agent.tools.rag import IngestDocumentTool, QueryDocumentsTool
    from local_agent.tools.registry import ToolRegistry
    from local_agent.tools.shell import ShellExecTool
    from local_agent.tools.web_search import WebSearchTool

    config = AgentConfig.from_env()
    config.ensure_dirs()

    sandbox = SandboxClient(config.sandbox_url)
    model_mgr = ModelManager(config)
    memory = MemoryStore(config.db_path)

    # Initialise RAG pipeline (optional — degrades gracefully).
    rag_pipeline = None
    try:
        from local_agent.rag import RAGPipeline, VectorStore
        rag_pipeline = RAGPipeline(
            model_manager=model_mgr,
            vector_store=VectorStore(config.data_dir / "vectordb"),
        )
    except Exception:  # noqa: BLE001 - Optional RAG can fail during import or initialization.
        logger.warning("RAG pipeline unavailable — document tools will return errors.")

    allow_execution = getattr(args, "allow_execution", False)
    if allow_execution and not sandbox._admin_token:
        raise ValueError("--allow-execution requires configured sandbox administration credentials")
    policy = PermissionPolicy(auto_approve_up_to=PermissionTier.SYSTEM if allow_execution else PermissionTier.NETWORK)
    registry = ToolRegistry(policy)
    registry.register_many(
        # Phase 1 — file & shell.
        ShellExecTool(sandbox),
        ReadFileTool(sandbox),
        WriteFileTool(sandbox),
        ListFilesTool(sandbox),
        PythonExecTool(sandbox),
        WebSearchTool(
            base_url=config.searxng_base_url,
            trace_dir=str(config.trace_dir),
        ),
        # Phase 2 — browser automation.
        *browser_tools(sandbox, vision_model=model_mgr),
        ImageQuestionTool(config.workspace_dir, model_mgr, sandbox),
        # Phase 2 — RAG / document intelligence.
        IngestDocumentTool(rag_pipeline),
        QueryDocumentsTool(rag_pipeline),
        # Phase 3 — Office automation.
        *office_tools(sandbox),
    )

    agent_loop = AgentLoop(config, model_mgr, registry, memory, task_verifier=TaskVerifier(sandbox))
    harness = EvalHarness()

    try:
        report = await harness.run_suite(agent_loop, category=args.category)
        output = config.trace_dir / "eval_report.json"
        harness.save_report(report, output)
        print(f"\n{'='*60}")
        print("Evaluation Report")
        print(f"{'='*60}")
        print(f"Total:   {report.total_tasks}")
        print(f"Passed:  {report.passed}")
        print(f"Failed:  {report.failed}")
        print(f"Errors:  {report.errors}")
        print(f"Rate:    {report.success_rate:.1%}")
        print(f"Time:    {report.wall_time_seconds:.1f}s")
        print(f"Report:  {output}")
        print(f"{'='*60}")
        return 0
    finally:
        await sandbox.close()
        await model_mgr.close()


# ── Search command (legacy Module 1) ────────────────────────────────────

def cmd_search(args: argparse.Namespace) -> int:
    """Run a web search via SearXNG."""
    from local_agent.web_search import (
        SearchMode,
        SearchPolicyError,
        SearchRequest,
        SearchUnavailableError,
        SearxNGClient,
        TraceStore,
        WebSearchService,
    )

    service = WebSearchService(SearxNGClient(), TraceStore())
    try:
        response = service.execute(
            SearchRequest(query=args.query, max_results=args.max_results, task_id=args.task_id),
            SearchMode(args.mode),
            args.approved,
        )
    except (SearchUnavailableError, SearchPolicyError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({
        "request_id": response.request_id,
        "query": response.query,
        "results": [r.__dict__ for r in response.results],
    }, indent=2))
    return 0


def cmd_ui(args: argparse.Namespace) -> int:
    """Start the local task and screenshot-question UI."""
    import uvicorn

    from local_agent.ui import app

    print(f"XALGV UI: http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


# ── Health command ───────────────────────────────────────────────────────

async def cmd_health(args: argparse.Namespace) -> int:
    """Check connectivity to Ollama and the sandbox."""
    from local_agent.models import ModelManager
    from local_agent.sandbox.client import SandboxClient

    config = AgentConfig.from_env()
    model_mgr = ModelManager(config)
    sandbox = SandboxClient(config.sandbox_url)

    ollama_ok = await model_mgr.health_check()
    sandbox_ok = await sandbox.health_check()

    print(f"Ollama  ({config.ollama_base_url}): {'✓ OK' if ollama_ok else '✗ UNREACHABLE'}")
    print(f"Sandbox ({config.sandbox_url}):     {'✓ OK' if sandbox_ok else '✗ UNREACHABLE'}")

    await model_mgr.close()
    await sandbox.close()
    return 0 if (ollama_ok and sandbox_ok) else 1


# ── CLI entry point ─────────────────────────────────────────────────────

def cli_main() -> int:
    parser = argparse.ArgumentParser(
        prog="xalgv",
        description="XALGV — Local Autonomous Computer-Using Agent",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # run
    run_parser = subparsers.add_parser("run", help="Execute the agent with a goal.")
    run_parser.add_argument("goal", help="The goal for the agent to accomplish.")
    run_parser.add_argument(
        "--model",
        help="Ollama model tag for planner, executor, router, vision, and translation roles (embeddings keep their configured model).",
    )
    run_parser.add_argument("--acceptance", help="Trusted JSON file of deterministic acceptance checks.")
    run_parser.add_argument("--allow-execution", action="store_true", help="Trust shell/Python SYSTEM tools for this run; requires configured sandbox admin credentials.")

    workflow_parser = subparsers.add_parser("multi-run", help="Execute a verified specialist workflow.")
    workflow_parser.add_argument("goal")
    workflow_parser.add_argument("--contract", required=True, help="Trusted workflow JSON file.")
    inspect_parser = subparsers.add_parser("multi-inspect", help="Read one workflow's durable journal.")
    inspect_parser.add_argument("run_id")
    resume_parser = subparsers.add_parser("multi-resume", help="Resume a workflow from its durable journal.")
    resume_parser.add_argument("run_id")
    resume_parser.add_argument("--confirm-outcome", action="append", default=[], metavar="TASK",
                               help="Confirm you independently reviewed the effects of this uncertain task.")

    # eval
    eval_parser = subparsers.add_parser("eval", help="Run the evaluation harness.")
    eval_parser.add_argument("--category", help="Only run tasks of this category.")
    eval_parser.add_argument("--allow-execution", action="store_true", help="Trust shell/Python SYSTEM tools for evaluation; requires sandbox admin credentials.")

    # search
    search_parser = subparsers.add_parser("search", help="Run a web search via SearXNG.")
    search_parser.add_argument("query", help="Search query.")
    search_parser.add_argument("--mode", choices=["off", "ask", "on"], default="off")
    search_parser.add_argument("--approved", action="store_true")
    search_parser.add_argument("--max-results", type=int, default=5)
    search_parser.add_argument("--task-id", default="cli")

    # health
    subparsers.add_parser("health", help="Check Ollama and sandbox connectivity.")

    # local UI
    ui_parser = subparsers.add_parser("ui", help="Start the local prompt and screenshot question UI.")
    ui_parser.add_argument("--host", default="127.0.0.1", help="Interface to bind (default: localhost only).")
    ui_parser.add_argument("--port", type=int, default=7860, help="Port for the local UI (default: 7860).")

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.command == "run":
        return asyncio.run(cmd_run(args))
    elif args.command in {"multi-run", "multi-resume"}:
        from local_agent.agents.cli import run_workflow

        return asyncio.run(run_workflow(args))
    elif args.command == "multi-inspect":
        from local_agent.agents.cli import inspect_workflow

        return inspect_workflow(args)
    elif args.command == "eval":
        return asyncio.run(cmd_eval(args))
    elif args.command == "search":
        return cmd_search(args)
    elif args.command == "health":
        return asyncio.run(cmd_health(args))
    elif args.command == "ui":
        return cmd_ui(args)
    return 1


if __name__ == "__main__":
    raise SystemExit(cli_main())
