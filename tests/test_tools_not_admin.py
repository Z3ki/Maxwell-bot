"""Creative and social tools stay open to everyone; leave_server does not."""

import asyncio
from types import SimpleNamespace

from bot import TOOL_PROTOCOL, MAXWELL_BASE_KNOWLEDGE
from control_defaults import DEFAULT_CONTROL
from bot_tools import (
    BanMemberTool,
    CreateInviteTool,
    LeaveServerTool,
    ListAdminServersTool,
    ListServersTool,
    CreateCategoryTool,
    CreateChannelTool,
    EditChannelTool,
    DeleteChannelTool,
    KickMemberTool,
    ManageRoleTool,
    PurgeMessagesTool,
    TimeoutMemberTool,
)


def test_tool_protocol_keeps_creative_tools_open():
    assert (
        "update_base_personality" not in TOOL_PROTOCOL
        and "update_server_prompt" not in TOOL_PROTOCOL
    )
    assert (
        "Sites, games, code, search, plugins and ordinary chat are available to everyone"
        in TOOL_PROTOCOL
    )
    personality = DEFAULT_CONTROL["base_personality"].lower()
    assert "run tools" not in personality
    assert "politely decline" not in personality
    # Access rules belong to the code-owned protocol, not mutable style.
    assert "permission" not in personality


def test_tool_protocol_states_dm_and_cross_chat_restrictions():
    text = TOOL_PROTOCOL.lower()
    assert "in dms, guild listing, moderation, structure and forwarding tools are unavailable" in text
    assert "send_message from a dm stays in that chat" in text
    assert "both bot and requester must have create_instant_invite there" in text
    assert "sending to another channel or dm requires runtime admin authorization" in text
    assert "sites, search" in text
    assert "list_channels" in text
    assert "list_roles" in text
    assert "list_members" in text
    assert "requester to have the matching discord permission" in MAXWELL_BASE_KNOWLEDGE.lower()
    assert "manage_messages" in MAXWELL_BASE_KNOWLEDGE.lower()
    assert "owner/admin authorization" not in text
    assert "use report" in text


def test_tool_descriptions_do_not_say_admin_only():
    bot = SimpleNamespace()
    for cls in (
        ListServersTool,
        ListAdminServersTool,
        CreateInviteTool,
        CreateCategoryTool,
        CreateChannelTool,
        EditChannelTool,
        DeleteChannelTool,
        KickMemberTool,
        BanMemberTool,
        TimeoutMemberTool,
        ManageRoleTool,
        PurgeMessagesTool,
    ):
        desc = cls(bot).get_description().lower()
        assert "admin-only" not in desc
        assert "admin only" not in desc


def test_send_message_description_announces_cross_chat_restriction():
    from bot_tools import SendMessageTool

    desc = SendMessageTool(SimpleNamespace()).get_description().lower()
    assert "admin-only" in desc
    assert "channel_id" in desc
    assert "not available from dms" in desc


def test_leave_server_refuses_a_non_admin():
    bot = SimpleNamespace(_is_admin=lambda _uid: False, guilds=[])
    msg = SimpleNamespace(author=SimpleNamespace(id=999))
    result = asyncio.run(LeaveServerTool(bot).execute(msg, server="Villa"))
    assert result.startswith("Error:")
    assert "admin" in result.lower()


def test_list_servers_works_for_anyone():
    bot = SimpleNamespace(
        guilds=[SimpleNamespace(name="Villa", id=1)],
        private_channels=[],
        _is_admin=lambda _uid: False,
    )
    msg = SimpleNamespace(author=SimpleNamespace(id=999))
    result = asyncio.run(ListServersTool(bot).execute(msg))
    assert "admin-only" not in result
    assert "Villa" in result


def test_create_invite_requires_server_from_dms():
    bot = SimpleNamespace(_is_admin=lambda _uid: False, guilds=[])
    msg = SimpleNamespace(author=SimpleNamespace(id=999), guild=None)
    result = asyncio.run(CreateInviteTool(bot).execute(msg))
    assert "which server" in result


def _invite_guild(*, gid=2, name="Other", asker_id=5, can_invite=True):
    perms = SimpleNamespace(
        administrator=False, create_instant_invite=can_invite
    )

    async def make_invite(**kwargs):
        return SimpleNamespace(url="https://discord.gg/abc")

    channel = SimpleNamespace(
        id=21,
        name="general",
        create_invite=make_invite,
        permissions_for=lambda _m: perms,
    )
    asker = SimpleNamespace(
        id=asker_id,
        display_name="Ada",
        guild_permissions=perms,
    )
    guild = SimpleNamespace(
        id=gid,
        name=name,
        me=SimpleNamespace(guild_permissions=perms),
        get_member=lambda uid: asker if uid == asker_id else None,
        system_channel=channel,
        text_channels=[channel],
        channels=[channel],
    )
    channel.guild = guild
    return guild


def test_create_invite_for_another_server_by_name():
    other = _invite_guild()
    here = SimpleNamespace(id=1, name="Here")
    bot = SimpleNamespace(
        _is_admin=lambda _uid: False,
        guilds=[here, other],
        get_guild=lambda gid: other if gid == 2 else here,
    )
    msg = SimpleNamespace(
        guild=here,
        author=SimpleNamespace(id=5, guild=here),
        channel=SimpleNamespace(id=11),
    )
    result = asyncio.run(CreateInviteTool(bot).execute(msg, server="Other"))
    assert "https://discord.gg/abc" in result
    assert "Other" in result


def test_create_invite_refuses_without_target_perm():
    other = _invite_guild(can_invite=False)
    bot = SimpleNamespace(
        _is_admin=lambda _uid: False,
        guilds=[other],
        get_guild=lambda gid: other if gid == 2 else None,
    )
    msg = SimpleNamespace(
        guild=None,
        author=SimpleNamespace(id=5),
        channel=None,
    )
    result = asyncio.run(CreateInviteTool(bot).execute(msg, server="Other"))
    assert result.startswith("Error:")
    assert "create_instant_invite" in result
