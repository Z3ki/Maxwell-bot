"""Hermetic search-provider, backpressure, cancellation and evidence regressions."""

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from bot_tools import WebSearchTool
from web_search import SearchHTTPError, SearchService, normalize_hit


HIT = {
    "title": "Official devices",
    "url": "https://example.org/devices",
    "content": "Supported devices from the current official page.",
}


async def _server_search(handler, **settings):
    app = web.Application()
    app.router.add_get("/search", handler)
    async with TestServer(app) as server:
        service = SearchService(
            searxng_url=str(server.make_url("/")).rstrip("/"), **settings
        )
        try:
            return await service.search(
                "GrapheneOS supported devices", 5, ["duckduckgo"], time_range="week"
            )
        finally:
            await service.close()


def test_searxng_json_protocol_normalization_and_public_hits():
    async def handler(request):
        assert request.query["format"] == "json"
        assert request.query["time_range"] == "week"
        assert request.query["q"] == "GrapheneOS supported devices"
        return web.json_response(
            {
                "results": [
                    HIT,
                    HIT,
                    {"url": "http://127.0.0.1/secret", "content": "bad"},
                    {"url": "javascript:bad", "content": "bad"},
                ]
            }
        )

    result = asyncio.run(_server_search(handler))
    assert result.provider == "searxng"
    assert len(result.hits) == 1
    assert result.hits[0]["href"] == HIT["url"]
    assert result.retrieved_at.endswith("+00:00")


def test_provider_fallback_after_empty_malformed_and_failed_results():
    async def scenario():
        class ScriptedService(SearchService):
            async def _http(self, provider, *_args):
                calls.append(provider)
                if provider == "searxng":
                    raise SearchHTTPError("HTTP 429")
                return [{"url": "https://example.org/empty"}]

            async def _ddgs(self, *args):
                calls.append("ddgs:" + args[3])
                return [HIT]

        calls = []
        service = ScriptedService(
            searxng_url="http://searxng:8080", tavily_key="private-key",
            scraper_fallback=True,
        )
        try:
            result = await service.search("devices", 5, ["duckduckgo", "bing"], object)
            assert result.hits
            assert calls == ["searxng", "tavily", "ddgs:duckduckgo"]
            # A useful partial result returns immediately, without filling 5 slots.
            assert result.provider == "ddgs:duckduckgo"
            assert "private-key" not in str(result)
        finally:
            await service.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["empty", "invalid_hits", "exception", "timeout"])
def test_searxng_falls_back_for_all_failure_modes(failure, caplog):
    async def scenario():
        calls = []

        class Service(SearchService):
            async def _http(self, provider, query, limit, timeout, time_range):
                calls.append((provider, timeout))
                if provider == "tavily":
                    return [HIT]
                if failure == "timeout":
                    await asyncio.sleep(1)
                if failure == "exception":
                    raise RuntimeError("secret-query secret-key")
                if failure == "invalid_hits":
                    return [{"url": "http://127.0.0.1/", "content": "private"}]
                return []

        service = Service(
            searxng_url="http://secret-endpoint",
            searxng_timeout=10,
            tavily_key="secret-key",
            budget=0.12,
        )
        try:
            result = await service.search("secret-query", 1, [], freshness="live")
            assert result.provider == "tavily"
            assert [name for name, _ in calls] == ["searxng", "tavily"]
            assert 0 < calls[0][1] <= 0.06
            assert calls[1][1] > 0
        finally:
            await service.close()

    import logging
    with caplog.at_level(logging.INFO, logger="web_search"):
        asyncio.run(scenario())
    assert "provider=searxng" in caplog.text
    assert "provider=tavily" in caplog.text
    assert "latency_ms=" in caplog.text
    assert "secret-query" not in caplog.text
    assert "secret-key" not in caplog.text
    assert "secret-endpoint" not in caplog.text


