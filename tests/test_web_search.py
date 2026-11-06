"""Tests for web search — both the low-level service and the Tool wrapper."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest

from local_agent.web_search import (
    SearchMode,
    SearchPolicy,
    SearchPolicyError,
    SearchRequest,
    SearchUnavailableError,
    SearchResponse,
    SearchResult,
    SearxNGClient,
    TraceStore,
    WebSearchService,
    web_search_tool_definition,
)


class StubClient:
    def search(self, request):
        return SearchResponse(
            request_id="req_001",
            query=request.query,
            retrieved_at="2026-11-05T00:00:00Z",
            results=(SearchResult("src_001", "Example", "https://example.com", "Example result", 1, "2026-11-05T00:00:00Z"),),
        )


class WebSearchToolTests:
    """Tests for the WebSearchTool wrapper."""

    def test_tool_schema(self):
        from local_agent.tools.web_search import WebSearchTool
        # SearXNG won't be running, so construct manually.
        tool = WebSearchTool.__new__(WebSearchTool)
        tool._client = None
        tool._trace = None
        tool._service = None
        tool._mode = SearchMode.ON
        assert tool.name == "web_search"
        schema = tool.parameters_schema
        assert "query" in schema["properties"]
        defn = tool.to_openai_tool()
        assert defn["function"]["name"] == "web_search"

    @pytest.mark.asyncio
    async def test_tool_execute_without_service(self):
        from local_agent.tools.web_search import WebSearchTool
        tool = WebSearchTool.__new__(WebSearchTool)
        tool._client = None
        tool._trace = None
        tool._service = None
        tool._mode = SearchMode.ON
        result = await tool.execute({"query": "test"})
        assert not result.success
        assert "not available" in result.error


class WebSearchTests(unittest.TestCase):
    def test_tool_is_hidden_when_search_is_off(self):
        self.assertEqual(web_search_tool_definition(SearchMode.OFF, False), [])
        self.assertEqual(web_search_tool_definition(SearchMode.ASK, False), [])
        self.assertEqual(web_search_tool_definition(SearchMode.ON, False)[0]["function"]["name"], "web_search")

    def test_ask_mode_requires_task_approval(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = WebSearchService(StubClient(), TraceStore(temp_dir))
            with self.assertRaises(SearchUnavailableError):
                service.execute(SearchRequest("test"), SearchMode.ASK, False)
            response = service.execute(SearchRequest("test"), SearchMode.ASK, True)
            self.assertEqual(response.results[0].source_id, "src_001")

    def test_enabled_search_writes_a_trace(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = WebSearchService(StubClient(), TraceStore(temp_dir))
            service.execute(SearchRequest("test", task_id="task/one"), SearchMode.ON, False)
            trace = Path(temp_dir) / "task_one.jsonl"
            event = json.loads(trace.read_text(encoding="utf-8"))
            self.assertEqual(event["event_type"], "web_search")
            self.assertEqual(event["response"]["query"], "test")

    def test_request_limits_are_enforced(self):
        policy = SearchPolicy(max_query_length=5, max_results=3)
        with self.assertRaises(SearchPolicyError):
            SearchRequest("longer than five").validate(policy)
        with self.assertRaises(SearchPolicyError):
            SearchRequest("short", max_results=4).validate(policy)

    def test_endpoint_must_be_loopback(self):
        with self.assertRaises(SearchPolicyError):
            SearxNGClient("https://example.com")


if __name__ == "__main__":
    unittest.main()
