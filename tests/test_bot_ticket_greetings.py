"""New ticket greetings obey the same room restrictions as normal replies."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from bot import MaxwellBot


def _greeting_context():
    guild = SimpleNamespace(id=10, me=SimpleNamespace(id=1))
    channel = Mock(spec=discord.TextChannel)
    channel.id = 20
    channel.name = "ticket-42"
    channel.guild = guild
    channel.send = AsyncMock()
    channel.permissions_for.return_value = SimpleNamespace(
        view_channel=True, send_messages=True
    )
    bot = SimpleNamespace(
        config=SimpleNamespace(MAXWELL_DEV_MODE=False),
        bot_name="Maxwell",
        _control={"bot_enabled": True},
        _load_control=lambda: None,
        _solo_channel_for=lambda guild: "",
        _ticket_greeting_enabled=lambda guild_id: True,
    )
    return bot, channel


@pytest.mark.parametrize("control", [
    {"bot_enabled": False},
    {"blocked_channels": ["20"]},
    {"allowed_channels": ["21"]},
    {"blocked_channels": ["20"], "allowed_channels": ["20"]},
])
def test_ticket_greeting_is_silent_outside_enabled_room_scope(control):
    bot, channel = _greeting_context()
    bot._control.update(control)

    asyncio.run(MaxwellBot.on_guild_channel_create(bot, channel))

    channel.send.assert_not_awaited()


@pytest.mark.parametrize("permission", ["view_channel", "send_messages"])
def test_ticket_greeting_requires_view_and_send_permissions(permission):
    bot, channel = _greeting_context()
    setattr(channel.permissions_for.return_value, permission, False)

    asyncio.run(MaxwellBot.on_guild_channel_create(bot, channel))

    channel.send.assert_not_awaited()


def test_ticket_greeting_does_not_start_while_draining():
    bot, channel = _greeting_context()
    bot._draining = True
    bot._load_control = Mock(side_effect=AssertionError("must not load while draining"))

    asyncio.run(MaxwellBot.on_guild_channel_create(bot, channel))

    channel.send.assert_not_awaited()


@pytest.mark.parametrize("restriction", ["dev_guild", "solo_channel", "opt_in"])
def test_ticket_greeting_keeps_existing_scope_gates(restriction):
    bot, channel = _greeting_context()
    if restriction == "dev_guild":
        bot.config.MAXWELL_DEV_MODE = True
        bot.config.MAXWELL_DEV_GUILD_IDS = frozenset({"99"})
    elif restriction == "solo_channel":
        bot._solo_channel_for = lambda guild: "21"
    else:
        bot._ticket_greeting_enabled = lambda guild_id: False

    asyncio.run(MaxwellBot.on_guild_channel_create(bot, channel))

    channel.send.assert_not_awaited()


def test_ticket_greeting_still_sends_in_explicitly_allowed_room():
    bot, channel = _greeting_context()
    bot._control["allowed_channels"] = ["20"]

    asyncio.run(MaxwellBot.on_guild_channel_create(bot, channel))

    channel.send.assert_awaited_once()
    assert "ticket" in channel.send.await_args.args[0]
    assert "Maxwell" in channel.send.await_args.args[0]
