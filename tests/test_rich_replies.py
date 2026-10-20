"""Embeds and polls are replies: they send, get recorded, and stay in memory."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import discord

from plugins.discord_messages.impl import CreatePollTool
from plugins.maxwell_extras.tools import SendRichMessageTool


def _channel(calls):
    member = SimpleNamespace(id=123)
    guild = SimpleNamespace(me=member)
    permissions = SimpleNamespace(
        view_channel=True,
        send_messages=True,
        send_messages_in_threads=True,
        embed_links=True,
    )

    async def send(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(id=900 + len(calls), content="")

    channel = SimpleNamespace(
        id=55,
        type=discord.ChannelType.text,
        guild=guild,
        permissions_for=lambda who: permissions,
        send=send,
    )
    return SimpleNamespace(id=7, channel=channel, guild=guild)


def _recording_bot():
    log = {"delivered": [], "remembered": []}

    async def record(message, sent):
        log["delivered"].append(sent.id)

    async def remember(message, sent, content, channel):
        log["remembered"].append((sent.id, content))

    bot = SimpleNamespace(_record_delivery=record, _remember_sent_message=remember)
    return bot, log


def test_auto_mode_sends_an_embed_with_list_fields_image_and_buttons():
    calls = []
    message = _channel(calls)
    bot, log = _recording_bot()
    result = asyncio.run(
        SendRichMessageTool(bot).execute(
            message,
            title="GPU picks",
            description="Under $500",
            fields=[
                {"name": "RX 7800 XT", "value": "16 GB", "inline": True},
                {"name": "RTX 4070", "value": "12 GB", "inline": "true"},
                {"name": "Verdict", "value": "AMD for VRAM"},
            ],
            footer="Prices move weekly",
            image_url="https://example.org/chart.png",
            buttons=[{"label": "Compare", "url": "https://example.org/compare"}],
        )
    )
    assert result == "Sent rich embed message."
    assert len(calls) == 1
    embed = calls[0]["embed"]
    assert [(f.name, f.value, f.inline) for f in embed.fields] == [
        ("RX 7800 XT", "16 GB", True),
        ("RTX 4070", "12 GB", True),
        ("Verdict", "AMD for VRAM", False),
    ]
    assert embed.footer.text == "Prices move weekly"
    assert embed.image.url == "https://example.org/chart.png"
    assert calls[0]["view"] is not None

    assert log["delivered"] == [901]
    (sent_id, text), = log["remembered"]
    assert sent_id == 901
    assert text.startswith("[rich message]\n**GPU picks**\nUnder $500")
    assert "RTX 4070: 12 GB" in text
    assert "image: https://example.org/chart.png" in text
    assert "button Compare: https://example.org/compare" in text


def test_json_string_fields_still_work():
    calls = []
    message = _channel(calls)
    result = asyncio.run(
        SendRichMessageTool(SimpleNamespace()).execute(
            message,
            title="Status",
            fields=json.dumps([{"name": "API", "value": "up"}]),
        )
    )
    assert result == "Sent rich embed message."
    assert [(f.name, f.value) for f in calls[0]["embed"].fields] == [("API", "up")]


def test_bookkeeping_failure_does_not_turn_a_sent_card_into_an_error():
    calls = []
    message = _channel(calls)

    async def boom(*_args, **_kwargs):
        raise RuntimeError("memory is down")

    bot = SimpleNamespace(_record_delivery=boom, _remember_sent_message=boom)
    result = asyncio.run(
        SendRichMessageTool(bot).execute(message, description="still delivered")
    )
    assert result == "Sent rich embed message."
    assert len(calls) == 1


def test_poll_accepts_a_list_so_answers_can_contain_commas():
    calls = []
    message = _channel(calls)
    bot, log = _recording_bot()
    tool = CreatePollTool(bot)
    tool._signal_streaming = lambda *_a, **_k: None
    result = asyncio.run(
        tool.execute(
            message,
            question="Dinner?",
            options=["Tacos, obviously", "Pizza", "Sushi"],
            duration_hours="12",
            multiple=True,
        )
    )
    assert result.startswith("Poll created: 'Dinner?'")
    poll = calls[0]["poll"]
    assert [a.text for a in poll.answers] == ["Tacos, obviously", "Pizza", "Sushi"]
    assert poll.multiple is True
    assert poll.duration.total_seconds() == 12 * 3600
    assert log["delivered"] == [901]
    assert log["remembered"] == [(901, "[poll] Dinner? — Tacos, obviously / Pizza / Sushi")]


def test_poll_still_accepts_the_old_comma_string():
    calls = []
    message = _channel(calls)
    tool = CreatePollTool(SimpleNamespace())
    tool._signal_streaming = lambda *_a, **_k: None
    result = asyncio.run(tool.execute(message, question="Ready?", options="Yes, No"))
    assert result.startswith("Poll created")
    assert [a.text for a in calls[0]["poll"].answers] == ["Yes", "No"]
    assert calls[0]["poll"].multiple is False


def test_poll_rejects_answers_discord_would_reject_without_sending():
    calls = []
    message = _channel(calls)
    tool = CreatePollTool(SimpleNamespace())
    tool._signal_streaming = lambda *_a, **_k: None
    long_answer = "x" * 56
    assert "55 characters" in asyncio.run(
        tool.execute(message, question="Q", options=[long_answer, "ok"])
    )
    assert "at least 2" in asyncio.run(tool.execute(message, question="Q", options=["one"]))
    assert "between 1 and 168" in asyncio.run(
        tool.execute(message, question="Q", options=["a", "b"], duration_hours=500)
    )
    assert calls == []


def _memory_bot(rows):
    async def add(channel_id, row, request):
        rows.append((channel_id, row))

    return SimpleNamespace(
        _control={},
        memory=object(),
        bot_name="Maxwell",
        user=SimpleNamespace(id=1, display_name="Maxwell"),
        add_message_to_memory=add,
    )


def test_blank_discord_content_keeps_the_callers_rendering():
    from bot import MaxwellBot

    rows = []
    bot = _memory_bot(rows)
    request = SimpleNamespace(id=7, channel=SimpleNamespace(id=55))
    sent = SimpleNamespace(id=901, content="", reference=None)
    asyncio.run(
        MaxwellBot._remember_sent_message(bot, request, sent, "[poll] Q — a / b", request.channel)
    )
    assert rows[0][1]["content"] == "[poll] Q — a / b"

    rows.clear()
    sent = SimpleNamespace(id=902, content="real text", reference=None)
    asyncio.run(MaxwellBot._remember_sent_message(bot, request, sent, "ignored", request.channel))
    assert rows[0][1]["content"] == "real text"


def test_delivered_cards_and_polls_skip_the_duplicate_tool_row():
    from bot import MaxwellBot

    rows = []
    bot = _memory_bot(rows)
    message = SimpleNamespace(id=7, channel=SimpleNamespace(id=55), author=None)
    for name, result in (
        ("send_rich_message", "Tool send_rich_message: Sent rich embed message."),
        ("create_poll", "Tool create_poll: Poll created: 'Q' with options: a, b"),
    ):
        asyncio.run(MaxwellBot._remember_tool_call(bot, message, name, {"title": "x"}, result))
    assert rows == []

    asyncio.run(
        MaxwellBot._remember_tool_call(
            bot, message, "send_rich_message", {}, "Tool send_rich_message: Error: title or description is required."
        )
    )
    assert len(rows) == 1 and rows[0][1]["is_tool"] is True


def test_gateway_copy_of_own_card_keeps_the_fields():
    """The gateway row replaces the tool's rendering, so it must keep the card."""
    from bot import MaxwellBot

    bot = SimpleNamespace(user=SimpleNamespace(id=1), _recent_users={})
    card = discord.Embed(title="Results", description="Round 2")
    card.add_field(name="Alice", value="12")
    card.set_footer(text="Final")
    message = SimpleNamespace(
        id=901,
        content="",
        author=SimpleNamespace(id=1),
        embeds=[card],
        attachments=[],
        stickers=[],
        channel=SimpleNamespace(id=55),
    )
    text = MaxwellBot._message_memory_content(bot, message)
    assert "Round 2" in text and "Alice: 12" in text and "Final" in text
