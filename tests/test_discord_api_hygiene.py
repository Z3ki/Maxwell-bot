"""Don't spam Discord REST for presence, lookups, or history walks."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from autonomy import AutonomyEngine
from bot import MaxwellBot
from bot_tools import LookupUserTool, SearchMessagesTool


def test_change_presence_does_not_write_user_settings_by_default():
    bot = MaxwellBot.__new__(MaxwellBot)
    bot._sleep_presence_overlay = False
    bot._sleep_until = 0.0
    seen = {}

    async def push(**kwargs):
        seen.update(kwargs)

    bot._push_presence = push
    bot._current_status = None

    asyncio.run(bot.change_presence(status=None))
    assert "edit_settings" not in seen
    assert seen.get("status") is None


def test_autonomy_history_defaults_stay_small():
    engine = SimpleNamespace(bot=SimpleNamespace(_control={}))
    engine._activity_channel_limit = AutonomyEngine._activity_channel_limit.__get__(
        engine
    )
    engine._activity_history_limit = AutonomyEngine._activity_history_limit.__get__(
        engine
    )
    assert engine._activity_channel_limit() == 6
    assert engine._activity_history_limit() == 12
    engine.bot._control = {
        "autonomy_activity_channels": 99,
        "autonomy_activity_messages": 500,
    }
    assert engine._activity_channel_limit() == 12
    assert engine._activity_history_limit() == 25


def test_search_messages_only_reads_this_channel():
    class Hist:
        def __init__(self):
            self.calls = []

        def history(self, limit=None):
            self.calls.append(limit)

            async def gen():
                msg = SimpleNamespace(
                    id=1,
                    content="hello cats",
                    author=SimpleNamespace(display_name="Ada"),
                )
                yield msg

            return gen()

    other = Hist()
    chan = Hist()
    chan.name = "general"
    guild = SimpleNamespace(text_channels=[chan, other], me=SimpleNamespace())
    other.permissions_for = lambda _me: SimpleNamespace(read_messages=True)
    msg = SimpleNamespace(channel=chan, guild=guild)
    out = asyncio.run(SearchMessagesTool(SimpleNamespace()).execute(msg, query="cats"))
    assert "hello cats" in out
    assert chan.calls == [20]
    assert other.calls == []


def test_lookup_user_uses_cache_and_skips_profile():
    user = SimpleNamespace(
        id=123456789,
        name="testuser",
        display_name="Test User",
        bot=False,
        created_at=SimpleNamespace(strftime=lambda _fmt: "2024-01-01"),
        display_avatar=SimpleNamespace(url="https://cdn.example/a.png"),
        bio="hi",
        banner=None,
        accent_color=None,
    )
    bot = SimpleNamespace(
        get_user=lambda uid: user if int(uid) == 123456789 else None,
        fetch_user=AsyncMock(side_effect=AssertionError("must not REST")),
        fetch_user_profile=AsyncMock(side_effect=AssertionError("no profile")),
        guilds=[],
    )
    tool = LookupUserTool(bot)
    msg = SimpleNamespace(guild=None)
    out = asyncio.run(tool.execute(msg, user_id="123456789"))
    assert "Test User" in out
    assert "Bio: hi" in out
    bot.fetch_user.assert_not_called()
    bot.fetch_user_profile.assert_not_called()


def test_user_label_does_not_fetch():
    bot = SimpleNamespace(
        _recent_users={},
        get_user=lambda _uid: None,
        fetch_user=AsyncMock(side_effect=AssertionError("must not REST")),
    )
    bot._cached_user_display_name = MaxwellBot._cached_user_display_name.__get__(bot)
    bot._user_label = MaxwellBot._user_label.__get__(bot)

    async def run():
        assert await MaxwellBot._user_label(bot, "123") == "123"
        bot.fetch_user.assert_not_called()

    asyncio.run(run())
