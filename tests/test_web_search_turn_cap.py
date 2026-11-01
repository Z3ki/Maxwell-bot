"""The search budget belongs to a request, not a batch or the bot."""

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
        _control={}, tools=tools, config=SimpleNamespace(),
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


def test_cap_spans_batches_and_preserves_other_tools_and_call_pairing(monkeypatch):
    bot, message, invoked = _fixture(monkeypatch)

    async def run():
        budget = [5, 0]
        _, first = await MaxwellBot._dispatch_tool_calls(
            bot, message, "", native_tool_calls=[_call("web_search", i) for i in range(4)],
            web_search_budget=budget,
        )
        _, second = await MaxwellBot._dispatch_tool_calls(
            bot, message, "", native_tool_calls=[_call("web_search", 4), _call("search", 5), _call("fetch_url", 6)],
            web_search_budget=budget,
        )
        assert len(first) == 4
        assert sum("Search limit reached" in line for line in second) == 1
        assert len(bot._last_native_followup_messages) == 4
        assert invoked.count("web_search") == 5
        assert invoked.count("fetch_url") == 1
        # A new request gets an independent budget.
        _, third = await MaxwellBot._dispatch_tool_calls(
            bot, message, "", native_tool_calls=[_call("web_search", 7)],
            web_search_budget=[5, 0],
        )
        assert "Search limit reached" not in third[0]
        assert invoked.count("web_search") == 6

    asyncio.run(run())


def test_zero_limit_is_unlimited_and_disabled_calls_do_not_consume_budget(monkeypatch):
    bot, message, invoked = _fixture(monkeypatch)

    async def run():
        budget = [0, 0]
        _, results = await MaxwellBot._dispatch_tool_calls(
            bot, message, "", native_tool_calls=[_call("web_search", i) for i in range(8)],
            web_search_budget=budget,
        )
        assert len(results) == len(invoked) == 8
        limited = [1, 0]
        message.user_install_web_mode = "off"
        await MaxwellBot._dispatch_tool_calls(
            bot, message, "", native_tool_calls=[_call("web_search", 8)],
            web_search_budget=limited,
        )
        assert limited == [1, 0]
        message.user_install_web_mode = "auto"
        _, results = await MaxwellBot._dispatch_tool_calls(
            bot, message, "", native_tool_calls=[_call("web_search", 9)],
            web_search_budget=limited,
        )
        assert "Search limit reached" not in results[0]
        assert limited == [1, 1]

    asyncio.run(run())
