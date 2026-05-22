import asyncio
import pytest

discord = pytest.importorskip("discord")

from bot import MaxwellBot
from rem import RemStore


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


class FakeMessage:
    def __init__(self, content):
        self.content = content
        self.channel = FakeChannel()
        self.author = FakeAuthor()
        self.guild = None


def test_rem_command_admin_gating_and_on_off_fix(tmp_path):
    bot = MaxwellBot.__new__(MaxwellBot)
    bot.command_prefix = ","
    bot.config = type("Cfg", (), {"DATA_DIR": str(tmp_path), "REM_RUN_HISTORY": 50, "OLLAMA_REM_MODEL": "rem"})()
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
