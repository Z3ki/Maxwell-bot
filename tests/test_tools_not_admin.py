"""Creative and social tools stay open to everyone; leave_server does not."""

import asyncio
from types import SimpleNamespace

from bot import TOOL_PROTOCOL
from control_defaults import DEFAULT_CONTROL
from bot_tools import (
    BanMemberTool,
    CreateInviteTool,
    LeaveServerTool,
    ListAdminServersTool,
    ListServersTool,
    UpdateBasePersonalityTool,
    UpdateServerPromptTool,
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
        "update_base_personality / update_server_prompt: admin-only"
        not in TOOL_PROTOCOL
    )
    assert (
        "Sites, games, code, search, plugins and chat are open to everyone"
        in TOOL_PROTOCOL
    )
    personality = DEFAULT_CONTROL["base_personality"].lower()
    assert "run tools" not in personality
    assert "politely decline" not in personality
    # Personality may use {placeholders} instead of a literal name; the
    # access rule still has to stay in the default text.
    assert "open to everyone" in personality


def test_tool_protocol_states_dm_and_cross_chat_restrictions():
    text = TOOL_PROTOCOL.lower()
    assert "in dms, discord moderation" in text
    assert "send_message stays in the current chat" in text
    assert "from a dm you cannot send" in text
    assert "shell, sites" in text
    assert "list_channels" in text
    assert "list_roles" in text
    assert "list_members" in text
    assert "person asking" in text
    assert "manage_messages" in text
    assert "owner/admin authorization" not in text
    assert "call report" in text


def test_tool_descriptions_do_not_say_admin_only():
    bot = SimpleNamespace()
    for cls in (
        UpdateBasePersonalityTool,
        UpdateServerPromptTool,
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


def test_personality_tools_refuse_a_non_admin():
    bot = SimpleNamespace(_is_admin=lambda _uid: False)
    msg = SimpleNamespace(author=SimpleNamespace(id=999))
    personality = asyncio.run(
        UpdateBasePersonalityTool(bot).execute(msg, text="keep replies short and honest.")
    )
    prompt = asyncio.run(
        UpdateServerPromptTool(bot).execute(msg, server_id="1", text="be chill")
    )
    assert personality.startswith("Error:")
    assert prompt.startswith("Error:")
    assert "admin" in personality.lower()
    assert "admin" in prompt.lower()


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


def test_create_invite_reaches_guild_check_for_anyone():
    bot = SimpleNamespace(_is_admin=lambda _uid: False)
    msg = SimpleNamespace(author=SimpleNamespace(id=999), guild=None)
    result = asyncio.run(CreateInviteTool(bot).execute(msg))
    assert result == "Error: Cannot create invites in DMs"
