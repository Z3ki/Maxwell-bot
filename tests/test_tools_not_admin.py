"""Creative and social tools stay open to everyone; join_server does not.

The one exception is deliberate: joining a server parks the account somewhere
permanent, under moderators nobody here controls, so it is gated on identity.
"""

import asyncio
from types import SimpleNamespace

from bot import TOOL_PROTOCOL
from control_defaults import DEFAULT_CONTROL
from bot_tools import (
    BanMemberTool,
    CreateInviteTool,
    JoinServerTool,
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
    assert "open to everyone" in personality


def test_tool_protocol_states_the_join_server_restriction():
    """The prompt has to agree with the code, or the model promises and fails."""
    assert "join_server is admin-only" in TOOL_PROTOCOL


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


def test_join_server_description_announces_the_restriction():
    """The model should decline up front rather than call and get refused."""
    desc = JoinServerTool(SimpleNamespace()).get_description().lower()
    assert "admins" in desc


def test_join_server_refuses_a_non_admin():
    bot = SimpleNamespace(_is_admin=lambda _uid: False)
    msg = SimpleNamespace(author=SimpleNamespace(id=999))
    result = asyncio.run(
        JoinServerTool(bot).execute(msg, invite="https://discord.gg/abcdef")
    )
    assert result.startswith("Error:")
    assert "admin" in result.lower()


def test_join_server_refuses_before_touching_the_invite():
    """A refusal must not fetch the invite — that is an observable side effect."""
    fetched = []

    async def fetch_invite(code, **_kwargs):
        fetched.append(code)
        raise AssertionError("should never be reached for a non-admin")

    bot = SimpleNamespace(_is_admin=lambda _uid: False, fetch_invite=fetch_invite)
    msg = SimpleNamespace(author=SimpleNamespace(id=999))
    asyncio.run(JoinServerTool(bot).execute(msg, invite="discord.gg/xyz"))
    assert fetched == []


def test_join_server_lets_an_admin_through_to_the_invite_lookup():
    seen = []

    async def fetch_invite(code, **_kwargs):
        seen.append(code)
        raise RuntimeError("stop here — the gate already passed")

    bot = SimpleNamespace(
        _is_admin=lambda uid: str(uid) == "42", fetch_invite=fetch_invite
    )
    msg = SimpleNamespace(author=SimpleNamespace(id=42))
    result = asyncio.run(
        JoinServerTool(bot).execute(msg, invite="https://discord.gg/abcdef")
    )
    assert seen == ["abcdef"]
    assert "restricted to admins" not in result


def test_join_server_survives_a_bot_without_the_admin_helper():
    """Fail closed rather than raising if _is_admin is missing."""
    bot = SimpleNamespace()
    msg = SimpleNamespace(author=SimpleNamespace(id=1))
    result = asyncio.run(JoinServerTool(bot).execute(msg, invite="discord.gg/xyz"))
    assert result.startswith("Error:")


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
