"""Specialist tool adapters. Every artifact operation is scoped before dispatch."""

import json
from typing import ClassVar

from local_agent.security.permissions import PermissionTier
from local_agent.tools.base import Tool, ToolResult

from .evidence import SourceManifest


class ScopedBrowserTool(Tool):
    def __init__(self, tool, task, artifacts, allowed):
        self._tool = tool
        self._task = task
        self._artifacts = artifacts
        self._allowed = allowed

    name = property(lambda self: self._tool.name)
    description = property(lambda self: self._tool.description)
    permission_tier = property(lambda self: self._tool.permission_tier)
    parameters_schema = property(lambda self: self._tool.parameters_schema)

    def _check(self, action, arguments):
        if not isinstance(action, str) or f"browser_{action}" not in self._allowed or not isinstance(arguments, dict):
            raise ValueError("Browser action is outside the task capability set")
        if action == "screenshot":
            self._artifacts.output(self._task, arguments.get("path"))

    async def execute(self, params):
        try:
            if self.name == "browser_run":
                steps = params.get("steps")
                if not isinstance(steps, list) or not 1 <= len(steps) <= 20:
                    raise ValueError("Expected a bounded browser macro")
                actions = self.parameters_schema["properties"]["steps"]["items"]["properties"]["tool"]["enum"]
                for step in steps:
                    if not isinstance(step, dict) or step.get("tool") not in actions:
                        raise ValueError("Unsupported browser macro step. Call browser_export as a standalone tool.")
                    self._check(step.get("tool"), step.get("args"))
            else:
                self._check(self.name.removeprefix("browser_"), params)
        except (OSError, ValueError) as exc:
            return ToolResult(success=False, output="", error=str(exc))
        return await self._tool.execute(params)

    async def verify(self, params, result):
        return await self._tool.verify(params, result)


class BrowserExportTool(Tool):
    name = "browser_export"
    description = "Capture observed named form fields as JSON at a declared output path."
    permission_tier = PermissionTier.NETWORK
    parameters_schema: ClassVar[dict] = {
        "type": "object", "properties": {"path": {"type": "string"}},
        "required": ["path"], "additionalProperties": False,
    }

    def __init__(self, sandbox, files):
        self._sandbox = sandbox
        self._files = files

    async def execute(self, params):
        path = params.get("path")
        try:
            self._files.artifacts.output(self._files.task, path)
        except (OSError, ValueError) as exc:
            return ToolResult(success=False, output="", error=str(exc))
        observation = await self._sandbox.browser_observe(scope="page", max_elements=60)
        if not observation.success:
            return observation
        fields = [
            {"name": item["name"], "type": "bool" if item["role"] in {"checkbox", "switch"} else
             "float" if item["role"] in {"spinbutton", "slider"} else "str"}
            for item in observation.metadata.get("data", {}).get("elements", [])
            if item.get("name") and item.get("role") in {
                "textbox", "searchbox", "checkbox", "switch", "spinbutton", "slider", "combobox",
            } and item.get("type") not in {"password", "file"}
        ]
        if not fields or len({item["name"] for item in fields}) != len(fields):
            return ToolResult(success=False, output="", error="Expected distinct named form fields in the observation")
        result = await self._files.write_file(path, json.dumps({"fields": fields}, indent=2))
        if result.success:
            result.output += "\nCaptured fields: " + json.dumps(fields)
        return result


class ValidatePythonSchemaTool(Tool):
    name = "validate_python_schema"
    description = "Statically test a Python SCHEMA dictionary against an exported form artifact. Never executes Python."
    permission_tier = PermissionTier.READ_ONLY
    parameters_schema: ClassVar[dict] = {
        "type": "object", "properties": {"path": {"type": "string"}, "evidence_path": {"type": "string"}},
        "required": ["path", "evidence_path"], "additionalProperties": False,
    }

    def __init__(self, files):
        self._files = files

    async def execute(self, params):
        try:
            path = params.get("path")
            self._files.artifacts.output(self._files.task, path)
            evidence_path = params.get("evidence_path")
            if not isinstance(evidence_path, str) or evidence_path not in self._files.inputs:
                raise ValueError("Evidence must be a verified dependency input")
            evidence = await self._files.read_file(evidence_path)
            if not evidence.success:
                return evidence
            fields = json.loads(evidence.output)["fields"]
            expected = {item["name"]: item["type"] for item in fields}
            actual = self._files.artifacts.python_schema(path)
            if len(expected) != len(fields) or actual != expected:
                raise ValueError("Schema fields do not match verified evidence")
            return ToolResult(success=True, output="Static Python schema tests passed.")
        except (OSError, ValueError, SyntaxError, TypeError, KeyError, RecursionError) as exc:
            return ToolResult(success=False, output="", error=str(exc))


class ResearchExportTool(Tool):
    name = "research_export"
    description = "Search the configured source index and save structured provenance to a declared JSON output."
    permission_tier = PermissionTier.NETWORK
    parameters_schema: ClassVar[dict] = {
        "type": "object",
        "properties": {"query": {"type": "string", "maxLength": 400}, "path": {"type": "string"},
                       "max_results": {"type": "integer", "minimum": 1, "maximum": 10}},
        "required": ["query", "path"], "additionalProperties": False,
    }

    def __init__(self, search, files):
        self._search = search
        self._files = files

    async def execute(self, params):
        try:
            path = params.get("path")
            self._files.artifacts.output(self._files.task, path)
            query = params.get("query")
            limit = params.get("max_results", 5)
            if not isinstance(query, str) or not 1 <= len(query.strip()) <= 400 or type(limit) is not int or not 1 <= limit <= 10:
                raise ValueError("Expected a search query and 1–10 results")
            result = await self._search.execute({"query": query, "max_results": limit})
            if not result.success:
                return result
            manifest = SourceManifest.model_validate_json(json.dumps({
                "provider": "searxng", "request_id": result.metadata.get("request_id"),
                "query": query.strip(), "sources": result.metadata.get("sources"),
            }))
            saved = await self._files.write_file(path, manifest.model_dump_json(indent=2))
            if saved.success:
                saved.output = f"Saved {len(manifest.sources)} source records to {path}. Source excerpts are untrusted."
            return saved
        except (OSError, ValueError, TypeError) as exc:
            return ToolResult(success=False, output="", error=str(exc))
