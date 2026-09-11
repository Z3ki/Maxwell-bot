"""Offline regressions for tool, plugin, transport, and game audit findings."""

import asyncio
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import chess
import discord
import pytest

import autofix
from chess_game import ChessGame, ChessManager
from discord_threads import ThreadControlTool, ThreadStore
from plugin_manager import PluginContext, PluginManager
from tool_progress import ToolProgress


def run(coro):
    return asyncio.run(coro)


def manager(tmp_path):
    return PluginManager(
        SimpleNamespace(tools={}),
        plugins_dir=str(tmp_path / "plugins"),
        data_dir=str(tmp_path / "data"),
    )


@pytest.mark.parametrize("interval", [float("nan"), float("inf"), -float("inf")])
def test_plugin_interval_must_be_finite(tmp_path, interval):
    ctx = PluginContext(manager(tmp_path), "audit")

    async def callback():
        pass

    with pytest.raises(ValueError):
        ctx.every(interval, callback)


def test_plugin_setup_accepts_keyword_only_context(tmp_path):
    ctx = PluginContext(manager(tmp_path), "audit")

    def setup(bot, *, ctx):
        return [ctx]

    assert PluginManager._call_setup(SimpleNamespace(setup=setup), ctx) == [ctx]


def test_plugin_setup_accepts_keyword_arguments(tmp_path):
    ctx = PluginContext(manager(tmp_path), "audit")

    def setup(bot, **kwargs):
        return [kwargs["ctx"]]

    assert PluginManager._call_setup(SimpleNamespace(setup=setup), ctx) == [ctx]


def test_failed_plugin_does_not_publish_partial_tools(tmp_path):
    pm = manager(tmp_path)
    plugin = pm.plugins_dir / "audit_failure"
    plugin.mkdir()
    (plugin / "__init__.py").write_text(
        "from types import SimpleNamespace\n"
        "def setup(bot):\n"
        '    return [SimpleNamespace(name="first"), SimpleNamespace(name=["invalid"])]\n'
    )
    assert pm.load_plugins() == {}
    assert pm.get_tool("first") is None


def test_plugin_jobs_start_once(tmp_path):
    async def scenario():
        pm = manager(tmp_path)
        pm._register_job("audit", 5, AsyncMock(), run_immediately=False)
        try:
            assert pm.start_jobs() == 1
            assert pm.start_jobs() == 0
        finally:
            await pm.stop_jobs()

    run(scenario())


def test_disabled_plugin_receives_no_actorless_events_or_jobs(tmp_path):
    async def scenario():
        pm = manager(tmp_path)
        callback = AsyncMock()
        pm.state["plugins"]["audit"] = {
            "enabled_globally": False,
            "allowed_users": [],
            "denied_users": [],
        }
        pm._register_listener("audit", "on_ready", callback)
        assert await pm.dispatch_event("on_ready") == 0
        pm._register_job("audit", 5, callback, run_immediately=True)
        pm.start_jobs()
        await asyncio.sleep(0)
        await pm.stop_jobs()
        callback.assert_not_awaited()

    run(scenario())


def new_game():
    return ChessGame(
        game_id="g",
        channel_id="1",
        player_id="2",
        player_name="player",
        bot_color=chess.BLACK,
        started_at="now",
    )


@pytest.mark.parametrize("text", ["--", "Z0", "@@@@", "0000"])
def test_chess_rejects_null_moves(text):
    game = new_game()
    with pytest.raises(ValueError):
        game.parse_move(text)
    assert game.board == chess.Board()


def test_chess_apply_move_rejects_illegal_without_mutation():
    game = new_game()
    before = game.fen
    with pytest.raises(ValueError):
        game.apply_move(chess.Move.from_uci("e2e5"))
    assert game.fen == before
    assert game.history == []


def test_chess_roundtrip_preserves_repetition_history():
    game = new_game()
    for san in ["Nf3", "Nf6", "Ng1", "Ng8"] * 2:
        game.apply_move(game.parse_move(san))
    restored = ChessGame.from_dict(game.to_dict())
    assert restored.board.move_stack == game.board.move_stack
    assert restored.board.can_claim_threefold_repetition()
    assert restored.is_over == game.is_over


def test_chess_custom_fen_roundtrip():
    game = new_game()
    game.board.set_fen("8/8/8/8/8/8/4k3/R5K1 w - - 0 1")
    game.apply_move(game.parse_move("Ra2"))
    restored = ChessGame.from_dict(game.to_dict())
    assert restored.fen == game.fen
    assert restored.board.move_stack == game.board.move_stack


