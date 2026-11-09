"""Repeatable single-agent versus four-specialist evaluation on local fixtures."""

import argparse
import asyncio
import json
import shutil
import statistics
import threading
import time
from contextlib import contextmanager
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

import httpx

from local_agent.agents.artifacts import TaskFiles
from local_agent.agents.base import AgentTask, PageCheck
from local_agent.agents.definitions import default_registry
from local_agent.agents.orchestrator import Orchestrator
from local_agent.agents.runtime import SpecialistRuntime
from local_agent.agents.task_graph import WorkflowRequest
from local_agent.agents.tools import BrowserExportTool, ResearchExportTool
from local_agent.config import AgentConfig, ModelRoleConfig
from local_agent.core.loop import AgentLoop
from local_agent.memory.store import MemoryStore
from local_agent.models import ModelManager
from local_agent.sandbox.client import SandboxClient
from local_agent.security.permissions import PermissionPolicy, PermissionTier
from local_agent.tools.base import VerificationResult, VerificationStatus
from local_agent.tools.browser import BrowserOpenTool
from local_agent.tools.file_ops import ReadFileTool, WriteFileTool
from local_agent.tools.registry import ToolRegistry
from local_agent.tools.web_search import WebSearchTool
from sandbox import browser_server, tool_server
from sandbox.xbrowser import BrowserSession
from sandbox.xbrowser.safety import BrowserPolicy

SOURCE = "https://docs.python.org/3/tutorial/"
GOAL = "Collect a Python documentation source, inspect a form, generate a matching Python schema, and write a cited Markdown report."


