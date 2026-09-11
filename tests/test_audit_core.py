import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import bot as bot_module
import sys
from io import BytesIO

from process_utils import communicate_process
from bot import MaxwellBot, TelegramMessageAdapter
from bot_tools import SendMessageTool


def _message():
    guild = SimpleNamespace(id=1)
    return SimpleNamespace(
        id=10,
        guild=guild,
        author=SimpleNamespace(id=11),
        channel=SimpleNamespace(id=99, guild=guild, send=AsyncMock()),
        reply=AsyncMock(),
    )


def _routing_bot(**kwargs):
    return SimpleNamespace(
        _is_admin=lambda _: True,
        get_channel=lambda _: None,
        fetch_channel=AsyncMock(return_value=None),
        get_user=lambda _: None,
        fetch_user=AsyncMock(return_value=None),
        **kwargs,
    )


@pytest.mark.parametrize("destination", ["invalid_id", "0", "123456789012345678"])
def test_explicit_destination_never_falls_back_to_origin(destination):
    message = _message()
    result = asyncio.run(
        SendMessageTool(_routing_bot()).execute(
            message, content="private destination content", channel_id=destination
        )
    )
    assert result.startswith("Error:")
    message.reply.assert_not_awaited()
    message.channel.send.assert_not_awaited()


def test_destination_mention_uses_parsed_snowflake():
    message = _message()
    target = SimpleNamespace(id=123456789012345678, send=AsyncMock())
    owner = _routing_bot()
    owner.get_channel = lambda cid: target if cid == target.id else None
    result = asyncio.run(
        SendMessageTool(owner).execute(
            message, content="target content", channel_id=f"<#{target.id}>"
        )
    )
    assert result.startswith("__MESSAGE_SENT__")
    target.send.assert_awaited_once_with("target content")
    message.reply.assert_not_awaited()
    message.channel.send.assert_not_awaited()


class _TelegramResponse:
    status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def json(self):
        return {"ok": True, "result": {"message_id": 42, "chat": {"id": 99}}}


def test_telegram_send_tool_reports_confirmed_delivery():
    async def run():
        session = SimpleNamespace(post=lambda *a, **kw: _TelegramResponse())
        message = TelegramMessageAdapter(session, "https://example.test/bot", 99, 1, 11)
        owner = SimpleNamespace(
            _respect_slowmode=AsyncMock(),
            _mark_bot_sent=lambda _: None,
        )
        owner._send_with_slowmode = lambda *a, **kw: MaxwellBot._send_with_slowmode(
            owner, *a, **kw
        )
        result = await SendMessageTool(owner).execute(message, content="hello")
        assert result.startswith("__MESSAGE_SENT__")
        sent = await message.reply("hello")
        assert sent.id == 42
        assert sent.channel.id == "tg:99"

    asyncio.run(run())


class _RunningProcess:
    def __init__(self):
        self.returncode = None
        self.started = asyncio.Event()
        self.killed = False
        self.reaped = False

    async def communicate(self):
        if self.killed:
            return b"", b""
        self.started.set()
        await asyncio.Event().wait()

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        self.reaped = True
        return self.returncode


