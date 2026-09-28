"""Security gates that must hold even when a tool is invoked directly."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from bot import (
    MaxwellBot,
    _current_byok_tool_budget,
    _current_request_provider,
)


def _message(*, visibility="public"):
    return SimpleNamespace(
        id="request-1",
        author=SimpleNamespace(id=123),
        guild=None,
        channel=SimpleNamespace(id=456),
        response_visibility=visibility,
    )


def _tool(name="read_data"):
    class Tool:
        tool_name = name

        def get_parameters(self):
            return {"type": "object", "properties": {}}

        async def execute(self, *_args, **_kwargs):
            return "executed"

    return Tool()


def _bot(*, enabled=True):
    bot = object.__new__(MaxwellBot)
    bot._control = {"tools_enabled": enabled, "disabled_tools": []}
    bot.plugin_manager = None
    return bot


def test_direct_tool_authorization_obeys_global_tools_off_switch():
    bot = _bot(enabled=False)
    denied = MaxwellBot._authorize_tool_execution(
        bot, _message(), "read_data", _tool(), {}
    )
    assert denied == "refused: tools are disabled"


def test_private_reminders_are_blocked_as_later_public_side_effects():
    bot = _bot()
    denied = MaxwellBot._authorize_tool_execution(
        bot, _message(visibility="private"), "reminder", _tool("reminder"), {}
    )
    assert denied and "choose Public visibility" in denied


def test_byok_budget_counts_parallel_model_tool_calls_as_one_shared_budget():
    bot = _bot()
    handler = _tool()
    calls = []

    async def execute(*_args, **_kwargs):
        await asyncio.sleep(0)
        calls.append(1)
        return "ok"

    handler.execute = execute
    message = _message()

    async def run():
        provider_token = _current_request_provider.set(object())
        budget_token = _current_byok_tool_budget.set({"calls": 0})
        try:
            outcomes = await asyncio.gather(
                *(
                    MaxwellBot._invoke_request_tool(
                        bot, message, "read_data", handler
                    )
                    for _ in range(12)
                ),
                return_exceptions=True,
            )
            assert sum(isinstance(item, PermissionError) for item in outcomes) == 4
            assert len(calls) == 8
            assert _current_byok_tool_budget.get()["calls"] == 8
        finally:
            _current_byok_tool_budget.reset(budget_token)
            _current_request_provider.reset(provider_token)

    asyncio.run(run())


@pytest.mark.parametrize("tool_name", ["see_image", "see_video"])
def test_nested_media_tool_delegation_rechecks_tool_policy(tool_name):
    from plugins.web.impl import _authorize_nested_media

    calls = []
    media_tool = SimpleNamespace(tool_name=tool_name, execute=lambda *_a, **_k: calls.append(1))
    bot = SimpleNamespace(
        tools={tool_name: media_tool},
        _authorize_tool_execution=lambda *_args: "refused: this tool is disabled",
    )
    tool, denied = _authorize_nested_media(
        bot, _message(), tool_name, "https://media.example/item"
    )
    assert tool is None
    assert denied == "refused: this tool is disabled"
    assert calls == []
