"""Channel management must honor target overwrites, not only guild roles."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from plugins.discord_guild import impl as guild_tools
from plugins.discord_moderation import impl as moderation_tools


def _context(*, denied="requester"):
    perms = discord.Permissions(manage_channels=True, manage_roles=True, view_channel=True)
    requester = SimpleNamespace(id=1, name="Requester", guild_permissions=perms)
    bot_member = SimpleNamespace(id=2, name="Bot", guild_permissions=perms)
    guild = SimpleNamespace(id=10, name="Server", me=bot_member)
    requester.guild = guild
    guild.get_member = lambda uid: requester if uid == 1 else bot_member

    def channel(cid, *, category=False, restricted=False):
        result = Mock(spec=discord.CategoryChannel if category else discord.TextChannel)
        result.id, result.name, result.guild = cid, f"channel-{cid}", guild
        result.category = SimpleNamespace(id=99)
        result.overwrites = {}
        result.permissions_synced = False
        result.permissions_for.side_effect = lambda member: (
            discord.Permissions(view_channel=True)
            if restricted and member.id == (1 if denied == "requester" else 2)
            else perms
        )
        result.edit = AsyncMock()
        result.delete = AsyncMock()
        result.clone = AsyncMock(return_value=SimpleNamespace(id=500, name="clone"))
        result.channels = []
        return result

    target = channel(20, restricted=True)
    category = channel(30, category=True, restricted=True)
    guild.get_channel = lambda cid: {20: target, 30: category}.get(cid)
    guild.categories = [category]
    guild.channels = [target]
    guild.create_text_channel = AsyncMock(return_value=target)
    bot = SimpleNamespace(get_channel=guild.get_channel, get_guild=lambda _gid: guild)
    message = SimpleNamespace(guild=guild, author=requester, channel=target)
    return bot, message, target, category, channel


@pytest.mark.parametrize("denied", ["requester", "bot"])
@pytest.mark.parametrize("tool_cls,params", [
    (guild_tools.EditChannelTool, {"name": "renamed"}),
    (guild_tools.DeleteChannelTool, {"confirm_name": "channel-20"}),
    (guild_tools.MoveChannelTool, {"category_id": "none"}),
    (guild_tools.CloneChannelTool, {}),
    (guild_tools.SyncChannelTool, {}),
    (guild_tools.ListPermissionsTool, {}),
])
def test_channel_tools_reject_target_overwrite_denial(denied, tool_cls, params):
    bot, message, target, _category, _channel = _context(denied=denied)
    result = asyncio.run(tool_cls(bot).execute(message, channel_id="20", **params))
    assert result.startswith("Error:")
    assert "manage_channels" in result or "manage_roles" in result
    target.edit.assert_not_awaited()
    target.delete.assert_not_awaited()
    target.clone.assert_not_awaited()


@pytest.mark.parametrize("tool_cls,params", [
    (guild_tools.CreateChannelTool, {"name": "new"}),
    (guild_tools.EditChannelTool, {"channel_id": "20", "name": "renamed"}),
    (guild_tools.MoveChannelTool, {"channel_id": "20"}),
    (guild_tools.CloneChannelTool, {"channel_id": "20"}),
    (guild_tools.EditCategoryTool, {"name": "renamed"}),
])
def test_channel_tools_reject_destination_category_denial(tool_cls, params):
    bot, message, target, category, _channel = _context()
    target.permissions_for.side_effect = None
    target.permissions_for.return_value = message.author.guild_permissions
    result = asyncio.run(tool_cls(bot).execute(message, category_id="30", **params))
    assert result.startswith("Error:")
    assert "manage_channels" in result
    target.edit.assert_not_awaited()
    target.clone.assert_not_awaited()
    category.edit.assert_not_awaited()
    message.guild.create_text_channel.assert_not_awaited()


@pytest.mark.parametrize("tool_cls,params", [
    (guild_tools.SyncChannelTool, {"channel_id": "30"}),
    (moderation_tools.LockChannelTool, {"channel_id": "30"}),
    (moderation_tools.LockdownTool, {"target": "server"}),
])
def test_bulk_channel_tools_skip_children_with_denied_overwrites(monkeypatch, tool_cls, params):
    bot, message, restricted, category, channel = _context()
    category.permissions_for.side_effect = None
    category.permissions_for.return_value = message.author.guild_permissions
    allowed = channel(21)
    category.channels = [restricted, allowed]
    message.guild.channels = [restricted, allowed]
    lock = AsyncMock(return_value="")
    monkeypatch.setattr(moderation_tools, "_lock_target", lock)

    result = asyncio.run(tool_cls(bot).execute(message, **params))
    assert "manage_channels" in result or "manage_roles" in result
    restricted.edit.assert_not_awaited()
    if tool_cls is guild_tools.SyncChannelTool:
        allowed.edit.assert_awaited_once()
    else:
        lock.assert_awaited_once()
        assert lock.await_args.args[0] is allowed
