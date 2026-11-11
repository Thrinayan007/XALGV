"""Smoke checks against an already running Docker sandbox with local fixtures.

The validation deployment must explicitly allow http://127.0.0.1:8765 and serve
eval/fixtures/profile.html there. No domains/private networks are globally relaxed.
"""

import argparse
import asyncio
import hashlib
import json
import subprocess
import uuid
from pathlib import Path

import httpx
import websockets

from local_agent.config import AgentConfig, ModelRoleConfig
from local_agent.models import ModelManager
from local_agent.sandbox.client import SandboxClient
from local_agent.tools.browser import BrowserScreenshotTool


async def smoke(args):
    checks = {}
    client = SandboxClient(args.base_url)
    try:
        root = Path(__file__).resolve().parents[1]
        source_files = [root / "sandbox" / name for name in ("tool_server.py", "browser_server.py")]
        source_files += [path for path in (root / "sandbox" / "xbrowser").iterdir() if path.suffix in {".py", ".json"}]
        expected = {str(path.relative_to(root / "sandbox")).replace("\\", "/"): hashlib.sha256(path.read_bytes()).hexdigest() for path in source_files}
        code = "import pathlib,hashlib,json; root=pathlib.Path('/opt/tool_server'); files=[root/'tool_server.py',root/'browser_server.py']+[p for p in (root/'xbrowser').iterdir() if p.suffix in {'.py','.json'}]; print(json.dumps({str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}))"
        manifest = await asyncio.to_thread(subprocess.run, ["docker", "exec", "xalgv_sandbox", "python3", "-c", code],
                                           capture_output=True, text=True, timeout=10, check=True)
        checks["deployed_source_matches"] = json.loads(manifest.stdout) == expected
        for _ in range(60):
            if await client.health_check():
                break
            await asyncio.sleep(0.5)
        checks["api_health"] = await client.health_check()
        async with httpx.AsyncClient() as http:
            response = await http.get(args.novnc_url + "/vnc.html")
            checks["novnc_html"] = response.status_code == 200
        async with websockets.connect(args.novnc_url.replace("http://", "ws://") + "/websockify", subprotocols=["binary"]) as socket:
            greeting = await asyncio.wait_for(socket.recv(), 5)
            checks["novnc_rfb_bridge"] = greeting.startswith(b"RFB ") if isinstance(greeting, bytes) else greeting.startswith("RFB ")
        display = await asyncio.to_thread(subprocess.run, ["docker", "exec", "xalgv_sandbox", "xwininfo", "-root"],
                                          capture_output=True, text=True, timeout=10, check=False)
        checks["xvfb_display"] = display.returncode == 0 and "1280" in display.stdout and "720" in display.stdout
        first = await client.browser_open(args.fixture_url)
        checks["headful_navigation"] = first.success
        first_id = first.metadata.get("tab_id")
        second = await client.browser_tabs("new", url=args.fixture_url)
        checks["tabs"] = second.success and second.metadata.get("tab_id") != first_id
        switched = await client.browser_tabs("switch", tab=first_id)
        checks["switch"] = switched.success and switched.metadata.get("tab_id") == first_id
        observed = await client.browser_observe()
        checks["refs"] = observed.success and bool(observed.metadata["data"].get("elements"))
        checks["actual_token_budget"] = observed.metadata["data"].get("token_count") is not None and observed.metadata["data"]["token_count"] <= 700
        path = f"xbrowser-validation/{uuid.uuid4().hex}.png"
        screenshot = await client.browser_screenshot(path=path, marks=True)
        checks["marked_screenshot"] = screenshot.success
        verified = await client.browser_verify([{"kind": "artifact", "path": path, "png": True}, {"kind": "url", "expected": args.fixture_url}])
        checks["task_acceptance"] = verified.success
        denied = await client.browser_open("http://169.254.169.254/latest/meta-data/")
        checks["metadata_ip_blocked"] = not denied.success and denied.metadata.get("error_code") == "NAVIGATION_DENIED"
        vision_details = None
        if args.vision_model:
            config = AgentConfig()
            config.model_roles["vision"] = ModelRoleConfig(model=args.vision_model, context_length=8192, temperature=0)
            manager = ModelManager(config)
            try:
                visual = await asyncio.wait_for(BrowserScreenshotTool(client, vision_model=manager).execute({
                    "path": f"xbrowser-validation/vision-{uuid.uuid4().hex}.png", "describe": True, "marks": True,
                }), 240)
                description = visual.output.split("Visual observation (untrusted): ", 1)[-1].lower()
                checks["real_model_vision"] = visual.success and "visual observation" in visual.output.lower() and (
                    "display name" in description or "save profile" in description
                )
                vision_details = {"output": visual.output, "error": visual.error, "metrics": visual.metadata.get("vision")}
            finally:
                await manager.close()
        restart = await client.browser_session("restart")
        checks["restart"] = restart.success and restart.metadata["generation"] > first.metadata["generation"]
        stale = await client.browser_tabs("switch", tab=first_id)
        checks["stale_tab_rejected"] = not stale.success and stale.metadata.get("error_code") == "TAB_NOT_FOUND"
        report = {"passed": all(checks.values()), "checks": checks, "screenshot": screenshot.artifacts,
                  "observation_tokens": observed.metadata["data"].get("token_count"),
                  "deployment": "Docker Chromium headful Xvfb noVNC"}
        if vision_details:
            report["vision"] = vision_details
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2))
        return report["passed"]
    finally:
        await client.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:9000")
    parser.add_argument("--novnc-url", default="http://localhost:6080")
    parser.add_argument("--fixture-url", default="http://127.0.0.1:8765/profile.html")
    parser.add_argument("--output", default="traces/xbrowser-validation/deployment-report.json")
    parser.add_argument("--vision-model", help="Also validate optional screenshot interpretation with a real local model.")
    return 0 if asyncio.run(smoke(parser.parse_args())) else 1


if __name__ == "__main__":
    raise SystemExit(main())
