"""Chess tools through Maxwell's real request dispatch, with Discord I/O faked."""

import asyncio
import json
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
        _control={},
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

    async def call(msg, name, *, native=False, **params):
        if native:
            calls = [{
                "id": "chess-call",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(params)},
            }]
            _, results, images = await MaxwellBot._process_native_tool_calls(
                bot, msg, "", calls, include_images=True
            )
            assert images == []
            assert bot._last_native_tool_media == []
            assert len(results) == 1
            assert bot._last_native_followup_messages[-1]["content"] == results[0]
            assert "__IMAGE_B64__" not in results[0]
            assert "Board image URL:" not in results[0]
            assert "Board image local path:" not in results[0]
            return results[0]
        return await MaxwellBot._execute_tool_by_name(
            bot, msg, name, params, disabled=set(), compatible=set(bot.tools)
        )

    return manager, message, call


@pytest.mark.parametrize("bot_side", ["white", "black"])
def test_native_followups_are_text_only_and_discord_keeps_the_board(
    runtime, monkeypatch, bot_side
):
    manager, message, call = runtime
    render = impl._chess_render_safe
    rendered = []

    def track_render(game):
        rendered.append(game.fen)
        return render(game)

    monkeypatch.setattr("tooling.helpers._chess_render_safe", track_render)

    async def run():
        msg = message()
        result = await call(msg, "chess_start", bot_side=bot_side, native=True)
        assert "Game started" in result
        assert f"FEN: {manager.active('10').fen}" in result
        for san in ("e4", "e5", "Nf3", "Nc6"):
            result = await call(msg, "chess_move", move=san, native=True)
            assert f"Played move(s): {san}" in result
            assert f"FEN: {manager.active('10').fen}" in result
        assert len(rendered) == 5  # one render per Discord board, not two
        assert msg.channel.send.await_count == 5
        for sent in msg.channel.send.await_args_list:
            file = sent.kwargs["file"]
            assert file.filename.startswith("chess-")
            assert file.fp.getvalue().startswith(b"\x89PNG\r\n\x1a\n")

        def no_render(_game):
            pytest.fail("state/resign/error results must not render a model image")

        monkeypatch.setattr("tooling.helpers._chess_render_safe", no_render)
        monkeypatch.setattr(impl, "_chess_render_safe", no_render)
        state = await call(msg, "chess_state", native=True)
        assert f"FEN: {manager.active('10').fen}" in state
        assert "Last move (SAN): Nc6" in state
        assert "Move history (SAN):" not in state
        assert "Error" in await call(msg, "chess_move", move="e9", native=True)
        ended = await call(msg, "chess_resign", native=True)
        assert "GAME OVER" in ended
        assert "FEN:" in ended
        assert msg.channel.send.await_count == 5

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["render", "forbidden"])
def test_failed_discord_board_still_returns_text_position(runtime, monkeypatch, failure):
    import discord

    manager, message, call = runtime
    msg = message()
    if failure == "render":
        monkeypatch.setattr("tooling.helpers._chess_render_safe", lambda _game: None)
    else:
        response = SimpleNamespace(status=403, reason="Forbidden")
        msg.channel.send.side_effect = discord.Forbidden(response, "Missing Permissions")
    result = asyncio.run(call(msg, "chess_start", bot_side="white", native=True))
    assert f"FEN: {manager.active('10').fen}" in result
    assert "YOUR LEGAL MOVES (20)" in result
    assert "Board image posted" not in result
    assert "Game started" in result


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


@pytest.mark.parametrize("ending", ["checkmate", "stalemate"])
def test_finished_games_expire_silently_while_idle_live_games_are_canceled(runtime, ending):
    manager, message, call = runtime
    finished = manager.start("10", "1", "Player", bot_color=chess.BLACK)
    if ending == "checkmate":
        for san in ("f3", "e5", "g4", "Qh4#"):
            finished.apply_move(finished.parse_move(san))
    else:
        finished.board = chess.Board("7k/5K2/6Q1/8/8/8/8/8 b - - 0 1")
    assert finished.is_over
    live = manager.start("11", "2", "Other", bot_color=chess.BLACK)
    finished.last_activity_at = _stale_activity(601)
    live.last_activity_at = _stale_activity(601)
    manager.persist()
    channels = {10: SimpleNamespace(send=AsyncMock()), 11: SimpleNamespace(send=AsyncMock())}
    bot = SimpleNamespace(get_channel=channels.get, user=SimpleNamespace(id=999, name="Maxwell"))

    asyncio.run(impl.cancel_idle_games(bot))

    channels[10].send.assert_not_awaited()
    channels[11].send.assert_awaited_once()
    assert "canceled" in channels[11].send.await_args.args[0]
    assert manager.active("10") is None
    assert manager.active("11") is None
    assert chess_game.ChessManager(store_path=manager._store)._games == {}
    asyncio.run(impl.cancel_idle_games(bot))
    assert channels[11].send.await_count == 1


@pytest.mark.parametrize("expiry_path", ["sweep", "lazy"])
def test_finished_game_cleanup_is_persisted_without_an_idle_live_game(tmp_path, expiry_path):
    store = tmp_path / "games.json"
    manager = chess_game.ChessManager(store_path=str(store))
    game = manager.start("10", "1", "Player", bot_color=chess.BLACK)
    for san in ("f3", "e5", "g4", "Qh4#"):
        game.apply_move(game.parse_move(san))
    game.last_activity_at = _stale_activity(601)
    manager.persist()
    assert "10" in json.loads(store.read_text())

    if expiry_path == "sweep":
        assert manager.sweep_expired() == []
    else:
        assert manager.active("10") is None
    assert manager._games == {}
    assert json.loads(store.read_text()) == {}
