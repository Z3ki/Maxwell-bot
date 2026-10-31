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


def test_send_suppresses_context_and_does_not_send_empty_text():
    async def run():
        bot = SimpleNamespace(_respect_slowmode=AsyncMock(), _mark_bot_sent=lambda _: None)
        channel = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=7)))
        parent = SimpleNamespace(reply=AsyncMock(return_value=SimpleNamespace(id=8)))
        await MaxwellBot._send_with_slowmode(
            bot, channel, "Hello.\n[PRIVATE INTERNAL CONTEXT]secret[/PRIVATE INTERNAL CONTEXT]",
            reply_to=parent,
        )
        assert parent.reply.await_args.kwargs["content"] == "Hello."
        assert await MaxwellBot._send_with_slowmode(
            bot, channel, "[PRIVATE INTERNAL CONTEXT]secret[/PRIVATE INTERNAL CONTEXT]"
        ) is None
        channel.send.assert_not_awaited()

    asyncio.run(run())


def test_tool_strips_context_before_chunking():
    from bot_tools import SendMessageTool

    async def run():
        channel = SimpleNamespace(id=1, guild=None, send=AsyncMock())
        message = SimpleNamespace(channel=channel, guild=None, author=SimpleNamespace(id=2))
        text = "a" * 1890 + "\n[PRIVATE INTERNAL CONTEXT]" + "secret " * 100 + "[/PRIVATE INTERNAL CONTEXT]\nDone."
        result = await SendMessageTool(SimpleNamespace()).execute(message, content=text, reply=False)
        sent = "".join(call.args[0] for call in channel.send.await_args_list)
        assert "secret" not in sent
        assert "PRIVATE INTERNAL" not in sent
        assert "Done." in sent
        assert "__MESSAGE_SENT__" in result
        channel.send.reset_mock()
        result = await SendMessageTool(SimpleNamespace()).execute(
            message, content="[PRIVATE INTERNAL CONTEXT]secret[/PRIVATE INTERNAL CONTEXT]", reply=False
        )
        assert result.startswith("Error:")
        channel.send.assert_not_awaited()

    asyncio.run(run())


def test_worker_report_filters_before_truncation():
    from plugins.maxwell_extras.task_jobs import format_job

    row = {"state": "completed", "direct": True, "reports": [
        {"report": "a" * 1840 + "[PRIVATE INTERNAL CONTEXT]" + "secret " * 100 + "[/PRIVATE INTERNAL CONTEXT]"}
    ]}
    text = format_job(row, limit=1850)
    assert "secret" not in text
    assert "PRIVATE INTERNAL" not in text


def test_normal_reply_filters_full_text_before_splitting_and_transition(monkeypatch):
    from contextlib import asynccontextmanager
    import bot_parts.messages as messages

    async def run():
        seen = []
        @asynccontextmanager
        async def typing(*_args, **_kwargs):
            yield

        bot = SimpleNamespace(
            _render_custom_emojis=lambda text, _guild: text,
            _extract_stickers_from_text=lambda text, _guild: (text, []),
            _split_response=lambda text, limit: [text[:1900], text[1900:]] if len(text) > 1900 else [text],
            _reply_typing=typing,
            _send_with_slowmode=AsyncMock(side_effect=lambda _ch, content=None, **kw: seen.append(content) or object()),
        )
        monkeypatch.setattr(messages.MaxwellBot, "_request_state", AsyncMock(return_value=True))
        message = SimpleNamespace(channel=SimpleNamespace(), guild=None)
        response, sent = await MaxwellBot._deliver_normal_reply(
            bot, message,
            "a" * 1890 + "\n[PRIVATE INTERNAL CONTEXT]" + "secret " * 100 + "[/PRIVATE INTERNAL CONTEXT]\nDone.",
            [],
        )
        assert sent and "Done." in response
        assert "secret" not in "".join(seen)
        assert "PRIVATE INTERNAL" not in "".join(seen)
        bot._send_with_slowmode.reset_mock()
        assert await MaxwellBot._deliver_normal_reply(
            bot, message, "[PRIVATE INTERNAL CONTEXT]secret[/PRIVATE INTERNAL CONTEXT]", []
        ) == ("", False)
        bot._send_with_slowmode.assert_not_awaited()

    asyncio.run(run())


def test_edit_filters_context_and_refuses_empty_content():
    from plugins.discord_messages.impl import EditMessageTool

    async def run():
        edited = SimpleNamespace(author=SimpleNamespace(id=7), edit=AsyncMock())
        channel = SimpleNamespace(id=1, guild=None, fetch_message=AsyncMock(return_value=edited))
        message = SimpleNamespace(channel=channel, author=SimpleNamespace(id=2))
        journal = SimpleNamespace(
            find_response=object(),
            submit=AsyncMock(return_value={"author_id": "2", "channel_id": "1"}),
        )
        tool = EditMessageTool(SimpleNamespace(user=SimpleNamespace(id=7), _request_journal=journal))
        await tool.execute(message, message_id="5", content="Hello.\n[PRIVATE INTERNAL CONTEXT]secret[/PRIVATE INTERNAL CONTEXT]")
        assert edited.edit.await_args.kwargs["content"] == "Hello."
        edited.edit.reset_mock()
        result = await tool.execute(message, message_id="5", content="[PRIVATE INTERNAL CONTEXT]secret[/PRIVATE INTERNAL CONTEXT]")
        assert result.startswith("Error:")
        edited.edit.assert_not_awaited()

    asyncio.run(run())


def test_rich_message_filters_embed_fields_before_delivery():
    from plugins.maxwell_extras.tools import SendRichMessageTool

    async def run():
        channel = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=1)), guild=None)
        message = SimpleNamespace(channel=channel, guild=None)
        tool = SendRichMessageTool(SimpleNamespace())
        result = await tool.execute(
            message,
            title="Hello.",
            description="Body.\n[PRIVATE INTERNAL CONTEXT]secret[/PRIVATE INTERNAL CONTEXT]",
            fields=[{"name": "Field", "value": "Visible.\n[PRIVATE INTERNAL CONTEXT]secret[/PRIVATE INTERNAL CONTEXT]"}],
            footer="[PRIVATE INTERNAL CONTEXT]secret[/PRIVATE INTERNAL CONTEXT]",
        )
        assert result.startswith("Sent")
        embed = channel.send.await_args.kwargs["embed"]
        assert embed.description == "Body."
        assert embed.fields[0].value == "Visible."
        assert not embed.footer.text
        channel.send.reset_mock()
        result = await tool.execute(message, title="[PRIVATE INTERNAL CONTEXT]secret[/PRIVATE INTERNAL CONTEXT]")
        assert result.startswith("Error:")
        channel.send.assert_not_awaited()

    asyncio.run(run())
