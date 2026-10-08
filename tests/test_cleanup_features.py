from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from plugins.maxwell_extras import autonomy_routing
from plugins.maxwell_extras import maxwell_embed_output
from plugins.maxwell_extras import taint_gate_cleanup


def test_maxwell_slash_reply_uses_plain_text(monkeypatch):
    import user_install as ui
    monkeypatch.setattr(ui, "_SEND_WRAPPERS", [])
    monkeypatch.setattr(ui.UserInstallSession, "send", ui.UserInstallSession._send_impl)
    maxwell_embed_output.install_maxwell_embed_output(SimpleNamespace())
    followup = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=1)))
    interaction = SimpleNamespace(data={"name": "maxwell", "type": 1}, followup=followup,
                                  response=SimpleNamespace(is_done=lambda: True))
    session = ui.UserInstallSession(interaction, visibility="public")
    asyncio.run(session.send("hello"))
    payload = followup.send.call_args.kwargs
    assert payload["content"] == "hello"
    assert "embed" not in payload
    assert payload["ephemeral"] is False


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


def test_tainted_destructive_tool_is_blocked_and_confirm_state_is_cleared():
    async def run():
        original_execute = AsyncMock(return_value="Tool shell: executed")
        bot = SimpleNamespace(
            command_prefix=",",
            _execute_tool_by_name=original_execute,
            tools={"shell": SimpleNamespace(is_destructive=True)},
            config=SimpleNamespace(DISABLE_TAINT_GATE=False),
            is_message_tainted=lambda _message: True,
            _destructive_confirm={"7": 123.0},
        )
        taint_gate_cleanup.install_taint_gate_cleanup(bot)

        message = SimpleNamespace(content=",confirm")
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
