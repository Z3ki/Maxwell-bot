"""Per-server nickname is injected into the prompt each turn.

Maxwell's core personality still says he is Maxwell, but the live Discord
guild nick (or account name in DMs) must appear in the per-turn context so
he knows what people in that room actually call him. The nick is read from
the guild member object every call — not cached on the bot.
"""

import asyncio
from types import SimpleNamespace

from bot import (
    MaxwellBot,
    ToolCircuitBreaker,
    _live_account_name,
    _live_self_identity_line,
    _live_self_name,
)


class FakeMemory:
    def __init__(self, messages=None):
        self.messages = list(messages or [])

    async def get_channel_memory(self, channel_id):
        return list(self.messages)

    def get_server_prompt(self, server_id):
        return None


def _bot(memory=None):
    bot = SimpleNamespace(
        _tool_breaker=ToolCircuitBreaker(failure_threshold=999, recovery_seconds=0),
        _control={
            "base_personality": "test",
            "cross_context_enabled": False,
            "emoji_context_enabled": False,
            "long_term_memory_enabled": False,
            "memory_context_budget": 30000,
            "memory_history_messages": 20,
            "music_context_enabled": False,
            "tools_enabled": False,
            "vc_response_mode": "always",
            "vc_wake_words": ["maxwell"],
        },
        _drugged_until={},
        _guild_emojis={},
        _recent_users={},
        _conversation_watch={},
        _tool_system_prompt=lambda *args, **kwargs: "",
        bot_name="Maxwell",
        memory=memory or FakeMemory(),
        user=SimpleNamespace(display_name="Maxwell", name="maxwell", id=1),
    )
    bot._reply_parent = MaxwellBot._reply_parent.__get__(bot)
    bot._replying_to_own_message = MaxwellBot._replying_to_own_message.__get__(bot)
    bot._render_reply_parent = MaxwellBot._render_reply_parent.__get__(bot)
    bot._author_is_self = MaxwellBot._author_is_self.__get__(bot)
    bot._iter_resolved_reply_chain = MaxwellBot._iter_resolved_reply_chain.__get__(bot)
    bot._reply_parent_context_lines = MaxwellBot._reply_parent_context_lines.__get__(
        bot
    )
    bot._directly_addressed = MaxwellBot._directly_addressed.__get__(bot)
    bot._conversation_watch_active = MaxwellBot._conversation_watch_active.__get__(bot)
    bot._is_short_live_turn = MaxwellBot._is_short_live_turn.__get__(bot)
    bot._get_personality = lambda: "test"
    bot._jailbreak_enabled = lambda gid: False
    return bot


def _message(*, guild=None):
    return SimpleNamespace(
        author=SimpleNamespace(bot=False, display_name="alice", id=456),
        channel=SimpleNamespace(id=123, name="general"),
        guild=guild,
        id=789,
        mentions=[],
        reference=None,
    )


def _guild(*, nick=None, display_name="Maxwell", me=True):
    member = SimpleNamespace(
        nick=nick, display_name=display_name, name="maxwell", id=1
    )
    if me:
        return SimpleNamespace(id=99, name="Cool Guild", me=member)
    return SimpleNamespace(
        id=99,
        name="Cool Guild",
        me=None,
        get_member=lambda uid: member if int(uid) == 1 else None,
    )


def _volatile(messages):
    return "\n".join(
        m["content"] for m in messages if m["role"] == "system" and m is not messages[0]
    )


def test_live_self_name_prefers_guild_nick():
    user = SimpleNamespace(display_name="Maxwell", name="maxwell", id=1)
    guild = _guild(nick="Sparky", display_name="Sparky")
    assert _live_self_name(user, guild, "Maxwell") == ("Sparky", "nick")
    line = _live_self_identity_line(user, guild, "Maxwell")
    assert "Your name here: Sparky" in line
    assert "server nickname" in line
    assert "Account name: Maxwell" in line


def test_live_self_name_falls_back_to_account_when_no_nick():
    user = SimpleNamespace(display_name="Maxwell", name="maxwell", id=1)
    guild = _guild(nick=None, display_name="Maxwell")
    assert _live_self_name(user, guild, "Maxwell") == ("Maxwell", "account")
    line = _live_self_identity_line(user, guild, "Maxwell")
    assert "Your name here: Maxwell" in line
    assert "no server nickname" in line


def test_live_self_name_treats_blank_nick_as_missing():
    user = SimpleNamespace(display_name="Maxwell", name="maxwell", id=1)
    guild = _guild(nick="   ", display_name="Maxwell")
    assert _live_self_name(user, guild, "Maxwell") == ("Maxwell", "account")


