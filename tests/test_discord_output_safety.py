"""The shared Discord send path suppresses pings in model and tool text."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot import MaxwellBot


def test_channel_send_and_reply_suppress_untrusted_mentions():
    async def run():
        bot = SimpleNamespace(
            _respect_slowmode=AsyncMock(),
            _mark_bot_sent=lambda _channel: None,
        )
        sent = SimpleNamespace(id=7)
        channel = SimpleNamespace(send=AsyncMock(return_value=sent))
        parent = SimpleNamespace(reply=AsyncMock(return_value=sent))
        text = "**A** `code` @everyone @here <@123> <@&456> 你好"

        assert await MaxwellBot._send_with_slowmode(bot, channel, text) is sent
        assert await MaxwellBot._send_with_slowmode(
            bot, channel, text, reply_to=parent
        ) is sent

        for call in (channel.send.await_args, parent.reply.await_args):
            assert call.kwargs["content"] == text
            mentions = call.kwargs["allowed_mentions"]
            assert mentions.everyone is False
            assert mentions.users is False
            assert mentions.roles is False
            assert mentions.replied_user is False

    asyncio.run(run())
