"""The shared Discord send path suppresses pings in model and tool text."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot import MaxwellBot


def test_reply_notifies_author_and_suppresses_untrusted_text_mentions():
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

        for call, is_reply in ((channel.send.await_args, False), (parent.reply.await_args, True)):
            assert call.kwargs["content"] == text
            mentions = call.kwargs["allowed_mentions"]
            assert mentions.everyone is False
            assert mentions.users is False
            assert mentions.roles is False
            assert mentions.replied_user is is_reply
            assert mentions.to_dict()["parse"] == []
            assert mentions.to_dict().get("replied_user", False) is is_reply

    asyncio.run(run())


def test_reply_can_explicitly_disable_author_notification():
    async def run():
        bot = SimpleNamespace(_respect_slowmode=AsyncMock(), _mark_bot_sent=lambda _channel: None)
        channel = SimpleNamespace(send=AsyncMock())
        parent = SimpleNamespace(reply=AsyncMock())
        await MaxwellBot._send_with_slowmode(bot, channel, "quiet status", reply_to=parent,
                                             mention_author=False)
        assert parent.reply.await_args.kwargs["allowed_mentions"].replied_user is False
        assert parent.reply.await_args.kwargs["mention_author"] is False

    asyncio.run(run())