def test_live_self_name_uses_get_member_when_me_missing():
    user = SimpleNamespace(display_name="Maxwell", name="maxwell", id=1)
    guild = _guild(nick="Sparky", display_name="Sparky", me=False)
    assert _live_self_name(user, guild, "Maxwell") == ("Sparky", "nick")


def test_live_self_name_dm_uses_account_name():
    user = SimpleNamespace(display_name="Maxwell", name="maxwell", id=1)
    assert _live_self_name(user, None, "Bot") == ("Maxwell", "account")
    line = _live_self_identity_line(user, None, "Bot")
    assert "Your name here: Maxwell" in line
    assert "no server nickname" in line
    assert _live_account_name(None, "Maxwell") == "Maxwell"


def test_build_messages_injects_guild_nick_into_dynamic_context():
    bot = _bot()
    message = _message(guild=_guild(nick="Sparky", display_name="Sparky"))

    async def run():
        return await MaxwellBot._build_messages(bot, message, "latest")

    messages = asyncio.run(run())
    static = messages[0]["content"]
    volatile = _volatile(messages)
    assert "Your name here: Sparky" in volatile
    assert "server nickname in Cool Guild" in volatile
    assert "Your Discord access in Cool Guild" in volatile
    assert "Your name here: Sparky" not in static
    # Core identity is still Maxwell; the nick is the server-facing name.
    assert "You are Maxwell" in static


def test_build_messages_guild_without_nick_still_says_maxwell():
    bot = _bot()
    message = _message(guild=_guild(nick=None, display_name="Maxwell"))

    async def run():
        return await MaxwellBot._build_messages(bot, message, "hello")

    messages = asyncio.run(run())
    volatile = _volatile(messages)
    assert "Your name here: Maxwell" in volatile
    assert "no server nickname" in volatile


def test_build_messages_dm_uses_account_name():
    bot = _bot()
    message = _message(guild=None)

    async def run():
        return await MaxwellBot._build_messages(bot, message, "hey")

    messages = asyncio.run(run())
    volatile = _volatile(messages)
    assert "Your name here: Maxwell" in volatile
    assert "no server nickname" in volatile
    assert "Your Discord access" not in volatile


def test_build_messages_author_without_bot_flag():
    bot = _bot()
    message = _message()
    del message.author.bot

    async def run():
        return await MaxwellBot._build_messages(bot, message, "hey")

    messages = asyncio.run(run())
    assert messages


def test_build_messages_reads_live_nick_each_turn():
    """A mid-session nick change must show up on the next _build_messages call."""
    me = SimpleNamespace(nick="Sparky", display_name="Sparky", name="maxwell", id=1)
    guild = SimpleNamespace(id=99, name="Cool Guild", me=me)
    bot = _bot()
    message = _message(guild=guild)

    async def run():
        return await MaxwellBot._build_messages(bot, message, "latest")

    first = _volatile(asyncio.run(run()))
    assert "Your name here: Sparky" in first
    me.nick = "Zap"
    me.display_name = "Zap"
    second = _volatile(asyncio.run(run()))
    assert "Your name here: Zap" in second
    assert "Sparky" not in second


def test_different_guilds_get_different_names():
    user = SimpleNamespace(display_name="Maxwell", name="maxwell", id=1)
    a = _live_self_identity_line(
        user, _guild(nick="Sparky", display_name="Sparky"), "Maxwell"
    )
    other = SimpleNamespace(
        id=100,
        name="Other Guild",
        me=SimpleNamespace(nick=None, display_name="Maxwell", name="maxwell", id=1),
    )
    b = _live_self_identity_line(user, other, "Maxwell")
    assert "Sparky" in a and "Cool Guild" in a
    assert "Sparky" not in b
    assert "Other Guild" in b


def test_vc_prompt_includes_guild_nick():
    bot = _bot()
    guild = _guild(nick="Sparky", display_name="Sparky")
    user = SimpleNamespace(display_name="alice")
    prompt = MaxwellBot._vc_build_system_prompt(bot, user, guild, [])
    assert "Your name here: Sparky" in prompt
    assert "server nickname" in prompt


def test_vc_addressed_mode_wakes_on_server_nick():
    bot = _bot()
    bot._control["vc_response_mode"] = "addressed"
    guild = _guild(nick="Sparky", display_name="Sparky")
    user = SimpleNamespace(display_name="alice")
    prompt = MaxwellBot._vc_build_system_prompt(bot, user, guild, [])
    assert "talking to you (Sparky)" in prompt
    assert "Sparky" in prompt
    # Stored wake-word list is not mutated.
    assert bot._control["vc_wake_words"] == ["maxwell"]
