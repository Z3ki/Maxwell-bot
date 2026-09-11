"""Guild structure and moderation tools: categories, lockdown, sync, softban."""

import asyncio
from types import SimpleNamespace

from bot_tools import (
    CreateChannelTool,
    EditCategoryTool,
    LockdownTool,
    MoveChannelTool,
    _CAP_TOOLS,
    _find_category,
    _is_youtube_url,
    DM_BLOCKED_TOOLS,
)
from control_defaults import KNOWN_TOOLS
from tool_schemas import TOOL_PARAMETERS


def _perms(**flags):
    flags.setdefault("administrator", False)
    flags.setdefault("manage_channels", False)
    flags.setdefault("manage_roles", False)
    flags.setdefault("ban_members", False)
    flags.setdefault("moderate_members", False)
    return SimpleNamespace(**flags)


def _member(*, uid=1, name="Max", position=2, perms=None):
    return SimpleNamespace(
        id=uid,
        display_name=name,
        name=name,
        guild_permissions=perms or _perms(manage_channels=True, administrator=True),
        top_role=SimpleNamespace(id=uid, name=name, position=position),
        roles=[],
    )


def test_is_youtube_url_rejects_watch_and_short_hosts():
    assert _is_youtube_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert _is_youtube_url("https://youtu.be/dQw4w9WgXcQ")
    assert _is_youtube_url("https://music.youtube.com/watch?v=abc")
    assert not _is_youtube_url("https://example.com/watch?v=dQw4w9WgXcQ")
    assert not _is_youtube_url("https://cdn.discordapp.com/attachments/1/2/a.mp4")


def test_find_category_none_uncategorizes():
    guild = SimpleNamespace(name="Villa", categories=[])
    cat, err = _find_category(guild, "none", None)
    assert cat is None
    assert err == ""


def test_find_category_by_name():
    wanted = SimpleNamespace(id=20, name="chat")
    guild = SimpleNamespace(name="Villa", categories=[wanted])
    cat, err = _find_category(guild, None, "chat")
    assert err == ""
    assert cat is wanted


def test_new_structure_tools_are_registered():
    for name in (
        "edit_category",
        "move_channel",
        "clone_channel",
        "sync_channel",
        "lockdown",
        "list_permissions",
        "manage_invites",
        "softban_member",
        "list_timeouts",
    ):
        assert name in KNOWN_TOOLS
        assert name in TOOL_PARAMETERS
        assert name in DM_BLOCKED_TOOLS


def test_manage_channels_unlocks_category_tools():
    names = _CAP_TOOLS["manage_channels"]
    for name in (
        "create_category",
        "create_channel",
        "edit_category",
        "move_channel",
        "clone_channel",
        "sync_channel",
        "lockdown",
        "list_permissions",
    ):
        assert name in names


def test_create_channel_rejects_unknown_kind():
    me = _member()
    guild = SimpleNamespace(
        id=10, name="Villa", me=me, categories=[], create_text_channel=None
    )
    bot = SimpleNamespace(get_guild=lambda _gid: None)
    msg = SimpleNamespace(guild=guild, author=SimpleNamespace(id=5))
    result = asyncio.run(
        CreateChannelTool(bot).execute(msg, name="general", kind="not-a-type")
    )
    assert "text, voice, announcement, forum, or stage" in result


def test_edit_category_requires_a_field():
    me = _member()
    cat = SimpleNamespace(id=20, name="chat")
    guild = SimpleNamespace(id=10, name="Villa", me=me, categories=[cat])
    bot = SimpleNamespace(get_guild=lambda _gid: None)
    msg = SimpleNamespace(guild=guild, author=SimpleNamespace(id=5))
    result = asyncio.run(
        EditCategoryTool(bot).execute(msg, category_id="20")
    )
    assert "provide name, position, or nsfw" in result


def test_lockdown_requires_target():
    me = _member()
    guild = SimpleNamespace(id=10, name="Villa", me=me, channels=[])
    bot = SimpleNamespace(get_guild=lambda _gid: None)
    msg = SimpleNamespace(guild=guild, author=SimpleNamespace(id=5))
    result = asyncio.run(LockdownTool(bot).execute(msg))
    assert "target is required" in result


def test_move_channel_requires_channel_id():
    me = _member()
    bot = SimpleNamespace(get_guild=lambda _gid: None)
    msg = SimpleNamespace(
        guild=SimpleNamespace(id=10, name="Villa", me=me),
        author=SimpleNamespace(id=5),
    )
    result = asyncio.run(MoveChannelTool(bot).execute(msg))
    assert "channel_id is required" in result
