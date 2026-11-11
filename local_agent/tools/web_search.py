"""Web search tool — wraps the SearXNG search service as a proper agent tool."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import Any

from local_agent.security.permissions import PermissionTier
from local_agent.tools.base import Tool, ToolResult, VerificationResult, VerificationStatus
from local_agent.web_search import (
    SearchMode,
    SearchPolicyError,
    SearchRequest,
    SearchUnavailableError,
    SearxNGClient,
    TraceStore,
    WebSearchService,
)


class WebSearchTool(Tool):
    """Search the public web via SearXNG and return cited result metadata.

    This tool wraps the existing :class:`WebSearchService` into the standard
    tool interface so the agent loop can call it like any other tool, with
    permission gating and verification.
    """

    def __init__(
        self,
        base_url: str = "http://localhost:8080",
        trace_dir: str = "traces",
        *,
        search_mode: SearchMode = SearchMode.ON,
        task_id: str = "agent",
    ) -> None:
        try:
            client = SearxNGClient(base_url)
        except SearchPolicyError:
            # If we can't validate the endpoint at init time (e.g. host not
            # running yet), create with defaults and let execute-time fail.
            client = None  # type: ignore[assignment]
        self._client = client
        self._trace = TraceStore(trace_dir)
        self._service = WebSearchService(client, self._trace) if client else None
        self._mode = search_mode
        self._task_id = task_id

    @property
    def name(self) -> str:
        return "web_search"

    @property
    def description(self) -> str:
        return (
            "Search the public web using a local SearXNG instance. "
            "Returns titles, URLs, and snippets for the top results. "
            "Cannot log in, download files, or submit forms."
        )

    @property
    def permission_tier(self) -> PermissionTier:
        return PermissionTier.NETWORK

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query string.",
                    "maxLength": 400,
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of results to return (default: 5).",
                    "default": 5,
                    "minimum": 1,
                    "maximum": 10,
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        }

    async def execute(self, params: dict[str, Any]) -> ToolResult:
        query = params.get("query", "")
        max_results = params.get("max_results", 5)

        if self._service is None:
            return ToolResult(
                success=False,
                output="",
                error="SearXNG search service is not available.  "
                      "Ensure SearXNG is running at the configured URL.",
            )

        try:
            request = SearchRequest(
                query=query,
                max_results=max_results,
                task_id=self._task_id,
            )
            response = await asyncio.to_thread(self._service.execute, request, self._mode, True)

            # Format results for the agent.
            lines: list[str] = []
            for r in response.results:
                lines.append(
                    f"[{r.source_id}] {r.title}\n"
                    f"  URL: {r.url}\n"
                    f"  Snippet: {r.snippet}"
                )

            output = "\n\n".join(lines) if lines else "No results found."
            return ToolResult(
                success=True,
                output=output,
                metadata={
                    "request_id": response.request_id,
                    "result_count": len(response.results),
                    "retrieved_at": response.retrieved_at,
                    "sources": [asdict(item) for item in response.results],
                },
            )

        except SearchUnavailableError as exc:
            return ToolResult(success=False, output="", error=f"Search disabled: {exc}")
        except SearchPolicyError as exc:
            return ToolResult(success=False, output="", error=f"Search policy error: {exc}")
        except (OSError, TypeError, ValueError) as exc:
            return ToolResult(success=False, output="", error=f"Search error: {exc}")

    async def verify(self, params: dict[str, Any], result: ToolResult) -> VerificationResult:
        if result.success and result.metadata.get("result_count", 0) > 0:
            return VerificationResult(
                status=VerificationStatus.PASSED,
                message=f"Search returned {result.metadata['result_count']} results.",
                evidence={"result_count": result.metadata["result_count"]},
            )
        if result.success:
            return VerificationResult(
                status=VerificationStatus.PASSED,
                message="Search completed but returned no results.",
            )
        return VerificationResult(
            status=VerificationStatus.FAILED,
            message=f"Search failed: {result.error}",
        )

