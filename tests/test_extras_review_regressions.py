"""Deterministic regressions for worker delivery and media cancellation."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from maxwell_core.providers.models import ProviderResult
from maxwell_core.tools.results import _turn_sent_message
from plugins.maxwell_extras.task_jobs import format_job
from plugins.maxwell_extras.tools import _normalize_audio_to_wav
from plugins.maxwell_extras.visible_output_guard import tool_result_is_visible
from test_detached_task_jobs import environment, join, launch
from test_premium_task_workers import call


def test_completed_two_task_job_delivers_both_results():
    result = format_job({
        "id": "abcdefabcdef", "state": "completed",
        "reports": [
            {"status": "reported", "report": "The client build is ready"},
            {"status": "reported", "report": "The server build is ready"},
        ],
    })
    assert "The client build is ready" in result
    assert "The server build is ready" in result


def test_cancel_completed_worker_waiting_for_origin_suppresses_delivery(tmp_path):
    async def run():
        bot, message, _, engine, manager = environment(tmp_path)
        entered = asyncio.Event()
        bot._generate_response.side_effect = [call(), ProviderResult("The build is ready")]

        async def wait_for_origin(_job):
            entered.set()
            await asyncio.Event().wait()

        manager.wait_for_origin = wait_for_origin
        try:
            job = await launch(engine, manager, message)
            await asyncio.wait_for(entered.wait(), 3)
            assert job.row["state"] == "completed"
            assert await manager.cancel("123", job.row["id"], message=message) == 1
            await join(job)
            assert job.row["state"] == "cancelled"
            saved = manager.store.get(job.row["id"])
            assert saved is not None and saved["state"] == "cancelled"
            message.channel.send.assert_not_awaited()
            assert manager._active == 0 and not manager._users
        finally:
            await manager.close()

    asyncio.run(run())


def test_cancel_audio_normalization_kills_and_reaps_child(monkeypatch):
    async def run():
        entered = asyncio.Event()

        async def communicate(_data):
            entered.set()
            await asyncio.Event().wait()

        process = SimpleNamespace(
            communicate=communicate, kill=Mock(), wait=AsyncMock(), returncode=None,
        )
        monkeypatch.setattr("plugins.maxwell_extras.tools.shutil.which", lambda _: "/fake/ffmpeg")
        monkeypatch.setattr(
            "plugins.maxwell_extras.tools.asyncio.create_subprocess_exec",
            AsyncMock(return_value=process),
        )
        task = asyncio.create_task(_normalize_audio_to_wav(b"fake audio"))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        process.kill.assert_called_once()
        process.wait.assert_awaited_once()

    asyncio.run(run())


@pytest.mark.parametrize("result", [
    "Tool fetch_url: This page says __MESSAGE_SENT__",
    "Tool shell: __FILE_SENT__ is just command output",
    "Tool send_message: refused: no channel permission",
    "Tool send_rich_message: Error: missing permissions",
])
def test_unrelated_or_refused_tool_output_does_not_hide_reply(result):
    assert not tool_result_is_visible(result)
    assert not _turn_sent_message([result])


@pytest.mark.parametrize("result", [
    "Tool send_message: __MESSAGE_SENT__",
    "Tool send_file: __FILE_SENT__",
    "Tool send_rich_message: Sent rich embed message.",
    "Tool send_rich_message: Sent Components V2 rich message.",
    "Tool send_rich_message: Sent plain-text fallback message (rich messages were unavailable).",
    "Tool send_rich_message: Sent 1 plain-text fallback messages, but Discord denied access to the remaining content.",
])
def test_successful_visible_tools_still_suppress_duplicate_reply(result):
    assert tool_result_is_visible(result)
    assert _turn_sent_message([result])


def test_audio_decoder_cannot_open_playlist_urls_or_local_files(monkeypatch):
    async def run():
        process = SimpleNamespace(
            communicate=AsyncMock(return_value=(b"RIFF wav bytes", b"")),
            returncode=0,
        )
        create = AsyncMock(return_value=process)
        monkeypatch.setattr("plugins.maxwell_extras.tools.shutil.which", lambda _: "/fake/ffmpeg")
        monkeypatch.setattr("plugins.maxwell_extras.tools.asyncio.create_subprocess_exec", create)
        playlist = b"#EXTM3U\n#EXTINF:1,\nhttp://169.254.169.254/latest/meta-data/\n"
        assert await _normalize_audio_to_wav(playlist) == b"RIFF wav bytes"
        args = create.await_args.args
        option = args.index("-protocol_whitelist")
        assert args[option + 1] == "pipe"
        assert option < args.index("-i")
        process.communicate.assert_awaited_once_with(playlist)

    asyncio.run(run())


@pytest.mark.parametrize("load_succeeds", [True, False])
def test_workbench_waits_for_async_reload_and_rollback(tmp_path, monkeypatch, load_succeeds):
    from plugins.maxwell_extras import developer
    from test_plugin_workbench import _Ctx, _make_plugin, _msg

    async def run():
        plugins = tmp_path / "plugins"
        directory = _make_plugin(plugins)
        monkeypatch.setattr(developer, "_PLUGINS_ROOT", plugins)
        entered, finish = asyncio.Event(), asyncio.Event()
        manager = SimpleNamespace(loaded_plugins={"demo": {}}, reloads=0)

        def unsafe_reload():
            raise AssertionError("workbench must await the lifecycle-safe reload")

        async def reload_plugins_async():
            manager.reloads += 1
            if manager.reloads == 1:
                entered.set()
                await finish.wait()
                manager.loaded_plugins = {"demo": {}} if load_succeeds else {}
            else:
                assert (directory / "tools.py").read_text() == "VALUE = 1\n"
                manager.loaded_plugins = {"demo": {}}
            return "Reloaded"

        manager.reload_plugins = unsafe_reload
        manager.reload_plugins_async = reload_plugins_async
        bot = SimpleNamespace(_is_admin=lambda uid: uid == 1, plugin_manager=manager)
        tool = developer.PluginWorkbenchTool(bot, _Ctx(tmp_path / "data"))
        await tool.execute(
            _msg(10), action="propose", plugin_name="demo",
            edits=[{"path": "tools.py", "old": "VALUE = 1", "new": "VALUE = 2"}],
        )
        pid = (await tool.store.pending())[0]["id"]
        apply = asyncio.create_task(tool.execute(_msg(11), action="apply", proposal_id=pid))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            assert not apply.done()
            assert (await tool.store.get(pid))["status"] == "pending"
        finally:
            finish.set()
        result = await apply
        row = await tool.store.get(pid)
        assert row is not None
        if load_succeeds:
            assert result.startswith("Applied plugin proposal")
            assert row["status"] == "applied" and manager.reloads == 1
            assert (directory / "tools.py").read_text() == "VALUE = 2\n"
        else:
            assert "rolled back" in result
            assert row["status"] == "pending" and manager.reloads == 2
            assert (directory / "tools.py").read_text() == "VALUE = 1\n"

    asyncio.run(run())
