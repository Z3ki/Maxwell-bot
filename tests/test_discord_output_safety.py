"""The shared Discord send path suppresses pings in model and tool text."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

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
            assert mentions.users is True
            assert mentions.roles is False
            assert mentions.replied_user is is_reply
            assert mentions.to_dict()["parse"] == ["users"]
            assert mentions.to_dict().get("replied_user", False) is is_reply
            if is_reply:
                assert call.kwargs["mention_author"] is True
            else:
                assert "mention_author" not in call.kwargs

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


@pytest.mark.parametrize("error", [discord.Forbidden, discord.NotFound])
def test_unavailable_reply_parent_falls_back_without_author_notification(error):
    async def run():
        bot = SimpleNamespace(_respect_slowmode=AsyncMock(), _mark_bot_sent=lambda _channel: None)
        sent = SimpleNamespace(id=7)
        channel = SimpleNamespace(send=AsyncMock(return_value=sent))
        response = SimpleNamespace(status=403 if error is discord.Forbidden else 404, reason="gone")
        parent = SimpleNamespace(reply=AsyncMock(side_effect=error(response, "Unknown Message")))

        assert await MaxwellBot._send_with_slowmode(
            bot, channel, "final answer", reply_to=parent
        ) is sent
        assert parent.reply.await_args.kwargs["mention_author"] is True
        kwargs = channel.send.await_args.kwargs
        assert kwargs["content"] == "final answer"
        assert kwargs["allowed_mentions"].to_dict() == {"parse": []}
        assert "mention_author" not in kwargs

    asyncio.run(run())


def test_reply_snapshot_without_reply_method_does_not_request_author_notification():
    async def run():
        bot = SimpleNamespace(_respect_slowmode=AsyncMock(), _mark_bot_sent=lambda _channel: None)
        channel = SimpleNamespace(send=AsyncMock())
        await MaxwellBot._send_with_slowmode(
            bot, channel, "final answer", reply_to=SimpleNamespace(id=1),
            mention_author=True,
        )
        kwargs = channel.send.await_args.kwargs
        assert kwargs["allowed_mentions"].replied_user is False
        assert kwargs["allowed_mentions"].everyone is False
        assert kwargs["allowed_mentions"].roles is False
        assert "mention_author" not in kwargs

    asyncio.run(run())
