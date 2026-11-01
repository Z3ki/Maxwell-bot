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
3. Keyless ddgs engines only if `WEB_SEARCH_SCRAPER_FALLBACK=true` and ddgs is installed.

An empty, malformed, rate-limited, timed-out or failed provider falls back to
the next configured provider within the overall search deadline. SearXNG has
its own `SEARXNG_TIMEOUT` deadline (6 seconds by default, clamped to 0.5–10
seconds). When a fallback is configured, SearXNG gets at most half of the
remaining overall budget, leaving time for fallback. Useful partial results
return immediately instead of searching more
engines to fill every slot. Brief provider cooldowns reduce repeated failures.

The scraper fallback defaults to false; ddgs and primp are not imported for
search when it is off. With no SearXNG or Tavily configured, automatic feature
detection disables web search unless the scraper is explicitly enabled and
ddgs is installed. `python3 doctor.py` reports the feature decision.
If providers fail, the tool returns `Web search unavailable: <reason>`; if
they return no usable hits, it returns `No results found for this query`.
No results should be treated as verified current evidence.
Searches use an HTTP connection pool and a separate bounded executor for ddgs;
timed-out threads retain capacity until they actually finish. Distinct queued
searches and cache entries have hard limits. Identical simultaneous queries
share retrieval; cancellation of one requester does not cancel another's
search. Retrieved hit dictionaries are copied before returning to callers.

The model can pass `freshness: live` to bypass completed-cache results,
`recent` for at most two minutes (default), or `stable` for at most thirty minutes.
It can choose `time_range: day|week|month|year` when publication recency matters.
Avoid date filters for maintained compatibility pages: their publication date
can be old while their supported-device list is current.

Tool results report provider, original retrieval time and cache status, plus
publication dates when supplied by a provider. Retrieval time does not certify
the age or accuracy of a claim. Source text remains untrusted, with the
existing SSRF and tool-taint protections. URLs actually supporting the answer
should be cited by the model; the host does not append every retrieved link to
the answer.

## Optional self-hosted SearXNG

For the default Linux host-network Maxwell deployment:

1. Generate a secret with `openssl rand -hex 32`, and save it as
   `SEARXNG_SECRET=...` in `.env`.
2. Set `SEARXNG_URL=http://127.0.0.1:8888` in `.env`.
3. Run `docker compose -f docker-compose.search.yml up -d`.
4. Rebuild/restart Maxwell to load the new settings.

The supplied settings enable JSON, required by `/search?format=json`, disable
the SearXNG limiter, and bind the published port to host loopback. The service
is intended for operator-controlled access, not public exposure. The Compose
file is independent of the bot's compose file; it does not change the bot's
networks or sandbox firewall. For Docker Desktop/bridge deployments, use an
operator-controlled address reachable from the bot container instead of its
own `127.0.0.1`; the supplied loopback-only binding must also be adapted.

Alternatively, use an independently installed host-level SearXNG instance:
enable its JSON search API and set `SEARXNG_URL=http://127.0.0.1:8888` when
it listens on that host address. The search Compose file is not required in
that case.

`SEARXNG_URL` is the instance base URL, not the `/search` URL: Maxwell appends
`/search` and requests JSON without following redirects. The configured
endpoint may be internal, but result URLs and `fetch_url` remain subject to
public-address checks. SearXNG upstream requests use the supplied 3-second
timeout (maximum 4 seconds); Maxwell's own timeout also bounds the request.

SearXNG consumes VPS resources and upstream engines can block automated traffic.
The optional Tavily key uses its account's available quota; configuring it does
not promise free or unlimited searches. Maxwell uses basic search and disables
automatic parameter upgrades. No paid provider is used without its key.

## Tuning and verification

Defaults: `WEB_SEARCH_TIMEOUT=12`, `SEARXNG_TIMEOUT=6`,
`WEB_SEARCH_MAX_PER_TURN=5`, `WEB_SEARCH_CONCURRENCY=4`,
`WEB_SEARCH_MAX_PENDING=32`, `WEB_SEARCH_CACHE_SIZE=256`,
`WEB_FETCH_TIMEOUT=12`. The per-turn limit caps web searches; set
`WEB_SEARCH_MAX_PER_TURN=0` for unlimited searches per turn. The source-page
deadline covers redirects and Jina
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
