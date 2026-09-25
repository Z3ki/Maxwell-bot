from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from plugins.maxwell_extras import autonomy_routing
from plugins.maxwell_extras import maxwell_embed_output
from plugins.maxwell_extras import taint_gate_cleanup


def test_maxwell_slash_reply_uses_embed_shape():
    interaction = SimpleNamespace(data={"name": "maxwell", "type": 1})
    assert maxwell_embed_output._is_maxwell_slash(interaction)

    bot = SimpleNamespace(
        user=SimpleNamespace(
            display_name="Maxwell el gato",
            display_avatar=SimpleNamespace(url="https://example.com/maxwell.png"),
        )
    )
    embed = maxwell_embed_output._reply_embed("hello", bot)
    assert embed.description == "hello"
    assert embed.colour.value == 0x5865F2
    assert embed.author.name == "Maxwell el gato"
    assert str(embed.author.icon_url) == "https://example.com/maxwell.png"
    assert embed.footer.text is None


def test_context_action_is_not_treated_as_maxwell_slash():
    interaction = SimpleNamespace(data={"name": "Ask Maxwell", "type": 3})
    assert not maxwell_embed_output._is_maxwell_slash(interaction)


def test_autonomy_route_rejects_context_only_guild_channel():
    bot = SimpleNamespace(
        _auto_channels={"100"},
        get_channel=lambda _cid: SimpleNamespace(guild=SimpleNamespace(id=9)),
    )
    engine = SimpleNamespace(
        bot=bot,
        _context_index=SimpleNamespace(kind_by_id={"200": "guild"}),
        _channel_allowed=lambda _cid: True,
    )

    allowed, cid, reason = autonomy_routing._route_allowed(engine, "200")
    assert allowed is False
    assert cid == "200"
    assert "context-only" in reason


def test_autonomy_route_accepts_configured_guild_channel():
    bot = SimpleNamespace(
        _auto_channels={"100"},
        get_channel=lambda _cid: SimpleNamespace(guild=SimpleNamespace(id=9)),
    )
    engine = SimpleNamespace(
        bot=bot,
        _context_index=SimpleNamespace(kind_by_id={"100": "guild"}),
        _channel_allowed=lambda _cid: True,
    )

    allowed, cid, reason = autonomy_routing._route_allowed(engine, "100")
    assert allowed is True
    assert cid == "100"
    assert reason == ""


def test_autonomy_reply_must_stay_in_source_channel():
    index = SimpleNamespace(
        msg_idx_by_id={"555": 1},
        message_channel_by_idx={1: "100"},
    )
    engine = SimpleNamespace(_context_index=index)
    ok, reason = autonomy_routing._reply_matches_route(
        engine,
        {"reply_to_message_id": "555"},
        "200",
    )
    assert ok is False
    assert "channel 100" in reason


def test_confirm_command_is_removed_and_tainted_destructive_tool_is_blocked():
    async def run():
        original_command = AsyncMock(return_value="handled")
        original_execute = AsyncMock(return_value="Tool shell: executed")
        bot = SimpleNamespace(
            command_prefix=",",
            _handle_command=original_command,
            _execute_tool_by_name=original_execute,
            tools={"shell": SimpleNamespace(is_destructive=True)},
            config=SimpleNamespace(DISABLE_TAINT_GATE=False),
            is_message_tainted=lambda _message: True,
            _destructive_confirm={"7": 123.0},
        )
        taint_gate_cleanup.install_taint_gate_cleanup(bot)

        message = SimpleNamespace(content=",confirm")
        assert await bot._handle_command(message) is None
        original_command.assert_not_awaited()
        assert bot._destructive_confirm == {}

        result = await bot._execute_tool_by_name(
            message,
            "shell",
            {},
            disabled=set(),
            compatible={"shell"},
        )
        assert "fresh message" in result
        assert ",confirm" not in result
        original_execute.assert_not_awaited()

    asyncio.run(run())


def test_clean_turn_still_runs_destructive_tool_normally():
    async def run():
        original_execute = AsyncMock(return_value="Tool shell: executed")
        bot = SimpleNamespace(
            command_prefix=",",
            _handle_command=AsyncMock(return_value="handled"),
            _execute_tool_by_name=original_execute,
            tools={"shell": SimpleNamespace(is_destructive=True)},
            config=SimpleNamespace(DISABLE_TAINT_GATE=False),
            is_message_tainted=lambda _message: False,
            _destructive_confirm={},
        )
        taint_gate_cleanup.install_taint_gate_cleanup(bot)
        message = SimpleNamespace(content="run it")

        result = await bot._execute_tool_by_name(
            message,
            "shell",
            {},
            disabled=set(),
            compatible={"shell"},
        )
        assert result == "Tool shell: executed"
        original_execute.assert_awaited_once()

    asyncio.run(run())