@contextmanager
def fixture_site():
    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            if not self.path.startswith("/search?"):
                return super().do_GET()
            payload = json.dumps({"results": [{
                "url": SOURCE, "title": "The Python Tutorial", "content": "Python is a programming language.",
            }]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(
        Handler, directory=str(Path(__file__).parent / "fixtures"),
    ))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def contract(origin, run_id, *, single=False):
    def path(role, name):
        return f"artifacts/{run_id}/{'single' if single else role}/{name}"

    sources = path("research", "sources.json")
    fields = path("browser", "fields.json")
    schema = path("coding", "schema.py")
    report = path("document", "report.md")
    url = origin + "/profile.html"
    checks = [
        {"kind": "source_manifest", "path": sources, "required_hosts": ["docs.python.org"]},
        {"kind": "url", "expected": url},
        {"kind": "artifact", "path": fields, "contains": '"Display name"'},
        {"kind": "python_schema", "path": schema, "fields": [{"name": "Display name", "type": "str"}]},
        {"kind": "markdown", "path": report, "headings": ["Summary", "Sources"],
         "sources_path": sources, "min_citations": 1},
    ]
    objectives = (
        f"Use research_export to search for The Python Tutorial and save results to {sources}.",
        f"Use browser_open on {url}, then call browser_export separately to save form fields to {fields}.",
        f"Read {fields} and write a Python module at {schema} with one literal SCHEMA dictionary mapping observed names to str, bool, or float strings.",
        f"Read the verified source, form, and schema artifacts. Write {report} as Markdown with headings Summary and Sources, and at least one Markdown link using the source manifest URL.",
    )
    if single:
        return AgentTask.model_validate_json(json.dumps({
            "run_id": run_id, "task_id": "single", "agent": "browser", "objective": "\n".join(objectives),
            "expected_outputs": [sources, fields, schema, report], "max_steps": 24, "max_wall_time_seconds": 600,
            "acceptance_checks": checks,
        }))

    def task(role, index, outputs, criteria, tools, bindings=()):
        return {"task": {"run_id": run_id, "task_id": role, "agent": role, "objective": objectives[index],
                         "expected_outputs": outputs, "max_steps": 8, "max_wall_time_seconds": 120,
                         "acceptance_checks": criteria},
                "tool_allowlist": tools, "required_tools": tools,
                "bindings": [{"name": name, "producer_task_id": producer, "path": file}
                             for name, producer, file in bindings]}

    return WorkflowRequest.model_validate_json(json.dumps({
        "run_id": run_id, "max_steps": 32, "max_wall_time_seconds": 600,
        "tasks": [
            task("research", 0, [sources], checks[:1], ["research_export"]),
            task("browser", 1, [fields], checks[1:3], ["browser_open", "browser_export"]),
            task("coding", 2, [schema], checks[3:4], ["read_file", "write_file"], [("fields", "browser", fields)]),
            task("document", 3, [report], checks[4:], ["read_file", "write_file"],
                 [("sources", "research", sources), ("fields", "browser", fields), ("schema", "coding", schema)]),
        ],
        "final_checks": [checks[0], checks[2], checks[3], checks[4]],
    }))


async def single_run(config, model, client, runtime, task):
    runtime.artifacts.claim(task.run_id)
    files = TaskFiles(runtime.artifacts, task)
    registry = ToolRegistry(PermissionPolicy(auto_approve_up_to=PermissionTier.NETWORK))
    registry.register_many(
        BrowserOpenTool(client), BrowserExportTool(client, files), ReadFileTool(files), WriteFileTool(files),
        ResearchExportTool(WebSearchTool(config.searxng_base_url, str(config.trace_dir), task_id="baseline"), files),
    )

    class Checks:
        async def verify(self, _checks):
            results = []
            for check in task.acceptance_checks:
                if isinstance(check, PageCheck):
                    passed = (await client.browser_verify([check.model_dump(mode="json")])).success
                else:
                    passed = runtime.verifier.artifact_passes(check)
                results.append({"kind": check.kind, "passed": passed})
            return VerificationResult(VerificationStatus.PASSED if all(item["passed"] for item in results)
                                      else VerificationStatus.FAILED, "Fixture acceptance checks", {"checks": results})

    checker = Checks()
    loop = AgentLoop(config, model, registry, MemoryStore(config.db_path), task_verifier=checker,
                     specialist_prompt="Write a single literal SCHEMA dictionary. Read artifacts to obtain source URLs.")
    checks = [item.model_dump(mode="json") for item in task.acceptance_checks]
    record = await loop.run(task.objective, milestones=[{"objective": task.objective, "checks": checks}])
    accepted = await checker.verify(checks)
    return record.status.value == "completed" and accepted.status == VerificationStatus.PASSED, accepted.evidence


async def resource_samples(samples):
    gpu = shutil.which("nvidia-smi")
    try:
        import psutil
    except ImportError:
        psutil = None
    process = psutil.Process() if psutil else None
    while True:
        if process:
            # This host-side number excludes separately launched Ollama.
            samples["host_rss_bytes"] = max(samples.get("host_rss_bytes", 0), process.memory_info().rss)
        if gpu:
            command = await asyncio.create_subprocess_exec(
                gpu, "--query-gpu=memory.used", "--format=csv,noheader,nounits",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            data, _ = await command.communicate()
            if command.returncode == 0:
                try:
                    used = sum(int(line) for line in data.splitlines())
                    samples["board_vram_mib"] = max(samples.get("board_vram_mib", 0), used)
                except ValueError:
                    pass
        await asyncio.sleep(2)


async def trial(origin, root, tag, mode):
    run_id = f"{mode}_{uuid4().hex[:8]}"
    directory = root / run_id
    config = AgentConfig(ollama_base_url=AgentConfig.from_env().ollama_base_url,
                         searxng_base_url=origin, workspace_dir=directory / "workspace",
                         data_dir=root / "data", db_path=directory / "memory.db",
                         trace_dir=directory / "traces", max_steps=24, max_wall_time_seconds=600)
    for role in ("planner", "executor", "vision"):
        config.model_roles[role] = ModelRoleConfig(model=tag, context_length=8192, temperature=0)
    session = BrowserSession(config.workspace_dir, scope=run_id, headless=True)
    session.policy = BrowserPolicy(local_origins=(origin,))
    previous = browser_server.session
    browser_server.session = session
    client = SandboxClient("http://sandbox", admin_token="")
    await client.close()
    client._client = httpx.AsyncClient(transport=httpx.ASGITransport(app=tool_server.app), base_url="http://sandbox")
    model = ModelManager(config)
    runtime = SpecialistRuntime(config, model, client, default_registry())
    started = time.monotonic()
    samples = {}
    monitor = asyncio.create_task(resource_samples(samples))
    try:
        spec = contract(origin, run_id, single=mode == "single")
        if mode == "single":
            passed, evidence = await single_run(config, model, client, runtime, spec)
        else:
            result = await Orchestrator(model, runtime).run(GOAL, spec)
            passed, evidence = result.completed, result.model_dump(mode="json")
        elapsed = time.monotonic() - started
        metrics = model.inference.metrics
        return {"run_id": run_id, "mode": mode, "passed": passed, "seconds": elapsed,
                "model_calls": len(metrics), "tokens": sum(item["actual_tokens"] or 0 for item in metrics),
                "resource_samples": samples, "evidence": evidence}
    finally:
        monitor.cancel()
        await asyncio.gather(monitor, return_exceptions=True)
        await session.control("stop")
        browser_server.session = previous
        await client.close()
        await model.close()


async def evaluate(args):
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {"model": args.model, "results": [], "summary": {}, "completed": False}
    with fixture_site() as origin:
        for index in range(args.repeats):
            for mode in (("single", "multi") if index % 2 == 0 else ("multi", "single")):
                try:
                    result = await trial(origin, output.parent, args.model, mode)
                except (OSError, ValueError, httpx.HTTPError, TimeoutError) as exc:
                    result = {"mode": mode, "passed": False, "error": str(exc)}
                report["results"].append(result)
                print(f"{mode}: {'PASS' if result['passed'] else 'FAIL'}", flush=True)
                output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    summarize(report)
    report["completed"] = True
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def summarize(report):
    for mode in ("single", "multi"):
        items = [item for item in report["results"] if item["mode"] == mode]
        successes = [item for item in items if item["passed"]]
        summary = {"trials": len(items), "pass_rate": len(successes) / len(items) if items else 0}
        for key in ("seconds", "model_calls", "tokens"):
            values = [item[key] for item in successes]
            summary[f"median_success_{key}"] = statistics.median(values) if values else None
        report["summary"][mode] = summary
    single, multi = (report["summary"][mode] for mode in ("single", "multi"))
    base_time, multi_time = single["median_success_seconds"], multi["median_success_seconds"]
    ratio = multi_time / base_time if base_time and multi_time else None
    checks = {
        "sample_size": min(single["trials"], multi["trials"]) >= 3,
        "fixture_success": multi["pass_rate"] >= 0.9,
        "success_vs_baseline": multi["pass_rate"] >= single["pass_rate"],
        "latency_vs_baseline": ratio is not None and ratio <= 1.25,
    }
    report["release_gate"] = {
        "passed": all(checks.values()), "checks": checks, "latency_ratio": ratio,
        "minimum_trials_per_mode": 3, "minimum_pass_rate": 0.9, "maximum_latency_ratio": 1.25,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="qwen3.5:9b")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output", default=".cache/multi-agent-release/report.json")
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    report = asyncio.run(evaluate(args))
    print(json.dumps(report["summary"], indent=2))
    print(json.dumps(report["release_gate"], indent=2))
    return 0 if report["release_gate"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