def test_searxng_primary_and_timeout_clamping():
    async def scenario():
        calls = []

        class Service(SearchService):
            async def _http(self, provider, query, limit, timeout, time_range):
                calls.append((provider, timeout))
                return [HIT]

        service = Service(
            searxng_url="http://searxng", searxng_timeout=100,
            tavily_key="key", budget=30,
        )
        try:
            result = await service.search("query", 1, ["bing"], object)
            assert result.provider == "searxng"
            assert calls == [("searxng", 10.0)]
            low_timeout = SearchService(searxng_timeout=0.01)
            try:
                assert low_timeout.searxng_timeout == 0.5
            finally:
                await low_timeout.close()
        finally:
            await service.close()

    asyncio.run(scenario())


def test_compatibility_helper_uses_configured_providers(monkeypatch):
    from config import Config
    from tooling.helpers import _web_search_collect
    import web_search

    captured = {}

    class Service:
        def __init__(self, **settings):
            captured.update(settings)

        async def search(self, *args):
            return web_search.SearchResult(hits=(HIT,))

        async def close(self):
            captured["closed"] = True

    monkeypatch.setattr(Config, "SEARXNG_URL", "http://searxng")
    monkeypatch.setattr(Config, "SEARXNG_TIMEOUT", 3.0)
    monkeypatch.setattr(Config, "TAVILY_API_KEY", "key")
    monkeypatch.setattr(Config, "WEB_SEARCH_SCRAPER_FALLBACK", True)
    monkeypatch.setattr(web_search, "SearchService", Service)
    hits, errors = asyncio.run(_web_search_collect(None, "query", 1, []))
    assert hits == [HIT]
    assert not errors
    assert captured["searxng_url"] == "http://searxng"
    assert captured["searxng_timeout"] == 3.0
    assert captured["tavily_key"] == "key"
    assert captured["scraper_fallback"] is True
    assert captured["closed"]


@pytest.mark.parametrize("cancelled", [False, True])
def test_search_metrics_exclude_query_and_preserve_cancellation(monkeypatch, tmp_path, cancelled):
    import usage_metrics
    from web_search import SearchResult

    events = []
    monkeypatch.setattr(usage_metrics, "record_event", lambda *args, **fields: events.append((args, fields)))

    class Service:
        async def search(self, *args, **kwargs):
            if cancelled:
                raise asyncio.CancelledError
            return SearchResult(errors=("provider failed",))

    bot = SimpleNamespace(config=SimpleNamespace(DATA_DIR=tmp_path))
    tool = WebSearchTool(bot)
    tool._search_service = Service()
    message = SimpleNamespace(author=SimpleNamespace(id=123), guild=None, channel=None)

    async def invoke():
        if cancelled:
            with pytest.raises(asyncio.CancelledError):
                await tool.execute(message, query="private search content")
        else:
            await tool.execute(message, query="private search content")

    asyncio.run(invoke())
    assert len(events) == 1
    args, fields = events[0]
    assert args == (tmp_path, "search")
    assert fields["outcome"] == ("cancelled" if cancelled else "failed")
    assert fields["success"] is False
    assert fields["queries"] == 1 and fields["results"] == 0
    assert "private search content" not in repr(events)


@pytest.mark.parametrize("kind", ["malformed", "oversized", "redirect"])
def test_http_response_is_bounded_and_redirects_are_not_followed(kind):
    async def handler(_request):
        if kind == "malformed":
            return web.json_response([HIT])
        if kind == "oversized":
            return web.Response(body=b"x" * (512 * 1024 + 1))
        raise web.HTTPFound("http://127.0.0.1/secret")

    result = asyncio.run(_server_search(handler))
    assert not result.hits
    assert result.errors


