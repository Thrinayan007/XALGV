"""Command line smoke test for Module 1."""

from __future__ import annotations

import argparse
import json

from local_agent.web_search import (
    SearchMode,
    SearchPolicyError,
    SearchRequest,
    SearchUnavailableError,
    SearxNGClient,
    TraceStore,
    WebSearchService,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the local agent web-search tool.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    search = subparsers.add_parser("search")
    search.add_argument("query")
    search.add_argument("--mode", choices=[mode.value for mode in SearchMode], default="off")
    search.add_argument("--approved", action="store_true", help="Approve search for this task when mode is ask.")
    search.add_argument("--max-results", type=int, default=5)
    search.add_argument("--task-id", default="cli")
    args = parser.parse_args()

    if args.command == "search":
        service = WebSearchService(SearxNGClient(), TraceStore())
        try:
            response = service.execute(
                SearchRequest(query=args.query, max_results=args.max_results, task_id=args.task_id),
                SearchMode(args.mode),
                args.approved,
            )
        except (SearchUnavailableError, SearchPolicyError) as exc:
            parser.error(str(exc))
        print(json.dumps({"request_id": response.request_id, "query": response.query, "results": [result.__dict__ for result in response.results]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