@pytest.mark.parametrize("kind", ["gif", "video", "frames", "local_tts"])
def test_media_subprocess_is_reaped_on_cancellation(monkeypatch, tmp_path, kind):
    async def run():
        proc = _RunningProcess()
        monkeypatch.setattr(
            asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)
        )
        owner = SimpleNamespace(
            _ffmpeg_input_argv=MaxwellBot._ffmpeg_input_argv,
        )
        if kind == "gif":
            operation = MaxwellBot._normalize_gif(owner, b"gif", "test.gif", 1024)
        elif kind == "video":
            operation = MaxwellBot._normalize_video(owner, b"video", "test.mp4", 1024)
        elif kind == "frames":
            operation = MaxwellBot._extract_video_derivatives(
                owner, b"video", "test.mp4", 1, 1024, include_frames=True
            )
        else:
            monkeypatch.setattr(bot_module.shutil, "which", lambda _: "espeak")
            operation = bot_module._synthesize_local_tts_wav(
                "hello", str(tmp_path / "out.wav")
            )
        task = asyncio.create_task(operation)
        await asyncio.wait_for(proc.started.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert proc.killed
        assert proc.reaped

    asyncio.run(run())


def test_process_timeout_reaps_real_child():
    async def run():
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        with pytest.raises(TimeoutError):
            await communicate_process(proc, timeout=0.05)
        assert proc.returncode is not None

    asyncio.run(run())


def test_process_success_preserves_output():
    async def run():
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "print('done')",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert await communicate_process(proc, timeout=10) == (b"done\n", b"")
        assert proc.returncode == 0

    asyncio.run(run())


def test_telegram_file_reply_returns_delivery_receipt():
    async def run():
        session = SimpleNamespace(post=lambda *a, **kw: _TelegramResponse())
        message = TelegramMessageAdapter(session, "https://example.test/bot", 99, 1)
        sent = await message.reply(
            file=SimpleNamespace(fp=BytesIO(b"file"), filename="file.txt")
        )
        assert sent.id == 42

    asyncio.run(run())


def test_telegram_does_not_confirm_malformed_success():
    async def run():
        response = _TelegramResponse()
        response.json = AsyncMock(return_value={"ok": False, "description": "failed"})
        session = SimpleNamespace(post=lambda *a, **kw: response)
        message = TelegramMessageAdapter(session, "https://example.test/bot", 99, 1)
        with pytest.raises(RuntimeError, match="did not confirm"):
            await message.reply("hello")

    asyncio.run(run())


def test_plugin_fallback_retains_and_releases_task():
    async def run():
        release = asyncio.Event()
        owner = SimpleNamespace(
            plugin_manager=SimpleNamespace(
                _listeners={"ready": [object()]},
                dispatch_event=AsyncMock(side_effect=lambda *_: None),
            )
        )

        async def dispatch(*_):
            await release.wait()

        owner.plugin_manager.dispatch_event = dispatch
        MaxwellBot._dispatch_plugin_event(owner, "ready")
        assert len(owner._detached_tasks) == 1
        task = next(iter(owner._detached_tasks))
        release.set()
        await task
        await asyncio.sleep(0)
        assert not owner._detached_tasks

    asyncio.run(run())


def test_parallel_tool_progress_lasts_for_whole_batch(monkeypatch):
    async def run():
        first_finished = asyncio.Event()
        second_started = asyncio.Event()
        progress = SimpleNamespace(update=AsyncMock(), stop=AsyncMock())
        previous = object()
        owner = SimpleNamespace(
            _control={},
            tools={},
            _current_progress_by_channel={"99": previous, "other": object()},
        )
        observed = []

        async def execute(self, message, name, *_args, **_kwargs):
            if name == "first":
                await second_started.wait()
            else:
                second_started.set()
                await first_finished.wait()
                observed.append(self._current_progress_by_channel.get("99"))
            return f"Tool {name}: done"

        async def remember(self, message, name, *_args):
            if name == "first":
                first_finished.set()

        monkeypatch.setattr(MaxwellBot, "_execute_tool_by_name", execute)
        monkeypatch.setattr(MaxwellBot, "_remember_tool_call", remember)
        calls = [
            {
                "id": name,
                "type": "function",
                "function": {"name": name, "arguments": "{}"},
            }
            for name in ("first", "second")
        ]
        await MaxwellBot._process_native_tool_calls(
            owner, _message(), "", calls, existing_progress=progress
        )
        assert observed == [progress]
        assert owner._current_progress_by_channel["99"] is previous
        assert "other" in owner._current_progress_by_channel
        progress.stop.assert_awaited_once()

    asyncio.run(run())


def test_parallel_tool_error_survives_history_reconstruction(monkeypatch):
    async def run():
        owner = SimpleNamespace(_control={}, tools={})
        message = _message()
        message.guild = None
        monkeypatch.setattr(
            MaxwellBot,
            "_execute_tool_by_name",
            AsyncMock(side_effect=RuntimeError("broken helper")),
        )
        monkeypatch.setattr(MaxwellBot, "_remember_tool_call", AsyncMock())
        calls = [
            {
                "id": "failed",
                "type": "function",
                "function": {"name": "helper", "arguments": "{}"},
            }
        ]
        _, results = await MaxwellBot._process_native_tool_calls(
            owner, message, "", calls
        )
        assert results == ["Tool helper: Error - RuntimeError: broken helper"]
        assert owner._last_native_followup_messages[-1]["content"] == results[0]

    asyncio.run(run())


def test_telegram_progress_deletes_its_confirmed_message():
    from tool_progress import ToolProgress

    async def run():
        deleted = asyncio.Event()
        calls = []

        def post(url, **kwargs):
            calls.append((url, kwargs))
            if url.endswith("/deleteMessage"):
                deleted.set()
            return _TelegramResponse()

        message = TelegramMessageAdapter(
            SimpleNamespace(post=post), "https://example.test/bot", 99, 1
        )
        progress = ToolProgress(message)
        await progress.start()
        await progress.stop()
        await asyncio.wait_for(deleted.wait(), timeout=2)
        assert calls[-1][1]["json"] == {"chat_id": 99, "message_id": 42}

    asyncio.run(run())


def test_plugin_reload_awaits_teardown_before_replacement():
    async def run():
        order = []

        async def teardown():
            await asyncio.sleep(0)
            order.append("teardown")

        def reload():
            order.append("reload")
            return "reloaded"

        owner = SimpleNamespace(
            command_prefix=",",
            _control={},
            _is_admin=lambda _: True,
            plugin_manager=SimpleNamespace(teardown=teardown, reload_plugins=reload),
        )
        message = _message()
        message.content = ",plugin reload"
        await MaxwellBot._handle_command(owner, message)
        assert order == ["teardown", "reload"]
        message.channel.send.assert_awaited_once_with("reloaded")

    asyncio.run(run())