def test_tavily_uses_fixed_endpoint_basic_search_and_header_credentials():
    async def scenario():
        captured = {}

        class Response:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                pass

            @property
            def content(self):
                return self

            async def iter_chunked(self, _size):
                yield json.dumps({"results": [HIT]}).encode()

        class Session:
            closed = False

            def post(self, url, **kwargs):
                captured.update(url=url, **kwargs)
                return Response()

            async def close(self):
                pass

        service = SearchService(tavily_key="tvly-secret")
        service._session = Session()
        try:
            result = await service.search("release", 3, [], time_range="day")
            assert result.provider == "tavily"
            assert captured["url"] == "https://api.tavily.com/search"
            assert captured["headers"] == {"Authorization": "Bearer tvly-secret"}
            assert captured["json"]["time_range"] == "day"
            assert captured["json"]["search_depth"] == "basic"
            assert captured["json"]["auto_parameters"] is False
            assert "api_key" not in captured["json"]
            assert captured["allow_redirects"] is False
            assert "tvly-secret" not in str(result)
        finally:
            await service.close()

    asyncio.run(scenario())


def test_cache_copies_hits_and_live_bypasses_completed_cache(caplog):
    async def scenario():
        calls = []

        class Service(SearchService):
            async def _http(self, provider, *_args):
                calls.append(provider)
                return [HIT]

        service = Service(searxng_url="http://search", cache_size=1)
        try:
            first = await service.search("devices", 5, [])
            first.hits[0]["body"] = "mutated by one caller"
            second = await service.search("devices", 5, [])
            assert second.cached
            assert second.hits[0]["body"] == HIT["content"]
            assert second.retrieved_at == first.retrieved_at
            await service.search("devices", 5, [], freshness="live")
            await service.search("devices", 5, [], freshness="live")
            assert len(calls) == 3
            await service.search("other", 5, [])
            assert len(service._cache) == 1
            await service.search("devices", 5, [])
            assert len(calls) == 5
        finally:
            await service.close()

    import logging
    with caplog.at_level(logging.INFO, logger="web_search"):
        asyncio.run(scenario())
    assert "search caller provider=searxng source=cache latency_ms=" in caplog.text
    assert "devices" not in caplog.text


def test_same_query_is_coalesced_and_one_cancellation_does_not_cancel_others(caplog):
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        calls = []

        class Service(SearchService):
            async def _http(self, provider, *_args):
                calls.append(provider)
                started.set()
                await release.wait()
                return [HIT]

        service = Service(searxng_url="http://search")
        try:
            alice = asyncio.create_task(service.search("devices", 5, []))
            await started.wait()
            bob = asyncio.create_task(service.search("devices", 5, []))
            await asyncio.sleep(0)
            alice.cancel()
            with pytest.raises(asyncio.CancelledError):
                await alice
            release.set()
            assert (await bob).hits
            assert calls == ["searxng"]
        finally:
            await service.close()

    import logging
    with caplog.at_level(logging.INFO, logger="web_search"):
        asyncio.run(scenario())
    assert "search caller provider=searxng source=shared latency_ms=" in caplog.text
    assert "devices" not in caplog.text


def test_distinct_pending_queries_are_bounded_but_duplicates_can_join():
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()

        class Service(SearchService):
            async def _http(self, *_args):
                started.set()
                await release.wait()
                return [HIT]

        service = Service(searxng_url="http://search", concurrency=1, max_pending=1)
        try:
            first = asyncio.create_task(service.search("one", 5, []))
            await started.wait()
            overflow = await service.search("two", 5, [])
            assert "capacity" in overflow.errors[0]
            duplicate = asyncio.create_task(service.search("one", 5, []))
            release.set()
            assert all(result.hits for result in await asyncio.gather(first, duplicate))
        finally:
            await service.close()

    asyncio.run(scenario())


