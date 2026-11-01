"""Deletion authorizes against the fetched Discord message and channel."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from plugins.discord_messages.impl import DeleteMessageTool


def _setup(author_id=999, *, requester_permission=False, bot_permission=False):
    guild = SimpleNamespace(id=500, name="test guild")
    requester = SimpleNamespace(id=7, guild=guild, display_name="Ada")
    guild.me = SimpleNamespace(id=999, guild=guild)
    guild.get_member = lambda uid: requester if uid == requester.id else None
    permissions = {7: requester_permission, 999: bot_permission}
    target = SimpleNamespace(author=SimpleNamespace(id=author_id), delete=AsyncMock())
    channel = SimpleNamespace(id=600, guild=guild, fetch_message=AsyncMock(return_value=target))
    channel.permissions_for = lambda member: SimpleNamespace(
        administrator=False, manage_messages=permissions.get(member.id, False)
    )
    message = SimpleNamespace(author=requester, guild=guild, channel=channel)
    journal = SimpleNamespace(find_response=object(), submit=AsyncMock())
    bot = SimpleNamespace(user=SimpleNamespace(id=999), _request_journal=journal)
    return DeleteMessageTool(bot), message, target, journal


@pytest.mark.parametrize("kind", ["system embed", "music embed", "unregistered bot message"])
def test_delete_own_message_does_not_require_registry_or_manage_messages(kind):
    tool, message, target, journal = _setup()
    target.embeds = [discord.Embed(title=kind)] if "embed" in kind else []
    journal.submit.side_effect = AssertionError("deletion must not query the response registry")

    result = asyncio.run(tool.execute(message, message_id="101"))

    assert result == "Deleted my message 101"
    message.channel.fetch_message.assert_awaited_once_with(101)
    target.delete.assert_awaited_once_with()
    journal.submit.assert_not_awaited()


@pytest.mark.parametrize("requester_permission,bot_permission", [(False, False), (False, True), (True, False)])
def test_delete_another_users_message_requires_both_permissions(requester_permission, bot_permission):
    tool, message, target, _ = _setup(
        author_id=8, requester_permission=requester_permission, bot_permission=bot_permission
    )

    result = asyncio.run(tool.execute(message, message_id="101"))

    assert result.startswith("Error:")
    assert "manage_messages" in result
    target.delete.assert_not_awaited()


def test_delete_another_users_message_with_both_permissions():
    tool, message, target, _ = _setup(author_id=8, requester_permission=True, bot_permission=True)

    result = asyncio.run(tool.execute(message, message_id="101"))

    assert result == "Deleted that message 101"
    target.delete.assert_awaited_once_with()


@pytest.mark.parametrize("stage", ["fetch", "delete"])
def test_delete_message_reports_not_found(stage):
    tool, message, target, _ = _setup()
    response = SimpleNamespace(status=404, reason="Not Found")
    error = discord.NotFound(response, "Unknown Message")
    if stage == "fetch":
        message.channel.fetch_message.side_effect = error
    else:
        target.delete.side_effect = error

    result = asyncio.run(tool.execute(message, message_id="101"))

    assert result == "Error: Message 101 not found"
    if stage == "fetch":
        target.delete.assert_not_awaited()


def test_delete_own_message_still_rejects_other_guild():
    tool, message, target, _ = _setup()
    message.channel.guild = SimpleNamespace(id=501)

    result = asyncio.run(tool.execute(message, message_id="101"))

    assert result == "Error: target channel is outside this server"
    message.channel.fetch_message.assert_not_awaited()
    target.delete.assert_not_awaited()
