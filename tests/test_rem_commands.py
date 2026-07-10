import asyncio
from typing import Any, cast

import pytest

discord = pytest.importorskip("discord")

from bot import MaxwellBot  # noqa: E402
from memory import RemEventLog  # noqa: E402
from rem import RemStore  # noqa: E402


class FakeChannel:
    def __init__(self):
        self.sent = []
        self.id = 1

    async def send(self, content):
        self.sent.append(content)


class FakeAuthor:
    id = 42
    display_name = "admin"
    bot = False


class FakeMention:
    id = 99
    display_name = "Maxwell"


class FakeReferencedAuthor:
    id = 99
    display_name = "Maxwell"


class FakeReferencedMessage:
    id = 555
    author = FakeReferencedAuthor()


class FakeReference:
    resolved = FakeReferencedMessage()


class FakeMessage:
    def __init__(self, content):
        self.id = 123
        self.content = content
        self.channel = FakeChannel()
        self.author = FakeAuthor()
        self.guild = None
        self.mentions = []
        self.reference: FakeReference | None = None


def test_rem_command_admin_gating_and_on_off_fix(tmp_path):
    bot = cast(Any, MaxwellBot.__new__(MaxwellBot))
    bot.command_prefix = ","
    bot.config = type(
        "Cfg",
        (),
        {"DATA_DIR": str(tmp_path), "REM_RUN_HISTORY": 50, "OLLAMA_REM_MODEL": "rem"},
    )()
    bot.rem_store = RemStore(str(tmp_path))
    bot.rem_enabled = False
    bot.rem_interval_seconds = 600
    bot.rem_max_turns = 3
    bot.rem_prompt_body = "custom"
    bot._admins = set()
    bot._control = {"disabled_commands": []}
    bot._rem_running = False
    bot.rem_log = type("Log", (), {"size": lambda self: 0})()

    async def run():
        msg = FakeMessage(",rem")
        await MaxwellBot._handle_command(bot, msg)
        assert msg.channel.sent == ["not authorized"]

        bot._admins = {"42"}
        msg = FakeMessage(",rem on")
        await MaxwellBot._handle_command(bot, msg)
        assert bot.rem_enabled is True
        msg = FakeMessage(",rem off")
        await MaxwellBot._handle_command(bot, msg)
        assert bot.rem_enabled is False
        msg = FakeMessage(",rem fix")
        await MaxwellBot._handle_command(bot, msg)
        assert "tears in rain" in bot.rem_prompt_body

    asyncio.run(run())


def test_record_rem_event_user_and_assistant_metadata(tmp_path):
    bot = cast(Any, MaxwellBot.__new__(MaxwellBot))
    bot.rem_log = RemEventLog(str(tmp_path), max_events=10)
    bot.bot_name = "Maxwell"
    bot._auto_channels = {"1"}
    bot._recorded_rem_msg_ids = set()
    bot._recent_users = {}
    bot._connection = type("Conn", (), {"user": type("User", (), {"id": 99})()})()
    msg = FakeMessage("hello <think>secret</think>")
    msg.mentions = [FakeMention()]
    msg.reference = FakeReference()

    async def run():
        await MaxwellBot._record_rem_event(bot, msg, "user")
        await MaxwellBot._record_rem_event(
            bot, msg, "assistant", "visible <think>hidden</think> reply"
        )
        events = await bot.rem_log.drain_slice(None)
        assert events[0]["role"] == "user"
        assert events[0]["user_id"] == "42"
        assert events[0]["channel_id"] == "1"
        assert events[0]["auto_mode"] is True
        assert events[0]["content"] == "hello"
        assert events[0]["message_id"] == "123"
        assert events[0]["mentions"] == [{"id": "99", "name": "Maxwell"}]
        assert events[0]["reply_to_message_id"] == "555"
        assert events[0]["reply_to_author_id"] == "99"
        assert events[0]["reply_to_self"] is True
        assert events[1]["role"] == "assistant"
        assert events[1]["user_id"] == "99"
        assert events[1]["user_name"] == "Maxwell"
        assert events[1]["content"] == "visible reply"

    asyncio.run(run())