def test_timeout_retains_ddgs_worker_capacity_until_thread_exits():
    async def scenario():
        started, release = threading.Event(), threading.Event()
        calls = []
        finished = asyncio.Event()
        loop = asyncio.get_running_loop()

        class SlowDDGS:
            def __init__(self, **_kwargs):
                pass

            def text(self, query, **_kwargs):
                calls.append(query)
                started.set()
                release.wait(timeout=2)
                loop.call_soon_threadsafe(finished.set)
                return [HIT]

        service = SearchService(budget=0.04, concurrency=1, scraper_fallback=True)
        try:
            first = asyncio.create_task(
                service.search("one", 1, ["duckduckgo"], SlowDDGS)
            )
            # This also verifies the default executor remains available while
            # the search thread is blocked.
            assert await asyncio.to_thread(started.wait, 1)
            assert not (await first).hits
            assert not (await service.search("two", 1, ["bing"], SlowDDGS)).hits
            assert calls == ["one"]
            release.set()
            await finished.wait()
            await asyncio.sleep(0.01)
            third = await service.search("three", 1, ["startpage"], SlowDDGS)
            assert third.hits
            assert calls == ["one", "three"]
        finally:
            release.set()
            await service.close()

    asyncio.run(scenario())


def test_deadline_includes_queueing_and_close_cancels_shared_work():
    async def scenario():
        class Service(SearchService):
            async def _http(self, *_args):
                await asyncio.Event().wait()

        service = Service(searxng_url="http://search", budget=0.03, concurrency=1)
        results = await asyncio.gather(
            *(service.search(str(i), 5, []) for i in range(8))
        )
        assert all(not result.hits for result in results)
        assert all("deadline" in " ".join(result.errors) for result in results)
        await service.close()
        assert not service._inflight
        assert "closed" in (await service.search("later", 5, [])).errors[0]

    asyncio.run(scenario())


def test_searxng_works_without_ddgs_and_does_not_persist_web_results(
    monkeypatch,
):

    async def scenario():
        stored = []

        class Memory:
            async def store_web_results(self, **kwargs):
                stored.append(kwargs)

        async def handler(_request):
            return web.json_response({"results": [HIT]})

        app = web.Application()
        app.router.add_get("/search", handler)
        async with TestServer(app) as server:
            bot = SimpleNamespace(
                config=SimpleNamespace(
                    SEARXNG_URL=str(server.make_url("/")).rstrip("/"),
                    SEARXNG_TIMEOUT=2.75,
                ),
                memory=Memory(),
            )
            tool = WebSearchTool(bot)
            try:
                result = await asyncio.wait_for(
                    tool.execute(None, query="devices"), timeout=1
                )
                assert "https://example.org/devices" in result
                assert "retrieved_at=" in result
                assert tool._search_service.searxng_timeout == 2.75
                assert stored == []
            finally:
                await tool.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "file:///etc/passwd",
        "http://127.0.0.1/",
        "http://localhost./",
        "http://224.0.0.1/",
        "http://[fec0::1]/",
        "http://[64:ff9b::7f00:1]/",
        "https://user:secret@example.org/",
        "https://example.org/\nforged",
        "https://[broken/",
    ],
)
def test_invalid_or_non_public_hits_are_not_evidence(url):
    assert normalize_hit({"url": url, "content": "text"}) is None


def test_model_search_instruction_preserves_generation_first_behavior():
    from bot import MaxwellBot
    from web_references import WEB_REFERENCE_INSTRUCTION

    assert not hasattr(MaxwellBot, "_run_preflight_web_search")
    assert not hasattr(MaxwellBot, "_automatic_web_search_query")
    assert "You decide" in WEB_REFERENCE_INSTRUCTION
    assert "supported" in WEB_REFERENCE_INSTRUCTION
    assert "factual follow-up" in WEB_REFERENCE_INSTRUCTION
    assert "web=off" in WEB_REFERENCE_INSTRUCTION


@pytest.mark.parametrize("failure", ["empty", "exception", "timeout"])
def test_disabled_scraper_never_runs_after_http_failure(failure):
    async def scenario():
        calls = []

        class Service(SearchService):
            async def _http(self, provider, *_args):
                calls.append(provider)
                if failure == "exception":
                    raise RuntimeError("provider failed")
                if failure == "timeout":
                    await asyncio.sleep(1)
                return []

            async def _ddgs(self, *_args):
                pytest.fail("disabled scraper must not run")

        service = Service(searxng_url="http://search", tavily_key="key", budget=0.1)
        try:
            result = await service.search("query", 1, ["bing"], object)
            assert not result.hits
            assert calls == ["searxng", "tavily"]
        finally:
            await service.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("fallback", [False, True])