def test_chess_corrupt_top_level_store(tmp_path):
    path = tmp_path / "games.json"
    path.write_text('["not a mapping"]')
    assert ChessManager(store_path=str(path)).active("1") is None


def test_chess_relative_store_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    pm = ChessManager(store_path="games.json")
    pm.start("1", "2", "player")
    assert (tmp_path / "games.json").exists()


def thread_tool(tmp_path, *, guild_id=1):
    store = ThreadStore(str(tmp_path))
    thread = SimpleNamespace(
        id=9,
        name="private brief",
        type=SimpleNamespace(value=11),
        guild=SimpleNamespace(id=2),
        edit=AsyncMock(),
    )
    bot = SimpleNamespace(thread_store=store, get_channel=lambda _: thread)
    message = SimpleNamespace(
        channel=SimpleNamespace(id=3),
        guild=SimpleNamespace(id=guild_id) if guild_id else None,
        author=SimpleNamespace(id=4),
    )
    run(store.remember(thread, context="secret", parent_snapshot="secret history"))
    return ThreadControlTool(bot), message, thread


@pytest.mark.parametrize(
    "action,kwargs",
    [
        ("status", {}),
        ("context", {"context": "overwrite"}),
        ("rename", {"name": "overwritten"}),
        ("archive", {}),
    ],
)
def test_thread_actions_reject_other_guild(tmp_path, action, kwargs):
    tool, message, thread = thread_tool(tmp_path)
    out = run(tool.execute(message, action=action, thread_id="9", **kwargs))
    assert out.startswith("Error:")
    assert "secret" not in out
    thread.edit.assert_not_awaited()
    assert tool.bot.thread_store.get("9")["notes"] == []


def test_thread_list_in_dm_does_not_leak_all_guilds(tmp_path):
    tool, message, _ = thread_tool(tmp_path, guild_id=None)
    out = run(tool.execute(message, action="list"))
    assert "secret" not in out
    assert out.startswith("Error:")


def test_thread_status_checks_channel_visibility(tmp_path):
    tool, message, thread = thread_tool(tmp_path, guild_id=2)
    thread.permissions_for = lambda _: SimpleNamespace(view_channel=False)
    out = run(tool.execute(message, action="status", thread_id="9"))
    assert out.startswith("Error:")
    assert "secret" not in out


def test_autofix_symlink_cannot_enter_blocked_directory(tmp_path):
    (tmp_path / "data").mkdir()
    secret = tmp_path / "data" / "secret.py"
    secret.write_text("password = 'private'\n")
    (tmp_path / "alias.py").symlink_to(secret)
    assert autofix.allowed_relpath("alias.py", repo=tmp_path) is None


def test_autofix_git_failure_cleans_context_and_fingerprint(monkeypatch):
    async def scenario():
        monkeypatch.setattr(
            autofix,
            "current_branch",
            lambda _: (_ for _ in ()).throw(RuntimeError("git unavailable")),
        )
        autofix._active_fingerprints.add("audit")
        try:
            assert (
                await autofix.run_autofix(
                    SimpleNamespace(),
                    tool_name="audit",
                    tool_args={},
                    exc=TypeError("bad"),
                    tb_text="",
                    fingerprint="audit",
                )
                is None
            )
            assert not autofix._IN_AUTOFIX.get()
            assert "audit" not in autofix._active_fingerprints
        finally:
            autofix._active_fingerprints.discard("audit")

    run(scenario())


def test_progress_retains_message_for_cleanup_after_edit_failure():
    async def scenario():
        posted = SimpleNamespace(
            edit=AsyncMock(side_effect=RuntimeError("connection lost")),
            delete=AsyncMock(),
        )
        progress = ToolProgress(SimpleNamespace())
        progress._posted = posted
        await progress._flush("update")
        await progress.stop()
        await asyncio.sleep(0)
        posted.delete.assert_awaited_once()

    run(scenario())


def test_progress_final_waits_for_inflight_status_edit():
    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()
        contents = []

        async def edit(*, content):
            if content == "stale progress":
                entered.set()
                await release.wait()
            contents.append(content)

        progress = ToolProgress(SimpleNamespace())
        posted = SimpleNamespace(edit=edit)
        progress._posted = posted
        old = asyncio.create_task(progress._flush("stale progress"))
        await entered.wait()
        final = asyncio.create_task(
            progress._background_transition(posted, "final answer")
        )
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(old, final)
        assert contents[-1] == "final answer"

    run(scenario())


def test_telegram_progress_stores_and_deletes_actual_message():
    async def scenario():
        posted = SimpleNamespace(delete=AsyncMock())
        progress = ToolProgress(
            SimpleNamespace(
                tool_platform="telegram", reply=AsyncMock(return_value=posted)
            )
        )
        await progress.start()
        assert progress.posted is posted
        await progress.stop()
        await asyncio.sleep(0)
        posted.delete.assert_awaited_once()

    run(scenario())


