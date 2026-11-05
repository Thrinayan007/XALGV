# Local Agent Web Search Module

Module 1 supplies a local, auditable public-web search capability. It is deliberately limited to search-result retrieval; it does not open pages, log in, solve CAPTCHAs, download files, upload files, or submit forms.

## First milestone

```text
question -> agent tool registry -> web_search -> SearXNG -> normalized results + trace
```

The search tool is exposed only when the user enables it:

- `off`: no search tool is available to the model.
- `ask`: the caller must approve the current task before search is available.
- `on`: the search tool is available for the current task.

## Local setup

1. Start SearXNG:

   ```powershell
   docker compose up -d searxng
   ```

2. Run the test suite:

   ```powershell
   python -m unittest discover -s tests -v
   ```

3. Run a search after enabling it:

   ```powershell
   python -m local_agent.main search "local agent tool calling" --mode on
   ```

The default endpoint is `http://localhost:8080`. Override it with `SEARXNG_BASE_URL` when needed.

## Safety boundary

`web_search` accepts a bounded query and returns only normalized result metadata. It enforces a result limit, a timeout, an endpoint policy, and trace recording. The next milestone will add safe page reading and citation extraction; it is intentionally not included here.

