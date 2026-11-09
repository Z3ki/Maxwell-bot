"""Offline regressions for Discord target permissions and mutation isolation."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from plugins.discord_guild import impl as guild_tools
from plugins.discord_messages import impl as message_tools
from plugins.discord_moderation import impl as mod_tools
from plugins.discord_presence import impl as presence_tools


def _permissions(**overrides):
    flags = dict(
        view_channel=True,
        read_message_history=True,
        send_messages=True,
        send_messages_in_threads=True,
        manage_channels=True,
        manage_roles=True,
        mute_members=True,
        deafen_members=True,
        move_members=True,
    )
    flags.update(overrides)
    return discord.Permissions(**flags)


def _context():
    guild = SimpleNamespace(id=10, name="Test server", owner_id=9999)
    actor = SimpleNamespace(
        id=20, guild=guild, display_name="Requester",
        guild_permissions=_permissions(), top_role=SimpleNamespace(position=10),
    )
    me = SimpleNamespace(
        id=30, guild=guild, guild_permissions=_permissions(),
        top_role=SimpleNamespace(position=20),
    )
    target = SimpleNamespace(
        id=40, guild=guild, top_role=SimpleNamespace(position=1), edit=AsyncMock(),
    )
    guild.me = me
    guild.default_role = SimpleNamespace(id=guild.id, name="@everyone")
    guild.get_member = lambda uid: {20: actor, 30: me, 40: target}.get(uid)
    channel = _channel(guild, 50)
    guild.channels = [channel]
    message = SimpleNamespace(id=60, author=actor, guild=guild, channel=channel)
    bot = SimpleNamespace(
        user=me, get_channel=lambda cid: channel if cid == channel.id else None,
        get_guild=lambda gid: guild if gid == guild.id else None,
        fetch_channel=AsyncMock(side_effect=AssertionError("unexpected channel fetch")),
        mark_message_tainted=Mock(),
    )
    return bot, message, target


def _channel(guild, cid, *, denied_actor=False, denied_bot=False, **flags):
    permissions = {
        20: _permissions(**flags),
        30: _permissions(**flags),
    }
    if denied_actor:
        permissions[20] = discord.Permissions.none()
    if denied_bot:
        permissions[30] = discord.Permissions.none()
    return SimpleNamespace(
        id=cid, name=f"room-{cid}", guild=guild,
        permissions_for=lambda member: permissions.get(member.id, discord.Permissions.none()),
        edit=AsyncMock(), delete=AsyncMock(),
        clone=AsyncMock(return_value=SimpleNamespace(id=cid + 1, name="clone")),
        category=SimpleNamespace(id=70), overwrites={},
    )


@pytest.mark.parametrize("denied", ["actor", "bot"])
@pytest.mark.parametrize(
    ("tool_class", "params"),
    [
        (guild_tools.EditChannelTool, {"name": "renamed"}),
        (guild_tools.DeleteChannelTool, {"confirm_name": "room-50"}),
        (guild_tools.MoveChannelTool, {"category_id": "none"}),
        (guild_tools.CloneChannelTool, {}),
        (guild_tools.SyncChannelTool, {}),
        (guild_tools.ListPermissionsTool, {}),
    ],
)
def test_guild_channel_actions_check_effective_target_permissions(tool_class, params, denied):
    bot, message, _ = _context()
    channel = _channel(message.guild, 50, **{f"denied_{denied}": True})
    bot.get_channel = lambda cid: channel

    result = asyncio.run(tool_class(bot).execute(message, channel_id="50", **params))

    assert result.startswith("Error:")
    channel.edit.assert_not_awaited()
    channel.delete.assert_not_awaited()
    channel.clone.assert_not_awaited()
    bot.fetch_channel.assert_not_awaited()


@pytest.mark.parametrize(
    ("tool_class", "params"),
    [
        (guild_tools.EditChannelTool, {"name": "renamed"}),
        (guild_tools.DeleteChannelTool, {"confirm_name": "room-50"}),
        (guild_tools.MoveChannelTool, {"category_id": "none"}),
        (guild_tools.CloneChannelTool, {}),
        (guild_tools.SyncChannelTool, {}),
        (guild_tools.ListPermissionsTool, {}),
    ],
)
def test_guild_channel_actions_allow_matching_effective_permissions(tool_class, params):
    bot, message, _ = _context()

    result = asyncio.run(tool_class(bot).execute(message, channel_id="50", **params))

    assert not result.startswith("Error:")


@pytest.mark.parametrize("tool_class", [
    guild_tools.CreateChannelTool, guild_tools.EditChannelTool,
    guild_tools.MoveChannelTool, guild_tools.CloneChannelTool,
])
def test_channel_creation_or_move_checks_destination_category(monkeypatch, tool_class):
    bot, message, _ = _context()
    category = _channel(message.guild, 70, denied_actor=True)
    monkeypatch.setattr(guild_tools, "_find_category", lambda *args: (category, ""))
    message.guild.create_text_channel = AsyncMock()

    result = asyncio.run(tool_class(bot).execute(
        message, channel_id="50", category_id="70", name="new-name",
    ))

    assert result.startswith("Error:")
    message.channel.edit.assert_not_awaited()
    message.channel.clone.assert_not_awaited()
    message.guild.create_text_channel.assert_not_awaited()


def test_edit_category_checks_category_overwrites(monkeypatch):
    bot, message, _ = _context()
    category = _channel(message.guild, 70, denied_actor=True)
    monkeypatch.setattr(guild_tools, "_find_category", lambda *args: (category, ""))

    result = asyncio.run(guild_tools.EditCategoryTool(bot).execute(
        message, category_id="70", name="renamed",
    ))

    assert result.startswith("Error:")
    category.edit.assert_not_awaited()


@pytest.mark.parametrize("action", ["sync", "lock", "lockdown"])
def test_bulk_channel_actions_skip_children_with_denied_overwrites(monkeypatch, action):
    bot, message, _ = _context()
    allowed = _channel(message.guild, 80)
    denied = _channel(message.guild, 90, denied_actor=True)

    class Category(SimpleNamespace):
        pass

    monkeypatch.setattr(discord, "CategoryChannel", Category)
    category = Category(**vars(_channel(message.guild, 70)), channels=[allowed, denied])
    message.guild.channels = [category, allowed, denied]
    bot.get_channel = lambda cid: category
    lock = AsyncMock(return_value="")
    monkeypatch.setattr(mod_tools, "_lock_target", lock)
    if action == "sync":
        result = asyncio.run(guild_tools.SyncChannelTool(bot).execute(message, channel_id="70"))
        allowed.edit.assert_awaited_once()
        denied.edit.assert_not_awaited()
        assert "failed" in result
    else:
        if action == "lock":
            result = asyncio.run(mod_tools.LockChannelTool(bot).execute(message, channel_id="70"))
        else:
            result = asyncio.run(mod_tools.LockdownTool(bot).execute(message, target="server"))
        lock.assert_awaited_once()
        assert lock.await_args.args[0] is allowed
        assert "1/2" in result


@pytest.mark.parametrize("action", ["mute", "unmute", "deafen", "undeafen", "disconnect", "move"])
@pytest.mark.parametrize("denied", ["actor", "bot"])
def test_voice_actions_check_target_voice_channel(action, denied):
    bot, message, target = _context()
    target.voice = SimpleNamespace(channel=_channel(
        message.guild, 80, **{f"denied_{denied}": True},
    ))

    result = asyncio.run(mod_tools.VoiceModTool(bot).execute(
        message, action=action, user_id="40", channel_id="90",
    ))

    assert result.startswith("Error:")
    target.edit.assert_not_awaited()
    bot.fetch_channel.assert_not_awaited()


def test_voice_move_checks_destination_effective_permissions(monkeypatch):
    bot, message, target = _context()
    target.voice = SimpleNamespace(channel=_channel(message.guild, 80))

    class Voice(SimpleNamespace):
        pass

    monkeypatch.setattr(discord, "VoiceChannel", Voice)
    destination = Voice(**vars(_channel(message.guild, 90, denied_actor=True)))
    bot.get_channel = lambda cid: destination

    result = asyncio.run(mod_tools.VoiceModTool(bot).execute(
        message, action="move", user_id="40", channel_id="90",
    ))

    assert result.startswith("Error:")
    target.edit.assert_not_awaited()


@pytest.mark.parametrize("allow", ["send_messages=false", "send_messages=inherit"])
def test_editing_overwrite_preserves_unrelated_private_channel_denies(allow):
    bot, message, _ = _context()
    original = discord.PermissionOverwrite(view_channel=False, send_messages=True, connect=False)
    message.channel.overwrites_for = Mock(return_value=original)
    message.channel.set_permissions = AsyncMock()

    result = asyncio.run(mod_tools.SetChannelPermissionsTool(bot).execute(
        message, channel_id="50", target="everyone", allow=allow,
    ))

    assert result.startswith("Updated overwrites")
    payload = message.channel.set_permissions.await_args.kwargs
    overwrite = payload["overwrite"]
    assert overwrite.view_channel is False
    assert overwrite.connect is False
    assert overwrite.send_messages is (None if allow.endswith("inherit") else False)
    assert set(payload) == {"overwrite", "reason"}


def test_reset_overwrite_still_explicitly_removes_whole_overwrite():
    bot, message, _ = _context()
    message.channel.set_permissions = AsyncMock()

    result = asyncio.run(mod_tools.SetChannelPermissionsTool(bot).execute(
        message, channel_id="50", target="everyone", reset=True,
    ))

    assert result.startswith("Cleared overwrites")
    assert message.channel.set_permissions.await_args.kwargs["overwrite"] is None


def test_unknown_overwrite_permission_does_not_silently_mutate():
    bot, message, _ = _context()
    message.channel.set_permissions = AsyncMock()

    result = asyncio.run(mod_tools.SetChannelPermissionsTool(bot).execute(
        message, channel_id="50", target="everyone", allow="not_a_permission=false",
    ))

    assert "unknown channel permissions" in result
    message.channel.set_permissions.assert_not_awaited()


@pytest.mark.parametrize("selector", ["message_id", "message_link", "reply_reference", "reference"])
def test_direct_message_lookup_marks_returned_untrusted_content_tainted(selector):
    bot, message, _ = _context()
    message.channel.fetch_message = AsyncMock(return_value=SimpleNamespace(
        id=100, content="untrusted instructions", author=message.author,
    ))
    params = {selector: "100"}
    if selector == "message_link":
        params[selector] = "https://discord.com/channels/10/50/100"
    elif selector == "reference":
        message.reference = SimpleNamespace(message_id=100)
        params = {}

    result = asyncio.run(message_tools.SearchMessagesTool(bot).execute(message, **params))

    assert result.startswith("Message lookup:")
    bot.mark_message_tainted.assert_called_once_with(message)


@pytest.mark.parametrize("denied", ["actor", "bot"])
def test_delete_cannot_remove_bot_message_from_unreadable_channel(denied):
    bot, message, _ = _context()
    destination = _channel(message.guild, 80, **{f"denied_{denied}": True})
    target = SimpleNamespace(author=bot.user, delete=AsyncMock())
    destination.fetch_message = AsyncMock(return_value=target)
    bot.get_channel = lambda cid: destination

    result = asyncio.run(message_tools.DeleteMessageTool(bot).execute(
        message, channel_id="80", message_id="100",
    ))

    assert result.startswith("Error:")
    destination.fetch_message.assert_not_awaited()
    target.delete.assert_not_awaited()


def test_delete_readable_bot_message_remains_available_without_manage_messages():
    bot, message, _ = _context()
    target = SimpleNamespace(author=bot.user, delete=AsyncMock())
    message.channel.fetch_message = AsyncMock(return_value=target)

    result = asyncio.run(message_tools.DeleteMessageTool(bot).execute(message, message_id="100"))

    assert result == "Deleted my message 100"
    target.delete.assert_awaited_once_with()


def test_forward_cannot_read_source_history_requester_cannot_read():
    bot, message, _ = _context()
    message.channel = _channel(message.guild, 50, read_message_history=False)
    message.channel.fetch_message = AsyncMock()

    result = asyncio.run(message_tools.ForwardMessageTool(bot).execute(
        message, message_id="100", channel_id="80",
    ))

    assert "cannot read" in result
    message.channel.fetch_message.assert_not_awaited()


@pytest.mark.parametrize("private", [False, True])
def test_forward_to_thread_uses_thread_send_permission(monkeypatch, private):
    bot, message, _ = _context()
    source = SimpleNamespace(forward=AsyncMock())
    message.channel.fetch_message = AsyncMock(return_value=source)

    class Thread(SimpleNamespace):
        pass

    monkeypatch.setattr(discord, "Thread", Thread)
    destination = Thread(
        **vars(_channel(message.guild, 80, send_messages=False)),
        type=discord.ChannelType.private_thread if private else discord.ChannelType.public_thread,
        fetch_thread_member=AsyncMock(return_value=SimpleNamespace(id=message.author.id)),
    )
    bot.get_channel = lambda cid: destination

    result = asyncio.run(message_tools.ForwardMessageTool(bot).execute(
        message, message_id="100", channel_id="80",
    ))

    assert result.startswith("Forwarded message")
    source.forward.assert_awaited_once_with(destination)
    if private:
        destination.fetch_thread_member.assert_awaited_once_with(message.author.id)
    else:
        destination.fetch_thread_member.assert_not_awaited()


def test_forward_refuses_private_thread_without_requester_membership(monkeypatch):
    bot, message, _ = _context()
    source = SimpleNamespace(forward=AsyncMock())
    message.channel.fetch_message = AsyncMock(return_value=source)

    class Thread(SimpleNamespace):
        pass

    monkeypatch.setattr(discord, "Thread", Thread)
    destination = Thread(
        **vars(_channel(message.guild, 80)), type=discord.ChannelType.private_thread,
        fetch_thread_member=AsyncMock(side_effect=discord.NotFound(
            SimpleNamespace(status=404, reason="Not Found"), "Unknown Member",
        )),
    )
    bot.get_channel = lambda cid: destination

    result = asyncio.run(message_tools.ForwardMessageTool(bot).execute(
        message, message_id="100", channel_id="80",
    ))

    assert "membership could not be verified" in result
    source.forward.assert_not_awaited()


@pytest.mark.parametrize("text", [None, "", "new activity"])
def test_invalid_activity_type_cannot_clear_or_replace_existing_activity(text):
    original = object()
    bot = SimpleNamespace(
        _custom_status=object(), _current_game=original,
        _build_activities=Mock(), change_presence=AsyncMock(),
    )

    result = asyncio.run(presence_tools.SetActivityTool(bot).execute(
        SimpleNamespace(), type="invalid", text=text,
    ))

    assert result.startswith("Error: type must be")
    assert bot._current_game is original
    bot._build_activities.assert_not_called()
    bot.change_presence.assert_not_awaited()


@pytest.mark.parametrize("explicit_channel", [False, True])
def test_invite_uses_channel_permission_grants_without_guild_wide_grant(explicit_channel):
    bot, message, _ = _context()
    invite_perms = _permissions(create_instant_invite=True)
    message.channel.permissions_for = lambda member: invite_perms
    message.channel.create_invite = AsyncMock(return_value=SimpleNamespace(
        url="https://discord.gg/offline-test",
    ))

    result = asyncio.run(message_tools.CreateInviteTool(bot).execute(
        message, **({"channel_id": "50"} if explicit_channel else {}),
    ))

    assert result.startswith("Invite created")
    message.channel.create_invite.assert_awaited_once_with(max_uses=1, max_age=86400)


def test_remote_invite_selection_skips_channel_requester_cannot_invite_from():
    bot, message, _ = _context()
    guild = message.guild
    denied = _channel(guild, 80)
    allowed = _channel(guild, 90, create_instant_invite=True)
    denied.permissions_for = lambda member: _permissions(
        create_instant_invite=member.id == guild.me.id,
    )
    for channel in (denied, allowed):
        channel.create_invite = AsyncMock(return_value=SimpleNamespace(
            url="https://discord.gg/offline-test",
        ))
    guild.system_channel = denied
    guild.text_channels = [denied, allowed]
    bot.guilds = [guild]
    message.guild = None
    message.channel = None

    result = asyncio.run(message_tools.CreateInviteTool(bot).execute(message, server="10"))

    assert result.startswith("Invite created")
    denied.create_invite.assert_not_awaited()
    allowed.create_invite.assert_awaited_once()
