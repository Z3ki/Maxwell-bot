"""Per-turn action permission summary and the matching mod tool refusals."""

import asyncio
from types import SimpleNamespace

import bot as bot_module
from bot import MaxwellBot
from tooling.helpers import (
    _GATED_MOD_TOOLS,
    _mod_tool_refusal,
    _mod_tools_allowed,
    _permission_context_line,
)

ALL_FALSE = (
    "administrator",
    "manage_guild",
    "manage_channels",
    "manage_roles",
    "manage_messages",
    "manage_nicknames",
    "manage_webhooks",
    "manage_expressions",
    "manage_emojis",
    "manage_emojis_and_stickers",
    "manage_events",
    "manage_threads",
    "kick_members",
    "ban_members",
    "moderate_members",
    "mute_members",
    "deafen_members",
    "move_members",
    "view_audit_log",
    "mention_everyone",
    "pin_messages",
    "create_instant_invite",
    "create_expressions",
)


def _perms(**flags):
    values = dict.fromkeys(ALL_FALSE, False)
    values.update(flags)
    return SimpleNamespace(**values)


class _Channel:
    def __init__(self, name, by_member):
        self.name = name
        self.id = 500
        self._by_member = by_member

    def permissions_for(self, member):
        return self._by_member[member.id]


def _setup(*, user=None, bot=None, voice=None, guild_name="Villa"):
    me = SimpleNamespace(id=1, name="Max", display_name="Max")
    asker = SimpleNamespace(id=2, name="ana", display_name="Ana", voice=voice)
    guild = SimpleNamespace(id=10, name=guild_name, me=me)
    me.guild = guild
    asker.guild = guild
    channel = _Channel("general", {1: bot or _perms(), 2: user or _perms()})
    message = SimpleNamespace(guild=guild, channel=channel, author=asker)
    return guild, message


def test_line_splits_available_asker_and_bot_gaps():
    guild, message = _setup(
        user=_perms(manage_channels=True, move_members=True, ban_members=True),
        bot=_perms(
            manage_channels=True,
            kick_members=True,
            ban_members=False,
            move_members=False,
        ),
    )
    line = _permission_context_line(guild, message)
    assert line.startswith("Action permissions in #general")
    assert "available=manage_channels |" in line
    assert "asker lacks=kick_members" in line
    assert "I lack=move_members, ban_members" in line
    assert "both lack=" in line and "manage_roles" in line
    assert "say whether the asker or I lack" in line
    assert "Asker not in voice" in line


def test_line_is_short_when_both_are_admin_and_shows_voice():
    vc = SimpleNamespace(name="Lobby", id=77)
    guild, message = _setup(
        user=_perms(administrator=True),
        bot=_perms(administrator=True),
        voice=SimpleNamespace(channel=vc),
    )
    line = _permission_context_line(guild, message)
    assert "available=all (both administrator)" in line
    assert "Tools for unavailable actions" not in line
    assert "Asker in voice #Lobby (77)" in line
    assert len(line) < 200


def test_line_uses_channel_overwrites_not_guild_perms():
    guild, message = _setup(
        user=_perms(manage_channels=False), bot=_perms(manage_channels=True)
    )
    # Guild-wide bits must not leak into the channel-effective summary.
    message.author.guild_permissions = _perms(manage_channels=True)
    line = _permission_context_line(guild, message)
    assert "asker lacks=manage_channels" in line


def test_line_ignores_member_object_from_another_server():
    guild, message = _setup(bot=_perms(manage_channels=True))
    other = SimpleNamespace(id=99, name="Elsewhere")
    message.author = SimpleNamespace(
        id=2, name="ana", display_name="Ana", guild=other, voice=None
    )
    guild.get_member = lambda _uid: None
    line = _permission_context_line(guild, message)
    assert "asker perms unreadable" in line
    assert "Elsewhere" not in line
    assert "voice" not in line


def test_line_empty_without_guild():
    assert _permission_context_line(None, SimpleNamespace()) == ""


def test_refusal_names_missing_user_permission():
    guild, message = _setup(user=_perms(), bot=_perms(manage_channels=True))
    text = _mod_tool_refusal(guild, message, "create_channel", {})
    assert "Ana lacks manage_channels in #general in Villa" in text
    assert "Do not retry" in text


def test_refusal_names_missing_bot_permission():
    guild, message = _setup(user=_perms(move_members=True), bot=_perms())
    text = _mod_tool_refusal(guild, message, "voice_mod", {})
    assert "I (the bot) lack" in text
    assert "move_members" in text


def test_refusal_when_neither_side_has_it():
    guild, message = _setup()
    text = _mod_tool_refusal(guild, message, "manage_role", {})
    assert "neither Ana nor I have manage_roles" in text


def test_refusal_matches_allowed_set_and_never_widens_it():
    guild, message = _setup(
        user=_perms(manage_channels=True, kick_members=True),
        bot=_perms(manage_channels=True, ban_members=True),
    )
    allowed = _mod_tools_allowed(guild, message)
    for tool in _GATED_MOD_TOOLS:
        refused = bool(_mod_tool_refusal(guild, message, tool, {}))
        assert refused == (tool not in allowed), tool


def test_refusal_leaves_ungated_and_cross_server_calls_to_the_tool():
    guild, message = _setup()
    assert _mod_tool_refusal(guild, message, "delete_message", {}) == ""
    assert _mod_tool_refusal(guild, message, "create_invite", {}) == ""
    assert _mod_tool_refusal(guild, message, "web_search", {}) == ""
    assert _mod_tool_refusal(guild, message, "create_channel", {"guild_id": "11"}) == ""
    assert _mod_tool_refusal(guild, message, "create_channel", {"guild_id": "10"})


def test_dispatch_returns_refusal_without_running_tool(monkeypatch):
    calls = []

    class Tool:
        is_destructive = False

        async def execute(self, message, **_params):
            calls.append(message)
            return "moved"

    class Breaker:
        @staticmethod
        def is_open(_name):
            return False

        @staticmethod
        def record_failure(_name):
            pass

        @staticmethod
        def record_success(_name):
            pass

    async def record_reasoning(*_args, **_kwargs):
        return None

    monkeypatch.setattr(bot_module, "record_reasoning", record_reasoning)
    guild, message = _setup(user=_perms(), bot=_perms(move_members=True))
    bot = SimpleNamespace(
        tools={"voice_mod": Tool()},
        plugin_manager=None,
        _tool_breaker=Breaker(),
        hooks=None,
    )
    result = asyncio.run(
        MaxwellBot._execute_tool_by_name(
            bot,
            message,
            "voice_mod",
            {"action": "move", "user": "3", "channel": "77"},
            disabled=set(),
            compatible={"voice_mod"},
        )
    )
    assert "Ana lacks" in result and "move_members" in result
    assert calls == []