def test_helper_passes_scraper_setting(monkeypatch, fallback):
    from config import Config
    from tooling.helpers import _web_search_collect
    import web_search

    captured = {}

    class Service:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def search(self, *args):
            return web_search.SearchResult(hits=())

        async def close(self):
            pass

    monkeypatch.setattr(Config, "WEB_SEARCH_SCRAPER_FALLBACK", fallback)
    monkeypatch.setattr(web_search, "SearchService", Service)
    asyncio.run(_web_search_collect(None, "query", 1, []))
    assert captured["scraper_fallback"] is fallback


@pytest.mark.parametrize(
    "hits,errors,expected",
    [
        ((), (), "No results found for this query"),
        ((), ("HTTP 429",), "Web search unavailable: "),
    ],
)
def test_plugin_empty_and_error_messages(hits, errors, expected):
    from web_search import SearchResult

    class Service:
        async def search(self, *args, **kwargs):
            return SearchResult(hits=hits, errors=errors)

        async def close(self):
            pass

    async def scenario():
        tool = WebSearchTool(SimpleNamespace(config=SimpleNamespace(SEARXNG_URL="http://search")))
        tool._search_service = Service()
        try:
            result = await tool.execute(None, query="query")
            if errors:
                assert result.startswith(expected)
                assert len(result) < 200
            else:
                assert result == expected
        finally:
            await tool.close()

    asyncio.run(scenario())


def test_disabled_plugin_does_not_import_scraper(monkeypatch):
    import builtins

    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name.split(".")[0] in {"ddgs", "primp"}:
            pytest.fail(f"unexpected scraper import: {name}")
        return original_import(name, *args, **kwargs)

    async def scenario():
        tool = WebSearchTool(SimpleNamespace(config=SimpleNamespace()))
        try:
            result = await tool.execute(None, query="query")
            assert result.startswith("Web search unavailable: ")
        finally:
            await tool.close()

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    asyncio.run(scenario())


@pytest.mark.parametrize(
    "fallback,searx,tavily,installed,enabled",
    [
        ("", "", "", True, False),
        ("false", "", "", True, False),
        ("true", "", "", True, True),
        ("true", "", "", False, False),
        ("", "http://search", "", False, True),
        ("", "", "key", False, True),
    ],
)
def test_search_feature_detection(fallback, searx, tavily, installed, enabled):
    import os
    import subprocess
    import sys

    env = os.environ.copy()
    env.update(
        MAXWELL_ENV_FILE="/tmp/nonexistent",
        ENABLE_WEB_SEARCH="auto",
        WEB_SEARCH_SCRAPER_FALLBACK=fallback,
        SEARXNG_URL=searx,
        TAVILY_API_KEY=tavily,
    )
    script = """
import importlib.util
original = importlib.util.find_spec
def find_spec(name, *args, **kwargs):
    if name == "ddgs":
        return object() if {installed} else None
    return original(name, *args, **kwargs)
importlib.util.find_spec = find_spec
from config import Config
assert Config.WEB_SEARCH_SCRAPER_FALLBACK is {fallback}
assert Config.ENABLE_WEB_SEARCH is {enabled}
""".format(installed=repr(installed), fallback=repr(fallback == "true"), enabled=repr(enabled))
    subprocess.run([sys.executable, "-c", script], env=env, check=True)


def test_search_modules_do_not_eagerly_import_scrapers():
    import os
    import subprocess
    import sys

    env = os.environ.copy()
    env.update(MAXWELL_ENV_FILE="/tmp/nonexistent", WEB_SEARCH_SCRAPER_FALLBACK="false")
    script = """
import builtins
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.split(".")[0] in {"ddgs", "primp"}:
        raise AssertionError("unexpected import: " + name)
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
import web_search
import tooling.helpers
import bot_tools
"""
    subprocess.run([sys.executable, "-c", script], env=env, check=True)
