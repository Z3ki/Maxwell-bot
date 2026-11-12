"""A stalled primary hands the turn to the fallback instead of holding it."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock


from maxwell_core.providers.errors import ProviderFirstByteTimeoutError
from maxwell_core.providers.factory import openai_compat_provider
from maxwell_core.providers.models import ProviderCapabilities, ProviderPolicy, ProviderResult
from maxwell_core.providers.routing import ProviderRouter

SSE_OK = b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n'


async def _server(handler):
    from aiohttp import web

    app = web.Application()
    app.router.add_post("/v1/chat/completions", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, site._server.sockets[0].getsockname()[1]


def _sse(request_body_delay=0.0, chunk_delay=0.0):
    from aiohttp import web

    async def handler(request):
        await request.read()
        await asyncio.sleep(request_body_delay)
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        await asyncio.sleep(chunk_delay)
        await response.write(SSE_OK)
        await response.write_eof()
        return response

    return handler


def _router(primary_port, fallback_port, first_byte_timeout):
    router = openai_compat_provider(
        base_url=f"http://127.0.0.1:{primary_port}/v1",
        model="primary-model",
        fallback_base_url=f"http://127.0.0.1:{fallback_port}/v1",
        fallback_model="fallback-model",
        retry_attempts=3,
        empty_response_retries=0,
        first_byte_timeout=first_byte_timeout,
    )
    assert isinstance(router, ProviderRouter)
    for client in router.providers:
        client.available = True
    return router


def test_stalled_primary_fails_over_after_first_byte_bound():
    async def run():
        primary, primary_port = await _server(_sse(request_body_delay=5))
        fallback, fallback_port = await _server(_sse())
        router = _router(primary_port, fallback_port, 0.3)
        try:
            started = time.monotonic()
            result = await router.generate_response([{"role": "user", "content": "hi"}])
            elapsed = time.monotonic() - started
        finally:
            await router.close()
            await primary.cleanup()
            await fallback.cleanup()
        assert result == "ok" and result.provider == "fallback"
        # One bounded primary attempt, no same-upstream retry, then fallback.
        assert elapsed < 3
        assert router._cooldowns.get("primary", 0) > time.monotonic()

    asyncio.run(run())


def test_started_stream_is_not_cut_by_first_byte_bound():
    async def run():
        primary, primary_port = await _server(_sse(chunk_delay=0.6))
        fallback, fallback_port = await _server(_sse())
        router = _router(primary_port, fallback_port, 0.3)
        try:
            result = await router.generate_response([{"role": "user", "content": "hi"}])
        finally:
            await router.close()
            await primary.cleanup()
            await fallback.cleanup()
        assert result == "ok" and result.provider == "primary"

    asyncio.run(run())


class _Recorder:
    supports_first_byte_timeout = True

    def __init__(self, name, outcomes):
        self.name = name
        self.model = name + "-model"
        self.capabilities = ProviderCapabilities()
        self.policy = ProviderPolicy()
        self.outcomes = list(outcomes)
        self.calls = []

    async def generate_response(self, messages, **kwargs):
        self.calls.append(kwargs.get("first_byte_timeout"))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return ProviderResult(outcome, provider=self.name, model=self.model)


def test_bound_applies_only_to_primary_while_an_untried_route_remains(monkeypatch):
    async def no_wait(_seconds):
        pass

    monkeypatch.setattr(asyncio, "sleep", no_wait)
    from maxwell_core.providers.errors import ProviderUnavailableError

    # Night routing: fallback first. Once it has failed, the primary is the
    # last resort and must be allowed its full request timeout.
    primary = _Recorder("primary", ["ok"])
    fallback = _Recorder("fallback", [ProviderUnavailableError("down")] * 2)
    router = ProviderRouter(primary, fallback, retry_attempts=3, first_byte_timeout=45)
    assert asyncio.run(router.generate_response([], prefer_fallback=True)) == "ok"
    assert fallback.calls == [None, None]
    assert primary.calls == [None]

    primary = _Recorder("primary", ["ok"])
    router = ProviderRouter(primary, _Recorder("fallback", []), first_byte_timeout=45)
    assert asyncio.run(router.generate_response([])) == "ok"
    assert primary.calls == [45]

    primary = _Recorder("primary", ["ok"])
    router = ProviderRouter(primary, _Recorder("fallback", []), first_byte_timeout=0)
    assert asyncio.run(router.generate_response([])) == "ok"
    assert primary.calls == [None]


def test_first_byte_timeout_error_cools_the_route():
    error = ProviderFirstByteTimeoutError("Provider primary sent no response within 45s")
    assert error.retryable and error.cooldown
    for word in ("tool", "function", "does not support"):
        assert word not in str(error).lower()


def test_search_cap_refusals_are_not_recorded_as_tool_failures(monkeypatch):
    import bot_parts.tool_calls as tool_calls
    from bot import MaxwellBot

    events = []
    monkeypatch.setattr(
        tool_calls.usage_metrics, "record_event",
        lambda data_dir, kind, **fields: events.append((kind, fields)),
    )
    tools = {"web_search": SimpleNamespace(side_effects=False, is_destructive=False)}
    bot = SimpleNamespace(
        _control={}, tools=tools, config=SimpleNamespace(DATA_DIR="/nonexistent"),
        _tool_breaker=SimpleNamespace(is_open=lambda name: False,
                                      record_failure=lambda name: None,
                                      record_success=lambda name: None),
    )
    message = SimpleNamespace(id=7, channel=SimpleNamespace(id=99), guild=None)

    async def invoke(self, message, name, handler, **params):
        return "result"

    monkeypatch.setattr(MaxwellBot, "_invoke_request_tool", invoke)
    monkeypatch.setattr(MaxwellBot, "_remember_tool_call", AsyncMock())
    monkeypatch.setattr(MaxwellBot, "_request_state", AsyncMock(return_value={}))
    monkeypatch.setattr(tool_calls, "record_reasoning", AsyncMock())
    calls = [
        {"id": str(i), "type": "function", "function": {"name": "web_search", "arguments": "{}"}}
        for i in range(3)
    ]
    asyncio.run(MaxwellBot._dispatch_tool_calls(
        bot, message, "", native_tool_calls=calls, web_search_budget=[2, 0],
    ))
    outcomes = sorted(
        (fields["outcome"], fields["success"]) for kind, fields in events if kind == "tool"
    )
    assert outcomes == [("refused", None), ("returned", None), ("returned", None)]