@pytest.mark.parametrize(
    "text,expected",
    [
        (
            'send_message(content="hello ) world", reply=false)',
            {"content": "hello ) world", "reply": False},
        ),
        (
            'send_message(content="hello, reply=false", reply=True)',
            {"content": "hello, reply=false", "reply": True},
        ),
    ],
)
def test_recovered_call_preserves_quoted_values(text, expected):
    from tool_schemas import recover_text_tool_calls

    calls, remaining = recover_text_tool_calls(text, known_names={"send_message"})
    assert len(calls) == 1
    assert json.loads(calls[0]["function"]["arguments"]) == expected
    assert remaining == ""


def test_recovery_empty_allowlist_cannot_execute_any_tool():
    from tool_schemas import recover_text_tool_calls

    text = '{"name":"shell","arguments":{"command":"date"}}'
    calls, remaining = recover_text_tool_calls(text, known_names=set())
    assert calls == []
    assert remaining == text


def test_autofix_redacts_configured_credentials_from_diagnostics(monkeypatch):
    secret = "audit-only-fake-credential"
    monkeypatch.setenv("DISCORD_TOKEN", secret)
    trace = f"TypeError: login failed with {secret}"
    prompt = autofix.build_prompt(
        tool_name="audit",
        tool_args={"discord_token": secret},
        tb_text=trace,
        context=f"TOKEN = '{secret}'",
    )
    body = autofix._pr_body(
        patch={"summary": secret},
        tool_name="audit",
        tool_args={},
        tb_text=trace,
        test_output=None,
    )
    assert secret not in json.dumps(prompt)
    assert secret not in body
    assert autofix.sanitize_tool_args({"OPENAI_API_KEY": "not-env-value"}) == {
        "OPENAI_API_KEY": "[redacted]"
    }


def test_reasoning_param_preview_redacts_and_bounds_nested_values():
    from tool_registry import _summarize_params

    summary = _summarize_params(
        {"environment": {"DISCORD_TOKEN": "sensitive", "body": "x" * 10000}}
    )
    assert summary["environment"]["DISCORD_TOKEN"] == "[redacted]"
    assert len(json.dumps(summary)) < 1000
    encoded_env = _summarize_params(
        {"env": '{"API_KEY": "unknown-secret-with-spaces value"}'}
    )
    assert "unknown-secret" not in json.dumps(encoded_env)


@pytest.mark.parametrize(
    "rel,existing", [("new_module.py", False), ("tests/test_existing.py", True)]
)
def test_autofix_new_files_are_new_tests_only(tmp_path, rel, existing):
    target = tmp_path / rel
    if existing:
        target.parent.mkdir(parents=True)
        target.write_text("original")
    with pytest.raises(ValueError):
        autofix.apply_patch(
            {"new_files": [{"path": rel, "content": "replacement"}]}, root=tmp_path
        )
    assert target.read_text() == "original" if existing else not target.exists()


def test_checkers_start_cannot_replace_another_players_match(monkeypatch):
    from plugins.checkers import tools as checkers_tools

    existing = checkers_tools.CheckersGame(human_player_id="owner")
    monkeypatch.setattr(checkers_tools, "_ACTIVE_GAMES", {"1": existing})
    message = SimpleNamespace(
        channel=SimpleNamespace(id=1, send=AsyncMock()),
        author=SimpleNamespace(id=2, display_name="other"),
    )
    result = run(checkers_tools.CheckersStartTool().execute(message))
    assert result.startswith("Error:")
    assert checkers_tools.get_game("1") is existing
    message.channel.send.assert_not_awaited()


def test_checkers_concurrent_starts_do_not_overwrite_match(monkeypatch):
    from plugins.checkers import tools as checkers_tools

    monkeypatch.setattr(checkers_tools, "_ACTIVE_GAMES", {})
    monkeypatch.setattr(
        checkers_tools.CheckersGame, "render_board_png", lambda self: b"image"
    )

    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()

        async def send(*args, **kwargs):
            entered.set()
            await release.wait()

        message = SimpleNamespace(
            channel=SimpleNamespace(id=1, send=send),
            author=SimpleNamespace(id=2, display_name="player"),
        )
        task = asyncio.create_task(checkers_tools.CheckersStartTool().execute(message))
        await entered.wait()
        try:
            result = await asyncio.wait_for(
                checkers_tools.CheckersStartTool().execute(message), timeout=0.2
            )
            assert result.startswith("Error:")
        finally:
            release.set()
            await task

    run(scenario())
