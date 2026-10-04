# Model-led current information

Maxwell's model decides whether to call `web_search` or `fetch_url`. There is
no keyword classifier, pre-inference search, separate decision model, or forced
lookup. `/maxwell web: Off` and disabled tools remain enforced by the host.
The current date, tool descriptions and shared prompt explain when changing
facts need verification and how to acknowledge unavailable evidence.

For “latest phone supporting GrapheneOS”, the intended model behavior is to
search, read the current official supported-device page, and cite that evidence.
For a page such as `https://grapheneos.org/faq#supported-devices`, `fetch_url`
starts at the requested HTML section rather than truncating it behind unrelated
FAQ text. This is guidance, not a guarantee that every model will choose tools
correctly; use a capable tool-calling model and verify its behavior in production.

## Search providers

When the model requests a search, Maxwell tries:

1. SearXNG, if `SEARXNG_URL` is configured.
2. Tavily, if `TAVILY_API_KEY` is configured.
3. Keyless ddgs engines, included in the core requirements.

An empty, malformed, rate-limited or failed provider falls back within the same
deadline. Useful partial results return immediately instead of searching more
engines to fill every slot. Brief provider cooldowns reduce repeated failures.
Searches use an HTTP connection pool and a separate bounded executor for ddgs;
timed-out threads retain capacity until they actually finish. Distinct queued
searches, cache entries and optional memory-write tasks all have hard limits.
Identical simultaneous queries share retrieval; cancellation of one requester
does not cancel another's search. Retrieved hit dictionaries are copied before
returning to callers, and memory persistence remains requester-scoped.

The model can pass `freshness: live` to bypass completed-cache results,
`recent` for at most two minutes (default), or `stable` for at most thirty minutes.
It can choose `time_range: day|week|month|year` when publication recency matters.
Avoid date filters for maintained compatibility pages: their publication date
can be old while their supported-device list is current.

Tool results report provider, original retrieval time and cache status, plus
publication dates when supplied by a provider. Retrieval time does not certify
the age or accuracy of a claim. Earlier RAG web results are labeled historical
context. Source text remains untrusted, with the existing SSRF and tool-taint
protections. URLs actually supporting the answer should be cited by the model;
the host does not append every retrieved link to the answer.

## Optional self-hosted SearXNG

For the default Linux host-network Maxwell deployment:

1. Generate a secret with `openssl rand -hex 32`, and save it as
   `SEARXNG_SECRET=...` in `.env`.
2. Set `SEARXNG_URL=http://127.0.0.1:8888` in `.env`.
3. Run `docker compose -f docker-compose.search.yml up -d`.
4. Rebuild/restart Maxwell to load the new settings.

The supplied settings enable JSON, required by `/search?format=json`, and bind
the published port to host loopback. This compose file is independent of the
bot's compose file; it does not change the bot's networks or sandbox firewall.
For Docker Desktop/bridge deployments, use an operator-controlled address
reachable from the bot container instead of its own `127.0.0.1`.

SearXNG consumes VPS resources and upstream engines can block automated traffic.
The optional Tavily key uses its account's available quota; configuring it does
not promise free or unlimited searches. Maxwell uses basic search and disables
automatic parameter upgrades. No paid provider is used without its key.

## Tuning and verification

Defaults: `WEB_SEARCH_TIMEOUT=12`, `WEB_SEARCH_CONCURRENCY=4`,
`WEB_SEARCH_MAX_PENDING=32`, `WEB_SEARCH_CACHE_SIZE=256`,
`WEB_FETCH_TIMEOUT=12`. The source-page deadline covers redirects and Jina
fallback. Oversized pages and HTTP 403/429 can use the existing Jina Reader
fallback; private/internal URLs remain refused. Authorization headers are
removed on cross-origin redirects.

Run `python -m pytest tests/test_realtime_search.py tests/test_web_search_query.py
tests/test_fetch_url.py` for provider, cancellation, cache, saturation and
source-text regressions. Then run `ruff check .` and the complete test suite.

Protocol references: [SearXNG Search API](https://docs.searxng.org/dev/search_api.html),
[SearXNG server settings](https://docs.searxng.org/admin/settings/settings_server.html),
[Tavily Search](https://help.tavily.com/articles/4840311948-tavily-search-api),
[ddgs](https://github.com/deedy5/ddgs).
