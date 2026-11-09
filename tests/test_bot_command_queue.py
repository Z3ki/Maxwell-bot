"""Dashboard command outcomes survive the bot's durable queue merge."""

import asyncio
import json
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest

from bot_parts import messages


@pytest.mark.parametrize("failure", ["missing_user", "send_error", None])
def test_command_queue_preserves_terminal_outcome_without_repeating(tmp_path, monkeypatch, failure):
    command = {
        "id": "command-1",
        "status": "pending",
        "type": "send_dm" if failure == "missing_user" else "send_message",
        "user_id": "123",
        "channel_id": "456",
        "content": "hello",
    }
    path = tmp_path / "bot_commands.json"
    path.write_text(json.dumps([command]), encoding="utf-8")
    send = AsyncMock(side_effect=RuntimeError("send failed") if failure == "send_error" else None)
    bot = SimpleNamespace(
        config=SimpleNamespace(DATA_DIR=tmp_path),
        _draining=False,
        get_channel=lambda _cid: SimpleNamespace(send=send),
        get_user=lambda _uid: None,
        fetch_user=AsyncMock(return_value=None),
    )
    ticks = 0

    async def tick(_seconds):
        nonlocal ticks
        ticks += 1
        # Process twice: terminal commands must not be re-executed on tick two.
        if ticks == 3:
            bot._draining = True

    monkeypatch.setattr(messages, "asyncio", SimpleNamespace(sleep=tick, to_thread=asyncio.to_thread))
    asyncio.run(messages.MessagesMixin._command_queue_loop(cast(messages.MessagesMixin, bot)))
    saved = json.loads(path.read_text(encoding="utf-8"))[0]
    assert saved["status"] == ("failed" if failure else "done")
    if failure == "missing_user":
        assert "not found" in saved["result"]
        bot.fetch_user.assert_awaited_once_with(123)
        send.assert_not_awaited()
    elif failure == "send_error":
        assert saved["result"] == "error: send failed"
        send.assert_awaited_once_with("hello")
    else:
        assert saved["result"] == "sent"
        send.assert_awaited_once_with("hello")
