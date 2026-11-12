"""Web searches have no per-turn limit."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import bot_parts.tool_calls as tool_calls
from bot import MaxwellBot


def _call(name, index):
    return {"id": str(index), "type": "function", "function": {"name": name, "arguments": "{}"}}


def _fixture(monkeypatch):
    tools = {name: SimpleNamespace(side_effects=False, is_destructive=False)
             for name in ("web_search", "fetch_url")}
    bot = SimpleNamespace(
        _control={}, tools=tools, config=SimpleNamespace(WEB_SEARCH_MAX_PER_TURN=1),
        _tool_breaker=SimpleNamespace(is_open=lambda name: False,
                                      record_failure=lambda name: None,
                                      record_success=lambda name: None),
    )
    message = SimpleNamespace(id=7, channel=SimpleNamespace(id=99), guild=None)
    invoked = []

    async def invoke(self, message, name, handler, **params):
        invoked.append(name)
        await asyncio.sleep(0)
        return "result"

    monkeypatch.setattr(MaxwellBot, "_invoke_request_tool", invoke)
    monkeypatch.setattr(MaxwellBot, "_remember_tool_call", AsyncMock())
    monkeypatch.setattr(MaxwellBot, "_request_state", AsyncMock(return_value={}))
    monkeypatch.setattr(tool_calls, "record_reasoning", AsyncMock())
    return bot, message, invoked


def test_many_searches_across_batches_all_run(monkeypatch):
    bot, message, invoked = _fixture(monkeypatch)

    async def run():
        for batch in range(3):
            calls = [_call("web_search", batch * 10 + i) for i in range(8)]
            calls.append(_call("fetch_url", batch * 10 + 9))
            _, results = await MaxwellBot._dispatch_tool_calls(
                bot, message, "", native_tool_calls=calls,
            )
            assert len(results) == 9
            assert not any("Search limit reached" in line for line in results)
        # A stale WEB_SEARCH_MAX_PER_TURN setting is ignored.
        assert invoked.count("web_search") == 24
        assert invoked.count("fetch_url") == 3

    asyncio.run(run())


def test_dispatch_no_longer_accepts_a_search_budget():
    import inspect

    for fn in (MaxwellBot._dispatch_tool_calls,):
        assert "web_search_budget" not in inspect.signature(fn).parameters


def test_config_has_no_per_turn_search_limit():
    from config import Config

    assert not hasattr(Config, "WEB_SEARCH_MAX_PER_TURN")
