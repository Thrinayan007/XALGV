"""Repeated local-model evaluation against real Chromium and objective criteria.

Run: python -m eval.xbrowser --model qwen3.5:9b --repeats 3
No external websites, fabricated model responses, or summary-only success checks.
"""

import argparse
import asyncio
import json
import math
import statistics
import threading
import time
import uuid
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

from local_agent.config import AgentConfig, ModelRoleConfig
from local_agent.core.loop import AgentLoop
from local_agent.core.task_verification import TaskVerifier
from local_agent.memory.store import MemoryStore
from local_agent.models import ModelManager
from local_agent.sandbox.client import SandboxClient
from local_agent.security.permissions import PermissionPolicy, PermissionTier
from local_agent.tools.browser import browser_tools
from local_agent.tools.registry import ToolRegistry
from sandbox import browser_server, tool_server
from sandbox.xbrowser import BrowserSession
from sandbox.xbrowser.safety import BrowserPolicy


class Meter:
    def __init__(self, manager):
        self.manager = manager
        self.calls = []

    async def chat(self, *args, **kwargs):
        started = time.monotonic()
        try:
            response = await self.manager.chat(*args, **kwargs)
        except Exception as exc:
            self.calls.append({"role": args[0], "seconds": time.monotonic() - started,
                               "prompt_tokens": 0, "generated_tokens": 0, "tools": [], "failed": True,
                               "error": str(exc)})
            raise
        self.calls.append({"role": args[0], "seconds": time.monotonic() - started,
                           "prompt_tokens": response.prompt_eval_count, "generated_tokens": response.eval_count,
                           "tools": [call.name for call in response.tool_calls]})
        return response


def percentile(values, fraction):
    return sorted(values)[min(len(values)-1, max(0, math.ceil(len(values)*fraction)-1))] if values else None


async def evaluate(args):
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    workspace = output.parent / "workspace"
    workspace.mkdir(exist_ok=True)

    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(Path(__file__).parent / "fixtures")))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/profile.html"
    config = AgentConfig(db_path=output.parent / "evaluation.db", trace_dir=output.parent / "checkpoints",
                         max_steps=20, max_wall_time_seconds=args.timeout, vram_budget_mb=6144)
    for role in ("planner", "executor", "vision"):
        config.model_roles[role] = ModelRoleConfig(model=args.model, context_length=8192, temperature=0)
    manager = ModelManager(config)
    report = {"model": args.model, "repeats": args.repeats, "mode": "real-model-real-chromium-local-fixtures",
              "context_length": 8192, "results": [], "status": "running"}
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    try:
        for repeat in range(args.repeats):
            for scenario in args.scenarios:
                trial = f"{scenario}-{repeat}-{uuid.uuid4().hex[:8]}"
                session = BrowserSession(workspace, scope=trial, headless=True)
                session.policy = BrowserPolicy(local_origins=(f"http://127.0.0.1:{server.server_port}",))
                browser_server.session = session
                client = SandboxClient("http://sandbox")
                await client.close()
                client._client = httpx.AsyncClient(transport=httpx.ASGITransport(app=tool_server.app), base_url="http://sandbox")
                registry = ToolRegistry(PermissionPolicy(auto_approve_up_to=PermissionTier.NETWORK))
                registry.register_many(*browser_tools(client))
                meter = Meter(manager)
                loop = AgentLoop(config, meter, registry, MemoryStore(config.db_path), task_verifier=TaskVerifier(client))
                checks = [{"kind": "url", "expected": url}]
                if scenario == "navigation":
                    goal = f"Open {url}, observe the page, and report its title. Then finish."
                    checks.append({"kind": "text", "expected": "Evaluation profile"})
                elif scenario == "reference_form":
                    goal = f"Open {url}, use browser_observe references to type Ada Lovelace into Display name, click Save profile, and verify Saved Ada Lovelace appears."
                    checks.append({"kind": "text", "expected": "Saved Ada Lovelace"})
                else:
                    paths = [f"{trial}/page-{index}.png" for index in range(5)]
                    goal = f"Use exactly ONE browser_run to open {url}, immediately take the first viewport PNG screenshot, then alternate scroll down 500 pixels and viewport screenshot four times. Paths in order: {json.dumps(paths)}. Use full_page=false. The macro contains exactly 10 steps: open, screenshot, scroll, screenshot, scroll, screenshot, scroll, screenshot, scroll, screenshot. Do not navigate or observe separately. When all five files exist, respond DONE."
                    checks.extend({"kind": "artifact", "path": path, "png": True, "width": 1280, "height": 720} for path in paths)
                started = time.monotonic()
                try:
                    record = await loop.run(goal, acceptance_checks=checks)
                    # Independently recheck after the loop, including when it stopped early.
                    verified = await client.browser_verify(checks)
                    passed = record.status.value == "completed" and verified.success
                    tools = [step.tool_name for step in record.steps if step.action_type == "tool_call"]
                    if scenario == "reference_form":
                        passed = passed and all(any(step.tool_name == tool and "ref" in (step.tool_params or {}) for step in record.steps)
                                                for tool in ("browser_click", "browser_type"))
                    workflow_correct = True
                    efficient = True
                    if scenario == "five_captures":
                        macros = [step for step in record.steps if step.tool_name == "browser_run"]
                        sequence = macros[0].tool_params["steps"] if len(macros) == 1 else []
                        workflow_correct = [step["tool"] for step in sequence] == ["open", "screenshot", "scroll", "screenshot", "scroll", "screenshot", "scroll", "screenshot", "scroll", "screenshot"]
                        efficient = len(meter.calls) <= 3
                        passed = passed and workflow_correct
                    report["results"].append({"trial": trial, "scenario": scenario, "passed": passed,
                        "run_status": record.status.value, "summary": record.summary, "seconds": time.monotonic()-started,
                        "model_calls": len(meter.calls), "prompt_tokens": sum(call["prompt_tokens"] for call in meter.calls),
                        "generated_tokens": sum(call["generated_tokens"] for call in meter.calls), "tools": tools,
                        "calls": meter.calls, "acceptance": verified.metadata,
                        "workflow_correct": workflow_correct, "three_call_target_met": efficient})
                    print(f"{trial}: {'PASS' if passed else 'FAIL'} ({len(meter.calls)} model calls)", flush=True)
                    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
                finally:
                    await session.control("stop")
                    await client.close()
        report["status"] = "complete"
        results = report["results"]
        report["success_rate"] = sum(item["passed"] for item in results) / len(results)
        report["target_success_rate"] = 0.9
        report["target_met"] = report["success_rate"] >= 0.9
        captures = [item for item in results if item["scenario"] == "five_captures"]
        report["capture_three_call_target_met"] = all(item["three_call_target_met"] for item in captures)
        report["median_seconds"] = statistics.median(item["seconds"] for item in results)
        report["p95_seconds"] = percentile([item["seconds"] for item in results], 0.95)
        report["median_model_calls"] = statistics.median(item["model_calls"] for item in results)
        report["running_models"] = await manager.list_running()
        output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    finally:
        await manager.close()
        server.shutdown()
        server.server_close()
        thread.join()
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="qwen3.5:9b")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument("--scenarios", nargs="+", choices=["navigation", "reference_form", "five_captures"], default=["navigation", "reference_form", "five_captures"])
    parser.add_argument("--output", default="traces/xbrowser-validation/model-report.json")
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("repeats must be positive")
    report = asyncio.run(evaluate(args))
    print(json.dumps({key: report[key] for key in ("success_rate", "target_met", "median_seconds", "median_model_calls")}))
    return 0 if report["target_met"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
