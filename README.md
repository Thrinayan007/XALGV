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

## Why pair web search with a local LLM?

Running the LLM and its search gateway locally can be a good fit when control, privacy, and auditability matter:

- **Better privacy:** prompts, search queries, and the resulting metadata can stay on your machine instead of being sent to an LLM provider's server. The search engine still contacts public search sources, so it is not completely offline or anonymous.
- **Clear user control:** this module exposes search only when the user enables or approves it. A local deployment makes it easier to enforce that rule for every task.
- **Auditable behavior:** each search can be recorded in a local trace file, making it possible to review which query was sent and which results were returned.
- **Local safety boundaries:** the client is restricted to a loopback SearXNG endpoint, blocks redirects, and returns only bounded, normalized search-result metadata. You control the configuration and updates.
- **Independence and customization:** you can choose the model, tune policies, change retention rules, and keep the system usable even when a hosted LLM service is unavailable.
- **Potentially lower ongoing cost:** after purchasing suitable hardware, local inference can avoid per-token hosted-model charges for frequent or sensitive workloads.

The trade-offs are that local models may need capable hardware, setup and maintenance are your responsibility, and a hosted model can be more capable or convenient. Also, web search itself requires network access; keeping the LLM local does not prevent SearXNG or its upstream engines from receiving the search query.

## Safety boundary

`web_search` accepts a bounded query and returns only normalized result metadata. It enforces a result limit, a timeout, an endpoint policy, and trace recording. The next milestone will add safe page reading and citation extraction; it is intentionally not included here.

