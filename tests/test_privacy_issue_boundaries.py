"""Regression coverage for the six open GitHub authorization/routing issues."""

import asyncio
import hashlib
from types import SimpleNamespace

import pytest

from bot import MaxwellBot
from bot_tools import (
    ListChannelsTool,
    ListMembersTool,
    ListRolesTool,
    LookupUserTool,
    CreateSiteTool,
    EditSiteTool,
)
from plugins.github_projects.impl import GitHubProjectService
from test_site_tools import bot, _msg, PAGE  # noqa: F401 fixture import
from test_guild_listings import _guild, _text


@pytest.mark.parametrize("tool", [ListChannelsTool, ListMembersTool, ListRolesTool])
def test_issue80_cannot_probe_another_guild(tool):
    guild, *_ = _guild()
    msg = SimpleNamespace(guild=SimpleNamespace(id=500), author=SimpleNamespace(id=777))
    runtime = SimpleNamespace(get_guild=lambda uid: guild)
    result = asyncio.run(tool(runtime).execute(msg, guild_id="10"))
    assert "restricted to the current server" in result
    assert "Villa" not in result


def test_issue80_private_channels_are_not_listed():
    guild, general, *_ = _guild()
    general.permissions_for = lambda member: SimpleNamespace(view_channel=True)
    secret = _text(cid=99, name="SECRET_STAFF", topic="SECRET_TOPIC")
    secret.permissions_for = lambda member: SimpleNamespace(view_channel=False)
    guild.channels.append(secret)
    msg = SimpleNamespace(guild=guild, author=SimpleNamespace(id=777))
    result = asyncio.run(ListChannelsTool(SimpleNamespace()).execute(msg))
    assert "general" in result
    assert "SECRET" not in result


@pytest.mark.parametrize("dm", [True, False])
def test_issue81_lookup_never_reports_other_server_voice(dm):
    guild, *_ = _guild()
    vc = SimpleNamespace(
        id=99,
        name="SECRET_ROOM",
        guild=guild,
        permissions_for=lambda member: SimpleNamespace(view_channel=True),
    )
    target = SimpleNamespace(id=4242, voice=SimpleNamespace(channel=vc))
    guild.get_member = lambda uid: target
    user = SimpleNamespace(
        id=4242,
        name="target",
        display_name="Target",
        bot=False,
        created_at=None,
        display_avatar=SimpleNamespace(url="avatar"),
        banner=None,
        accent_color=None,
    )
    runtime = SimpleNamespace(get_user=lambda uid: user, guilds=[guild])
    current = None if dm else SimpleNamespace(id=200, get_member=lambda uid: None)
    msg = SimpleNamespace(guild=current, author=SimpleNamespace(id=777))
    result = asyncio.run(LookupUserTool(runtime).execute(msg, user_id="4242"))
    assert "SECRET_ROOM" not in result and "Villa" not in result


def test_issue81_invisible_current_voice_is_not_reported():
    guild, *_ = _guild()
    vc = SimpleNamespace(
        id=99,
        name="SECRET_ROOM",
        permissions_for=lambda member: SimpleNamespace(view_channel=False),
    )
    user = SimpleNamespace(
        id=4242,
        name="target",
        display_name="Target",
        bot=False,
        created_at=None,
        display_avatar=SimpleNamespace(url="avatar"),
        banner=None,
        accent_color=None,
    )
    guild.get_member = lambda uid: SimpleNamespace(voice=SimpleNamespace(channel=vc))
    msg = SimpleNamespace(guild=guild, author=SimpleNamespace(id=777))
    result = asyncio.run(
        LookupUserTool(SimpleNamespace(get_user=lambda uid: user)).execute(
            msg, user_id="4242"
        )
    )
    assert "SECRET_ROOM" not in result


@pytest.mark.parametrize(
    "action,extra",
    [
        ("rename", {"title": "stolen"}),
        ("backend", {"backend": "clear"}),
        ("backend", {"backend": True}),
        ("extend", {"permanent": True}),
        ("title", {"title": "stolen"}),
        ("renew", {}),
    ],
)
def test_issue79_all_site_metadata_mutations_require_owner(bot, action, extra):
    asyncio.run(
        CreateSiteTool(bot).execute(_msg(uid=1), name="mine", title="Mine", body=PAGE)
    )
    before = dict(bot._sites["mine"])
    result = asyncio.run(
        EditSiteTool(bot).execute(_msg(uid=2), name="mine", action=action, **extra)
    )
    assert "belongs to someone else" in result
    assert bot._sites["mine"] == before


def test_issue75_kv_guide_includes_deployment_path(bot):
    bot.config.MAXWELL_PUBLIC_BASE_URL = "https://maxwell.example.com/dev"
    result = asyncio.run(
        CreateSiteTool(bot).execute(
            _msg(uid=1), name="mine", title="Mine", body=PAGE, backend=True
        )
    )
    assert "/dev/api/site/mine" in result
    result = asyncio.run(
        EditSiteTool(bot).execute(
            _msg(uid=1), name="mine", action="backend", backend=True
        )
    )
    assert "/dev/api/site/mine" in result


@pytest.mark.parametrize(
    "action,method",
    [
        ("status", "GET"),
        ("read", "GET"),
        ("request", "GET"),
        ("request", "HEAD"),
        ("request", "OPTIONS"),
    ],
)
def test_issue74_private_backend_reads_reach_tool_gate(action, method):
    runtime = SimpleNamespace(
        _control={}, config=SimpleNamespace(), plugin_manager=None
    )
    handler = SimpleNamespace(
        tool_name="site_server",
        requires_admin=False,
        get_parameters=lambda: {"type": "object", "properties": {}},
    )
    msg = SimpleNamespace(
        author=SimpleNamespace(id=1), guild=None, response_visibility="private"
    )
    result = MaxwellBot._authorize_tool_execution(
        runtime, msg, "site_server", handler, {"action": action, "method": method}
    )
    assert result is None


def test_issue74_private_backend_mutations_are_still_blocked():
    runtime = SimpleNamespace(
        _control={}, config=SimpleNamespace(), plugin_manager=None
    )
    handler = SimpleNamespace(tool_name="site_server", requires_admin=False)
    msg = SimpleNamespace(
        author=SimpleNamespace(id=1), guild=None, response_visibility="private"
    )
    result = MaxwellBot._authorize_tool_execution(
        runtime, msg, "site_server", handler, {"action": "request", "method": "POST"}
    )
    assert "choose Public visibility" in result


def test_issue76_default_container_identity_stays_legacy(monkeypatch):
    monkeypatch.setattr("plugins.github_projects.impl.controller_id", lambda: "")
    expected = "maxwell-gh-" + hashlib.sha256(b"123456789012345678").hexdigest()[:16]
    assert GitHubProjectService._container("123456789012345678") == expected
    monkeypatch.setattr("plugins.github_projects.impl.controller_id", lambda: "dev")
    assert GitHubProjectService._container("123456789012345678") != expected
