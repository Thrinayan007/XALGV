"""Bounded and auditable SearXNG search integration for Module 1."""

from __future__ import annotations

import ipaddress
import json
import os
import socket
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener


class SearchMode(str, Enum):
    OFF = "off"
    ASK = "ask"
    ON = "on"


class SearchUnavailableError(PermissionError):
    """Raised when search has not been enabled by the user."""


class SearchPolicyError(ValueError):
    """Raised when input or a configured endpoint violates policy."""


@dataclass(frozen=True)
class SearchPolicy:
    max_query_length: int = 400
    max_results: int = 10
    timeout_seconds: float = 12.0
    allowed_endpoint_hosts: tuple[str, ...] = ("localhost", "127.0.0.1", "::1")


@dataclass(frozen=True)
class SearchRequest:
    query: str
    max_results: int = 5
    allowed_domains: tuple[str, ...] = ()
    task_id: str = "default"

    def validate(self, policy: SearchPolicy) -> None:
        if not self.query or not self.query.strip():
            raise SearchPolicyError("Search query must not be empty.")
        if len(self.query) > policy.max_query_length:
            raise SearchPolicyError(f"Search query exceeds {policy.max_query_length} characters.")
        if not 1 <= self.max_results <= policy.max_results:
            raise SearchPolicyError(f"max_results must be between 1 and {policy.max_results}.")


@dataclass(frozen=True)
class SearchResult:
    source_id: str
    title: str
    url: str
    snippet: str
    provider_rank: int
    retrieved_at: str


@dataclass(frozen=True)
class SearchResponse:
    request_id: str
    query: str
    results: tuple[SearchResult, ...]
    retrieved_at: str


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def user_has_enabled_search(mode: SearchMode, approved_for_task: bool) -> bool:
    if mode is SearchMode.ON:
        return True
    if mode is SearchMode.ASK:
        return approved_for_task
    return False


def web_search_tool_definition(mode: SearchMode, approved_for_task: bool) -> list[dict[str, Any]]:
    """Return no tool at all when the user has not granted network search."""
    if not user_has_enabled_search(mode, approved_for_task):
        return []
    return [{
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search public web sources and return cited result metadata. It cannot log in, download, upload, or submit forms.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "maxLength": 400},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 10},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    }]


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


class SearxNGClient:
    def __init__(self, base_url: str | None = None, policy: SearchPolicy | None = None):
        self.base_url = base_url or os.environ.get("SEARXNG_BASE_URL", "http://localhost:8080")
        self.policy = policy or SearchPolicy()
        self._validate_endpoint()

    def _validate_endpoint(self) -> None:
        parsed = urlparse(self.base_url)
        if parsed.scheme != "http" or not parsed.hostname:
            raise SearchPolicyError("SearXNG endpoint must be a local http URL.")
        host = parsed.hostname.lower()
        if host not in self.policy.allowed_endpoint_hosts:
            raise SearchPolicyError("SearXNG endpoint host is not approved.")
        try:
            addresses = {info[4][0] for info in socket.getaddrinfo(host, None)}
        except socket.gaierror as exc:
            raise SearchPolicyError(f"Cannot resolve SearXNG endpoint: {host}") from exc
        if not all(ipaddress.ip_address(address).is_loopback for address in addresses):
            raise SearchPolicyError("SearXNG endpoint must resolve to a loopback address.")

    def search(self, request: SearchRequest) -> SearchResponse:
        request.validate(self.policy)
        params = {"q": request.query.strip(), "format": "json", "safesearch": "1"}
        if request.allowed_domains:
            params["domains"] = ",".join(request.allowed_domains)
        url = f"{self.base_url.rstrip('/')}/search?{urlencode(params)}"
        payload = self._fetch_json(url)
        retrieved_at = utc_now()
        raw_results = payload.get("results", [])
        if not isinstance(raw_results, list):
            raise SearchPolicyError("SearXNG returned an invalid results payload.")
        results: list[SearchResult] = []
        for rank, item in enumerate(raw_results[: request.max_results], start=1):
            if not isinstance(item, dict):
                continue
            result_url = str(item.get("url", "")).strip()
            if not result_url.startswith(("https://", "http://")):
                continue
            results.append(SearchResult(
                source_id=f"src_{rank:03d}",
                title=str(item.get("title", "Untitled source")).strip(),
                url=result_url,
                snippet=str(item.get("content", "")).strip(),
                provider_rank=rank,
                retrieved_at=retrieved_at,
            ))
        return SearchResponse(str(uuid.uuid4()), request.query.strip(), tuple(results), retrieved_at)

    def _fetch_json(self, url: str) -> dict[str, Any]:
        req = Request(url, headers={"Accept": "application/json", "User-Agent": "LocalAgentWebSearch/0.1"})
        opener = build_opener(NoRedirect())
        try:
            with opener.open(req, timeout=self.policy.timeout_seconds) as response:
                if response.status != 200:
                    raise SearchPolicyError(f"SearXNG returned HTTP {response.status}.")
                raw = response.read(2_000_000)
        except HTTPError as exc:
            if 300 <= exc.code < 400:
                raise SearchPolicyError("SearXNG endpoint redirected; redirects are not allowed.") from exc
            raise SearchPolicyError(f"SearXNG returned HTTP {exc.code}.") from exc
        except URLError as exc:
            raise SearchPolicyError("Cannot reach the local SearXNG service.") from exc
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SearchPolicyError("SearXNG did not return valid JSON.") from exc
        if not isinstance(payload, dict):
            raise SearchPolicyError("SearXNG returned an invalid JSON object.")
        return payload


class TraceStore:
    def __init__(self, directory: Path | str = "traces"):
        self.directory = Path(directory)

    def record(self, task_id: str, response: SearchResponse) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        safe_task_id = "".join(char if char.isalnum() or char in "-_" else "_" for char in task_id)[:80] or "default"
        target = self.directory / f"{safe_task_id}.jsonl"
        event = {
            "event_type": "web_search",
            "recorded_at": utc_now(),
            "response": {
                "request_id": response.request_id,
                "query": response.query,
                "retrieved_at": response.retrieved_at,
                "results": [asdict(result) for result in response.results],
            },
        }
        with target.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        return target


class WebSearchService:
    def __init__(self, client: SearxNGClient, trace_store: TraceStore):
        self.client = client
        self.trace_store = trace_store

    def execute(self, request: SearchRequest, mode: SearchMode, approved_for_task: bool) -> SearchResponse:
        if not user_has_enabled_search(mode, approved_for_task):
            raise SearchUnavailableError("Web search is disabled. Enable it or approve this task before searching.")
        response = self.client.search(request)
        self.trace_store.record(request.task_id, response)
        return response

