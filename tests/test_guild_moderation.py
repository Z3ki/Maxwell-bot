"""Discord guild access lines and mod tools check live permissions."""

import asyncio
from types import SimpleNamespace

from bot import MaxwellBot
from bot_tools import (
    KickMemberTool,
    ListAdminServersTool,
    _admin_caps,
    _guild_access_line,
    _user_access_line,
)


def _perms(**flags):
    flags.setdefault("administrator", False)
    return SimpleNamespace(**flags)


def _member(*, uid, name, position, perms, roles=None):
    return SimpleNamespace(
        id=uid,
        display_name=name,
        name=name,
        guild_permissions=perms,
        top_role=SimpleNamespace(id=uid, name=name, position=position),
        roles=list(roles or []),
    )


def test_admin_caps_reads_kick_members():
    me = _member(uid=1, name="Max", position=2, perms=_perms(kick_members=True))
    guild = SimpleNamespace(id=10, name="Villa", me=me)
    caps, reason = _admin_caps(guild)
    assert reason == ""
    assert "kick_members" in caps
    assert "administrator" not in caps


def test_guild_access_line_lists_roles_perms_and_tools():
    role = SimpleNamespace(
        name="Mod",
        id=2,
        position=5,
        is_default=lambda: False,
        permissions=_perms(kick_members=True),
    )
    me = _member(
        uid=1,
        name="Max",
        position=5,
        perms=_perms(kick_members=True),
        roles=[role],
    )
    guild = SimpleNamespace(id=10, name="Villa", me=me)
    line = _guild_access_line(guild)
    assert "Your Discord access in Villa" in line
    assert "Mod" in line
    assert "kick_members" in line
    assert "kick_member" in line


def test_list_admin_servers_includes_current_server_without_extra_perms():
    here_me = _member(uid=1, name="Max", position=1, perms=_perms())
    here = SimpleNamespace(
        id=1, name="Here", me=here_me, channels=[], owner_id=9
    )
    there_me = _member(
        uid=1, name="Max", position=1, perms=_perms(administrator=True)
    )
    there = SimpleNamespace(
        id=2, name="There", me=there_me, channels=[], owner_id=9
    )
    bot = SimpleNamespace(get_guild=lambda gid: there if gid == 2 else here, guilds=[here, there])
    msg = SimpleNamespace(guild=here, author=SimpleNamespace(id=5))
    result = asyncio.run(ListAdminServersTool(bot).execute(msg))
    assert "This server:" in result
    assert "Here" in result
    assert "There" in result


def test_kick_member_refuses_without_permission():
    me = _member(uid=1, name="Max", position=2, perms=_perms(kick_members=False))
    guild = SimpleNamespace(id=10, name="Villa", me=me, owner_id=9, get_member=lambda _uid: None)
    bot = SimpleNamespace(get_guild=lambda _gid: None)
    msg = SimpleNamespace(guild=guild, author=SimpleNamespace(id=5))
    result = asyncio.run(KickMemberTool(bot).execute(msg, user_id="99"))
    assert "kick_members" in result
    assert "Error:" in result


def test_kick_member_blocks_equal_or_higher_role():
    me = _member(uid=1, name="Max", position=1, perms=_perms(kick_members=True))
    target = _member(uid=99, name="Alice", position=5, perms=_perms())
    asker = _member(uid=5, name="Mod", position=2, perms=_perms(kick_members=True))
    guild = SimpleNamespace(
        id=10,
        name="Villa",
        me=me,
        owner_id=9,
        get_member=lambda uid: target if int(uid) == 99 else None,
    )
    bot = SimpleNamespace(get_guild=lambda _gid: None)
    msg = SimpleNamespace(guild=guild, author=asker)
    result = asyncio.run(KickMemberTool(bot).execute(msg, user_id="99"))
    assert "hierarchy" in result.lower()
    assert "Alice" in result


def test_kick_member_refuses_when_asker_lacks_permission():
    me = _member(uid=1, name="Max", position=2, perms=_perms(kick_members=True))
    asker = _member(uid=5, name="Ada", position=1, perms=_perms(kick_members=False))
    guild = SimpleNamespace(
        id=10, name="Villa", me=me, owner_id=9, get_member=lambda _uid: None
    )
    bot = SimpleNamespace(get_guild=lambda _gid: None)
    msg = SimpleNamespace(guild=guild, author=asker)
    result = asyncio.run(KickMemberTool(bot).execute(msg, user_id="99"))
    assert result.startswith("Error:")
    assert "Ada" in result
    assert "kick_members" in result
    assert "person asking" in result.lower()


def test_user_access_line_lists_all_roles_and_role_perms():
    helper = SimpleNamespace(
        name="Helper",
        id=3,
        position=2,
        is_default=lambda: False,
        permissions=_perms(manage_messages=True),
    )
    mod = SimpleNamespace(
        name="Mod",
        id=2,
        position=5,
        is_default=lambda: False,
        permissions=_perms(kick_members=True, ban_members=True),
    )
    asker = _member(
        uid=5,
        name="Ada",
        position=5,
        perms=_perms(kick_members=True, ban_members=True, manage_messages=True),
        roles=[helper, mod],
    )
    guild = SimpleNamespace(id=10, name="Villa", me=asker)
    line = _user_access_line(guild, asker)
    assert "Asker Ada (5) Discord access in Villa" in line
    assert "Helper [manage_messages]" in line
    assert "Mod [kick_members, ban_members]" in line or "Mod [ban_members, kick_members]" in line
    assert "kick_members" in line
    assert "kick_member" in line


def test_turn_hides_kick_when_asker_lacks_permission():
    me = _member(uid=1, name="Max", position=2, perms=_perms(kick_members=True))
    asker = _member(uid=5, name="Ada", position=1, perms=_perms(kick_members=False))
    guild = SimpleNamespace(id=10, name="Villa", me=me)
    bot = SimpleNamespace(
        tools={"kick_member": object(), "delete_message": object(), "send_message": object()},
        _control={"disabled_tools": []},
        plugin_manager=None,
        _is_admin=lambda _uid: False,
    )
    msg = SimpleNamespace(
        guild=guild,
        author=asker,
        channel=SimpleNamespace(id=1, guild=guild),
    )
    names = MaxwellBot._turn_tool_names(bot, "discord", msg, "kick them")
    assert "kick_member" not in names
    assert "delete_message" in names
    assert "send_message" in names
