"""Tools are open to everyone — none are Maxwell-admin-only."""

import asyncio
from types import SimpleNamespace

from bot import TOOL_PROTOCOL
from bot_tools import (
    BanMemberTool,
    CreateInviteTool,
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


def test_tool_protocol_does_not_mark_tools_admin_only():
    assert "update_base_personality / update_server_prompt: admin-only" not in TOOL_PROTOCOL
    assert "none are admin-only" in TOOL_PROTOCOL


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
