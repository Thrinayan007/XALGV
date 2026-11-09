"""Four roles with real browser and search APIs, deterministic model replies."""

import json

import httpx

from eval.multi_agent import SOURCE, contract, fixture_site
from local_agent.agents.definitions import default_registry
from local_agent.agents.orchestrator import Orchestrator
from local_agent.agents.runtime import SpecialistRuntime
from local_agent.agents.workflow_store import WorkflowStore
from local_agent.config import AgentConfig
from local_agent.models import ChatResponse, ToolCall
from local_agent.sandbox.client import SandboxClient
from sandbox import browser_server, tool_server
from sandbox.xbrowser.safety import BrowserPolicy


async def test_four_specialists_share_verified_evidence(browser, tmp_path, monkeypatch):
    monkeypatch.setattr(browser_server, "session", browser)
    with fixture_site() as origin:
        browser.policy = BrowserPolicy(local_origins=(origin,))
        spec = contract(origin, "four_roles")
        source, fields, schema, report = [item.task.expected_outputs[0] for item in spec.tasks]
        roles = ["research", "browser", "coding", "document"]
        plan = json.dumps({"tasks": [{"task_id": name, "approach": name, "dependencies": []} for name in roles]})
        script = iter([
            (None, plan, None),
            ("research_export", {"query": "Python Tutorial", "path": source}, {"research_export"}),
            (None, "DONE: source manifest captured", {"research_export"}),
            ("browser_open", {"url": f"{origin}/profile.html"}, {"browser_open", "browser_export"}),
            ("browser_export", {"path": fields}, {"browser_open", "browser_export"}),
            (None, "DONE: form fields captured", {"browser_open", "browser_export"}),
            ("read_file", {"path": fields}, {"read_file", "write_file"}),
            ("write_file", {"path": schema, "content": 'SCHEMA = {"Display name": "str"}'}, {"read_file", "write_file"}),
            (None, "DONE: schema written", {"read_file", "write_file"}),
            ("read_file", {"path": source}, {"read_file", "write_file"}),
            ("write_file", {"path": report, "content": f"# Summary\nA schema for the form.\n\n## Sources\n[Python Tutorial]({SOURCE})"}, {"read_file", "write_file"}),
            (None, "DONE: cited report written", {"read_file", "write_file"}),
        ])

        class Model:
            async def chat(self, role, messages, *, tools=None, **kwargs):
                name, value, allowed = next(script)
                if allowed is not None:
                    assert {item["function"]["name"] for item in tools} == allowed
                return ChatResponse(value if name is None else "",
                                    [ToolCall("tool", name, value)] if name else [], "fixture", role, True)

        client = SandboxClient("http://sandbox", admin_token="")
        await client.close()
        client._client = httpx.AsyncClient(transport=httpx.ASGITransport(app=tool_server.app), base_url="http://sandbox")
        config = AgentConfig(workspace_dir=browser.workspace, data_dir=tmp_path / "data", searxng_base_url=origin)
        model = Model()
        try:
            result = await Orchestrator(model, SpecialistRuntime(config, model, client, default_registry())).run(
                "Produce a cited form schema report", spec,
            )
        finally:
            await client.close()
    assert result.completed, result.model_dump_json(indent=2)
    assert [item.task_id for item in result.tasks] == roles
    assert result.final_checks == (True, True, True, True)
    stored = WorkflowStore(config.data_dir / "agent-runs/workflows.sqlite3", read_only=True).inspect("four_roles")
    assert len([event for event in stored["agent_events"] if event["kind"] == "artifact_handoff"]) == 4
    assert (browser.workspace / report).read_text(encoding="utf-8").endswith(f"({SOURCE})")
