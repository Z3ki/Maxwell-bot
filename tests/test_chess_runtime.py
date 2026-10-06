"""Chess tools through Maxwell's real request dispatch, with Discord I/O faked."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import chess
import pytest

import chess_game
from bot import MaxwellBot, ToolCircuitBreaker
from plugins.chess import setup
from plugins.chess import impl


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    manager = chess_game.ChessManager(store_path=str(tmp_path / "games.json"))
    monkeypatch.setattr(impl, "_chess_get_manager", lambda: manager)
    bot = SimpleNamespace(
        config=SimpleNamespace(DATA_DIR=str(tmp_path), DISABLE_TAINT_GATE=False),
        user=SimpleNamespace(id=999, name="Maxwell"),
        _tool_breaker=ToolCircuitBreaker(failure_threshold=100),
        tool_concurrency=None,
        hooks=None,
        memory=None,
    )
    tools = setup(bot)
    bot.tools = {tool.name: tool for tool in tools}
    bot.plugin_manager = SimpleNamespace(
        get_tool=lambda name: bot.tools.get(name),
        get_available_tools=lambda **_kw: bot.tools,
    )
    bot._message_tool_platform = lambda _message: "discord"
    bot.is_message_tainted = lambda _message: False
    bot._record_llm_trace = AsyncMock()

    def message(channel_id=10, author_id=1):
        channel = SimpleNamespace(id=channel_id)
        channel.send = AsyncMock(return_value=SimpleNamespace(attachments=[]))
        return SimpleNamespace(
            id=channel_id * 1000 + author_id,
            channel=channel,
            author=SimpleNamespace(id=author_id, name="Player", display_name="Player"),
            mentions=[],
            guild=None,
            content="let's play chess",
        )

    async def call(msg, name, **params):
        return await MaxwellBot._execute_tool_by_name(
            bot, msg, name, params, disabled=set(), compatible=set(bot.tools)
        )

    return manager, message, call


def test_dispatch_lifecycle_multiple_channels_and_restart(runtime, tmp_path):
    manager, message, call = runtime

    async def run():
        first = message()
        second = message(channel_id=11, author_id=2)
        started = await call(first, "chess_start", bot_side="black")
        assert "Tool chess_start:" in started
        assert "NameError" not in started
        assert "Board image posted" in started
        assert first.channel.send.await_count == 1
        assert manager.active("10").fen == chess.Board().fen()
        assert "already active" in await call(first, "chess_start")
        assert "depth must be an integer" in await call(second, "chess_start", depth="oops")
        assert manager.active("11") is None
        assert "Game started" in await call(second, "chess_start", bot_side="white")
        assert "belongs to" in await call(message(author_id=3), "chess_move", move="e4")
        assert "Error:" in await call(first, "chess_move", move="e5")
        assert manager.active("10").fen == chess.Board().fen()
        assert "Played move(s): e4" in await call(first, "chess_move", move="e4")
        assert manager.active("10").history_san == ["e4"]
        # Second channel remains independent and survives a manager reload.
        assert manager.active("11").history_san == []
        reloaded = chess_game.ChessManager(store_path=manager._store)
        assert reloaded.active("10").history_san == ["e4"]
        assert "FEN" in (await call(first, "chess_state")).upper()
        assert "Game ended" in await call(first, "chess_resign")
        assert manager.active("10") is None
        assert manager.active("11") is not None

    asyncio.run(run())


def test_missing_optional_dependency_disables_registration(runtime, monkeypatch):
    manager, message, call = runtime
    monkeypatch.setattr("tooling.helpers.__CHESS_IMPORTED__", False)
    assert setup(SimpleNamespace(config=None)) == []
    # The existing registered tool also fails usefully if availability changes.
    monkeypatch.setattr(impl, "__CHESS_IMPORTED__", False)
    assert asyncio.run(call(message(), "chess_start")).startswith(
        "Tool chess_start: Error: chess is not available"
    )
    assert manager.active("10") is None


def test_completed_game_can_be_restarted(runtime):
    manager, message, call = runtime

    async def run():
        msg = message()
        assert "Game started" in await call(msg, "chess_start", bot_side="black")
        for san in ("f3", "e5", "g4", "Qh4#"):
            result = await call(msg, "chess_move", move=san)
            assert "Error" not in result
        assert manager.active("10").is_over
        assert "Game started" in await call(msg, "chess_start", bot_side="black")
        assert not manager.active("10").is_over
        assert manager.active("10").history_san == []

    asyncio.run(run())


def _stale_activity(seconds: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def test_game_survives_manager_reload_after_restart(tmp_path):
    store = str(tmp_path / "games.json")
    manager = chess_game.ChessManager(store_path=store)
    game = manager.start("10", "1", "Player", bot_color=True)
    game.apply_move(chess.Move.from_uci("e2e4"))
    manager.persist()

    reloaded = chess_game.ChessManager(store_path=store)
    restored = reloaded.active("10")
    assert restored is not None
    assert restored.history_san == ["e4"]
    assert restored.turn == chess.BLACK
    assert restored.last_activity_at == game.last_activity_at


def test_idle_game_is_canceled_and_the_slot_frees(tmp_path):
    store = str(tmp_path / "games.json")
    manager = chess_game.ChessManager(store_path=store)
    game = manager.start("10", "1", "Player", bot_color=True)
    game.last_activity_at = _stale_activity(700)
    manager.persist()

    # The game survived the "restart" (it is still in the store) but is
    # past its idle window, so the sweep cancels it and frees the slot.
    reloaded = chess_game.ChessManager(store_path=store)
    expired = reloaded.sweep_expired()
    assert [channel for channel, _game in expired] == ["10"]
    assert reloaded.active("10") is None
    reloaded.start("10", "2", "Someone", bot_color=True)


def test_expired_game_reads_as_no_active_game(tmp_path):
    manager = chess_game.ChessManager(store_path=str(tmp_path / "games.json"))
    manager.start("10", "1", "Player", bot_color=True)
    manager._games["10"].last_activity_at = _stale_activity(700)
    with pytest.raises(ValueError):
        manager.game_for("10", "1")


def test_chess_activity_refreshes_the_idle_window(tmp_path):
    manager = chess_game.ChessManager(store_path=str(tmp_path / "games.json"))
    game = manager.start("10", "1", "Player", bot_color=True)
    game.last_activity_at = _stale_activity(500)
    assert manager.game_for("10", "1") is game
    assert manager.sweep_expired() == []


def test_cancel_idle_games_announces_and_frees_the_slot(runtime):
    manager, _message, _call = runtime
    manager.start("10", "1", "Galletas", bot_color=True)
    manager._games["10"].last_activity_at = _stale_activity(700)
    manager.persist()

    sent = AsyncMock(return_value=None)
    channel = SimpleNamespace(send=sent)
    bot = SimpleNamespace(
        get_channel=lambda _cid: channel,
        user=SimpleNamespace(id=999, name="Maxwell"),
    )

    asyncio.run(impl.cancel_idle_games(bot))
    sent.assert_awaited_once()
    assert "canceled" in sent.await_args.args[0]
    assert manager.active("10") is None
