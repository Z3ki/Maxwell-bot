"""Typing shows for the whole pinged turn, then again around the send."""

import asyncio
from types import SimpleNamespace

from bot import MaxwellBot
from bot_tools import SendMessageTool


class FakeTyping:
    def __init__(self, log):
        self.log = log

    async def __aenter__(self):
        self.log.append("enter")
        return self

    async def __aexit__(self, *args):
        self.log.append("exit")
        return False


class FakeChannel:
    def __init__(self):
        self.log = []
        self.sent = []

    def typing(self):
        return FakeTyping(self.log)

    async def send(self, text):
        self.sent.append(text)


def _bot(*, typing=True, delay=0.0):
    bot = SimpleNamespace(
        _control={"typing_indicator": typing},
        _reply_typing_delay=lambda _content: delay,
        user=SimpleNamespace(id=1382894657624866889),
    )
    bot._directly_addressed = MaxwellBot._directly_addressed.__get__(bot)
    bot._should_show_live_typing = MaxwellBot._should_show_live_typing.__get__(bot)
    bot._enter_live_typing = MaxwellBot._enter_live_typing.__get__(bot)
    bot._exit_live_typing = MaxwellBot._exit_live_typing.__get__(bot)
    bot._reply_typing = MaxwellBot._reply_typing.__get__(bot)
    return bot


def test_reply_typing_delay_scales_and_clamps():
    bot = SimpleNamespace()
    assert MaxwellBot._reply_typing_delay(bot, "") == 0.35
    assert 0.35 <= MaxwellBot._reply_typing_delay(bot, "hi") <= 1.2
    assert MaxwellBot._reply_typing_delay(bot, "x" * 5000) == 1.2


def test_ping_shows_live_typing_for_the_whole_turn():
    bot = _bot(typing=True)
    channel = FakeChannel()
    bot.user = SimpleNamespace(id=1)
    msg = SimpleNamespace(
        channel=channel,
        mentions=[bot.user],
        suppress_typing=False,
        guild=None,
        reference=None,
    )

    async def run():
        assert MaxwellBot._should_show_live_typing(bot, msg) is True
        cm = await MaxwellBot._enter_live_typing(bot, msg)
        assert channel.log == ["enter"]
        await MaxwellBot._exit_live_typing(bot, cm)
        assert channel.log == ["enter", "exit"]

    asyncio.run(run())


def test_unpinged_or_suppressed_turn_does_not_start_live_typing():
    bot = _bot(typing=True)
    channel = FakeChannel()
    quiet = SimpleNamespace(
        channel=channel,
        mentions=[],
        suppress_typing=False,
        guild=None,
        reference=None,
    )
    suppressed = SimpleNamespace(
        channel=channel,
        mentions=[bot.user],
        suppress_typing=True,
        guild=None,
        reference=None,
    )

    async def run():
        assert MaxwellBot._should_show_live_typing(bot, quiet) is False
        assert await MaxwellBot._enter_live_typing(bot, quiet) is None
        assert channel.log == []
        assert MaxwellBot._should_show_live_typing(bot, suppressed) is False
        assert await MaxwellBot._enter_live_typing(bot, suppressed) is None
        assert channel.log == []

    asyncio.run(run())


def test_no_typing_while_thinking_helper_is_send_gated():
    bot = _bot(typing=True, delay=0.0)
    channel = FakeChannel()

    async def run():
        async with bot._reply_typing(channel, "hey"):
            assert channel.log == ["enter"]
        assert channel.log == ["enter", "exit"]

        quiet = _bot(typing=False, delay=0.0)
        silent = FakeChannel()
        async with quiet._reply_typing(silent, "hey"):
            pass
        assert silent.log == []

    asyncio.run(run())


def test_send_message_tool_types_only_around_the_send():
    channel = FakeChannel()
    message = SimpleNamespace(
        channel=channel,
        guild=None,
        replies=[],
        suppress_typing=False,
    )

    async def reply(text, stickers=None):
        message.replies.append(text)

    message.reply = reply
    bot = _bot(typing=True, delay=0.0)

    async def run():
        result = await SendMessageTool(bot).execute(message, content="yo")
        assert "__MESSAGE_SENT__" in result
        assert message.replies == ["yo"]
        assert channel.log == ["enter", "exit"]

    asyncio.run(run())
