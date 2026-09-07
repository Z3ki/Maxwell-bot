"""list_channels / list_roles / list_members and the per-turn server map."""

import asyncio
from types import SimpleNamespace

from bot_tools import (
    ListChannelsTool,
    ListMembersTool,
    ListRolesTool,
    ListServersTool,
    _format_member_line,
    _guild_room_context,
    _render_channel_map,
    _render_member_list,
    _render_role_list,
)


def _role(
    *,
    rid,
    name,
    position=1,
    colour=0,
    hoist=False,
    mentionable=False,
    managed=False,
    members=None,
    perms=None,
):
    return SimpleNamespace(
        id=rid,
        name=name,
        position=position,
        colour=SimpleNamespace(value=colour),
        hoist=hoist,
        mentionable=mentionable,
        managed=managed,
        members=list(members or []),
        permissions=perms or SimpleNamespace(administrator=False),
        is_default=lambda: name == "@everyone",
    )


def _text(*, cid, name, category_id=None, topic="", nsfw=False, slowmode=0, position=0):
    return SimpleNamespace(
        id=cid,
        name=name,
        kind="text",
        type="text",
        category_id=category_id,
        topic=topic,
        nsfw=nsfw,
        slowmode_delay=slowmode,
        position=position,
    )


def _voice(*, cid, name, category_id=None, members=None, user_limit=0, bitrate=64000):
    return SimpleNamespace(
        id=cid,
        name=name,
        kind="voice",
        type="voice",
        category_id=category_id,
        members=list(members or []),
        user_limit=user_limit,
        bitrate=bitrate,
        position=0,
    )


def _category(*, cid, name, position=0):
    return SimpleNamespace(
        id=cid,
        name=name,
        kind="category",
        type="category",
        category_id=None,
        position=position,
    )


def _member(
    *,
    uid,
    name,
    display=None,
    nick=None,
    bot=False,
    roles=None,
    status="online",
    joined=None,
):
    return SimpleNamespace(
        id=uid,
        name=name,
        display_name=display or nick or name,
        nick=nick,
        bot=bot,
        pending=False,
        roles=list(roles or []),
        status=status,
        joined_at=joined,
        voice=None,
    )


def _guild():
    everyone = _role(rid=10, name="@everyone", position=0)
    mod = _role(
        rid=11,
        name="Mod",
        position=5,
        colour=0xFF0000,
        hoist=True,
        mentionable=True,
        perms=SimpleNamespace(administrator=False, kick_members=True, ban_members=True),
    )
    cat = _category(cid=20, name="chat", position=0)
    general = _text(
        cid=21, name="general", category_id=20, topic="say hi", slowmode=5
    )
    lounge = _voice(
        cid=22,
        name="lounge",
        category_id=20,
        members=[_member(uid=2, name="bob")],
        user_limit=8,
    )
    ada = _member(
        uid=2,
        name="ada",
        nick="Ada",
        roles=[everyone, mod],
        status="online",
        joined=SimpleNamespace(strftime=lambda fmt: "2026-01-02"),
    )
    bob = _member(uid=3, name="bob", display="Bobby", roles=[everyone], status="idle")
    me = _member(uid=1, name="maxwell", nick="Sparky", roles=[everyone, mod])
    guild = SimpleNamespace(
        id=10,
        name="Villa",
        owner_id=9,
        me=me,
        member_count=3,
        roles=[everyone, mod],
        channels=[cat, general, lounge],
        members=[me, ada, bob],
        get_member=lambda uid: ada if int(uid) == 2 else None,
        get_role=lambda rid: mod if int(rid) == 11 else everyone if int(rid) == 10 else None,
    )
    return guild, general, mod, ada


def test_channel_map_includes_topic_voice_and_ids():
    guild, _general, _mod, _ada = _guild()
    out = _render_channel_map(guild)
    assert "Villa" in out
    assert "#general (21) text" in out
    assert "topic=say hi" in out
    assert "slowmode=5s" in out
    assert "lounge (22) voice" in out
    assert "users=1/8" in out
    assert "in=bob" in out
    assert "chat (20) category" in out


def test_channel_map_filters_kind_and_query():
    guild, _general, _mod, _ada = _guild()
    voice = _render_channel_map(guild, kind="voice")
    assert "lounge" in voice
    assert "general" not in voice
    named = _render_channel_map(guild, query="gen")
    assert "general" in named
    assert "lounge" not in named


def test_role_list_includes_color_counts_and_perms():
    guild, _general, mod, ada = _guild()
    mod.members = [ada]
    out = _render_role_list(guild)
    assert "Mod (11, pos 5)" in out
    assert "#FF0000" in out
    assert "1 members" in out
    assert "hoist" in out
    assert "kick_members" in out


def test_member_list_includes_nick_roles_status_and_role_filter():
    guild, _general, _mod, _ada = _guild()
    out = _render_member_list(guild)
    assert "Ada (2) @ada" in out
    assert "roles=Mod" in out
    assert "status=online" in out
    assert "joined=2026-01-02" in out
    assert "Bobby (3) @bob" in out
    only_mod = _render_member_list(guild, role_spec="Mod")
    assert "Ada" in only_mod
    assert "Bobby" not in only_mod
    idle = _render_member_list(guild, status="idle")
    assert "Bobby" in idle
    assert "Ada" not in idle


def test_format_member_line_marks_bots_and_timeouts():
    member = _member(uid=9, name="helper", bot=True, status="online")
    member.timed_out_until = SimpleNamespace(strftime=lambda fmt: "2026-09-08")
    line = _format_member_line(member)
    assert "bot" in line
    assert "timed_out_until=2026-09-08" in line


def test_room_context_mentions_list_tools_and_recent_people():
    guild, general, _mod, ada = _guild()
    text = _guild_room_context(guild, general, recent_users={"2": "Ada"})
    assert "This channel:" in text
    assert "#general (21)" in text
    assert "list_channels / list_roles / list_members" in text
    assert "Ada (2) @ada" in text
    assert _guild_room_context(None) == ""


def test_list_tools_execute_against_current_guild():
    guild, general, _mod, _ada = _guild()
    msg = SimpleNamespace(guild=guild, author=SimpleNamespace(id=5), channel=general)
    bot = SimpleNamespace(get_guild=lambda _gid: None)

    channels = asyncio.run(ListChannelsTool(bot).execute(msg, kind="text"))
    assert "#general (21)" in channels
    roles = asyncio.run(ListRolesTool(bot).execute(msg, query="mod"))
    assert "Mod (11" in roles
    members = asyncio.run(ListMembersTool(bot).execute(msg, query="ada", limit="10"))
    assert "Ada (2)" in members
    servers = asyncio.run(
        ListServersTool(SimpleNamespace(guilds=[guild], private_channels=[])).execute(
            msg
        )
    )
    assert "Villa (ID: 10)" in servers
    assert "members~3" in servers
    assert "my_nick=Sparky" in servers
    assert "channels=1t/1v/1c" in servers
