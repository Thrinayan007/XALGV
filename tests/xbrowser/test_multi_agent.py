"""Browser → Coding vertical slice over the actual XBrowser API and Chromium."""

import json

import httpx

from local_agent.agents.definitions import default_registry
from local_agent.agents.orchestrator import Orchestrator
from local_agent.agents.runtime import SpecialistRuntime
from local_agent.agents.task_graph import WorkflowRequest
from local_agent.agents.workflow_store import WorkflowStore
from local_agent.config import AgentConfig
from local_agent.models import ChatResponse, ToolCall
from local_agent.sandbox.client import SandboxClient
from sandbox import browser_server, tool_server


async def test_browser_to_coding_workflow(browser, site, tmp_path, monkeypatch):
    monkeypatch.setattr(browser_server, "session", browser)
    fields = "artifacts/form_run/inspect/fields.json"
    schema = "artifacts/form_run/code/schema.py"
    url = f"{site}/first.html"
    plan = {"tasks": [{"task_id": "inspect", "approach": "Export the form"},
                      {"task_id": "code", "approach": "Use the verified export"}]}
    calls = iter([
        ("planner", None, json.dumps(plan), {}),
        ("executor", "write_file", "", {"path": fields, "content": "forged evidence"}),
        ("executor", "browser_open", "", {"url": url}),
        ("executor", "browser_export", "", {"path": fields}),
        ("executor", None, "DONE: Captured form fields", {}),
        ("executor", "read_file", "", {"path": fields}),
        ("executor", "write_file", "", {"path": schema, "content": 'SCHEMA = {"Name": "str"}\n'}),
        ("executor", "validate_python_schema", "", {"path": schema, "evidence_path": fields}),
        ("executor", None, "DONE: Generated and tested schema", {}),
    ])

    class Model:
        async def chat(self, role, messages, *, tools=None, **kwargs):
            expected_role, name, content, args = next(calls)
            assert role == expected_role
            if role == "executor":
                advertised = {tool["function"]["name"] for tool in tools}
                assert "shell_exec" not in advertised
                if "browser_open" in advertised:
                    assert "write_file" not in advertised
                else:
                    assert "browser_export" not in advertised
                    assert "Captured form fields" not in str(messages)
            return ChatResponse(content, [ToolCall("call", name, args)] if name else [], "fixture", role, True)

    check = {"kind": "python_schema", "path": schema, "fields": [{"name": "Name", "type": "str"}]}
    request = WorkflowRequest.model_validate_json(json.dumps({
        "run_id": "form_run", "max_steps": 20, "max_wall_time_seconds": 70,
        "tasks": [
            {"task": {"run_id": "form_run", "task_id": "inspect", "agent": "browser", "objective": "Inspect the form",
                      "max_steps": 8, "max_wall_time_seconds": 30, "expected_outputs": [fields],
                      "acceptance_checks": [{"kind": "url", "expected": url},
                                            {"kind": "artifact", "path": fields, "contains": '"Name"'}]},
             "required_tools": ["browser_open", "browser_export"]},
            {"task": {"run_id": "form_run", "task_id": "code", "agent": "coding", "objective": "Write and test the schema",
                      "max_steps": 8, "max_wall_time_seconds": 30, "expected_outputs": [schema],
                      "acceptance_checks": [check]},
             "bindings": [{"name": "form_fields", "producer_task_id": "inspect", "path": fields}],
             "required_tools": ["read_file", "write_file", "validate_python_schema"]},
        ],
        "final_checks": [check],
    }))
    config = AgentConfig(workspace_dir=browser.workspace, data_dir=tmp_path / "data")
    client = SandboxClient("http://sandbox", admin_token="")
    await client.close()
    client._client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=tool_server.app), base_url="http://sandbox",
    )
    model = Model()
    adapter = SpecialistRuntime(config, model, client, default_registry())
    try:
        result = await Orchestrator(model, adapter).run("Generate and test a Python schema for the fixture", request)
    finally:
        await client.close()
    assert result.completed, result.model_dump_json(indent=2)
    assert result.final_checks == (True,)
    assert [entry.task_id for entry in result.tasks] == ["inspect", "code"]
    assert all(entry.verification.passed for entry in result.tasks)
    assert (browser.workspace / schema).read_text() == 'SCHEMA = {"Name": "str"}\n'
    assert (config.data_dir / "agent-runs/form_run/inspect/1/memory.db").is_file()
    assert (config.data_dir / "agent-runs/form_run/code/1/memory.db").is_file()
    store = WorkflowStore(config.data_dir / "agent-runs/workflows.sqlite3")
    journal = store.inspect("form_run")
    assert journal["run"]["status"] == "completed"
    assert len(journal["task_attempts"]) == len(journal["task_verifications"]) == 2
    assert len(journal["task_artifacts"]) == 2
    assert all(row["closed_at"] for row in journal["resource_leases"])
    handoffs = [json.loads(row["payload"]) for row in journal["agent_events"] if row["kind"] == "artifact_handoff"]
    assert len(handoffs) == 1 and handoffs[0]["producer_task_id"] == "inspect"
    assert handoffs[0]["artifact"]["sha256"] == result.tasks[0].attempts[0].artifacts[0].sha256
    assert store.manifests("unrelated_run", ["inspect"]) == {"inspect": {}}
