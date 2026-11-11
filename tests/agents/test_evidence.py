import json

import pytest

from eval.multi_agent import contract, summarize
from local_agent.agents.definitions import default_registry
from local_agent.agents.evidence import SourceManifest, markdown_evidence
from local_agent.agents.task_graph import TaskGraph, WorkflowRequest


def test_markdown_citations_are_parsed_as_links():
    title, urls = markdown_evidence("# Summary\n[Python][src]\n\n[src]: https://docs.python.org/3/tutorial/\n")
    assert title == {"Summary"} and urls == {"https://docs.python.org/3/tutorial/"}
    assert markdown_evidence("# Summary\n`[Fake](https://example.org/)`")[1] == set()
    with pytest.raises(ValueError, match="raw HTML"):
        markdown_evidence("<!-- [Hidden](https://example.org/) -->")


def test_provenance_rejects_local_or_credentialed_urls():
    record = {"source_id": "src_001", "title": "source", "provider_rank": 1,
              "snippet": "", "retrieved_at": "2026-11-09T10:00:00Z"}
    for url in ("http://127.0.0.1/private", "http://localhost/private", "https://user:secret@example.com/"):
        with pytest.raises(ValueError):
            SourceManifest.model_validate_json(json.dumps({
                "provider": "searxng", "request_id": "request", "query": "research", "sources": [{**record, "url": url}],
            }))


def test_release_gate_separates_smoke_success_from_release_qualification():
    report = {"results": [
        {"mode": mode, "passed": True, "seconds": duration, "model_calls": 3, "tokens": 200}
        for mode, duration in (("single", 10), ("multi", 14))
    ], "summary": {}}
    summarize(report)
    assert report["summary"]["multi"]["pass_rate"] == 1
    assert report["release_gate"]["checks"]["sample_size"] is False
    assert report["release_gate"]["checks"]["latency_vs_baseline"] is False
    assert not report["release_gate"]["passed"]


def test_generic_writer_cannot_be_admitted_as_a_provenance_producer():
    raw = contract("http://127.0.0.1:8765", "test_run").model_dump(mode="json")
    task = raw["tasks"][0]
    task["task"]["agent"] = "coding"
    task["tool_allowlist"] = task["required_tools"] = ["write_file"]
    registry = default_registry()
    with pytest.raises(ValueError, match="Source manifests require"):
        TaskGraph(WorkflowRequest.model_validate_json(json.dumps(raw)), registry,
                  lambda role: registry.require(role).allowed_tools)
