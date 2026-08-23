"""`,blacklist` lists display names, not bare snowflakes."""

import asyncio
from types import SimpleNamespace

from bot import MaxwellBot


def _bot(*, cached=None, recent=None):
    bot = SimpleNamespace(
        _recent_users=recent or {},
        _users=cached or {},
    )

    def get_user(uid):
        return bot._users.get(int(uid))

    async def fetch_user(uid):
        return bot._users.get(int(uid))

    bot.get_user = get_user
    bot.fetch_user = fetch_user
    bot._cached_user_display_name = MaxwellBot._cached_user_display_name.__get__(bot)
    bot._user_label = MaxwellBot._user_label.__get__(bot)
    return bot


def test_user_label_prefers_guild_display_name():
    member = SimpleNamespace(display_name="Z3ki", name="zeki", id=147)
    guild = SimpleNamespace(get_member=lambda uid: member if int(uid) == 147 else None)
    bot = _bot()

    async def run():
        assert (
            await MaxwellBot._user_label(bot, "147", guild=guild)
            == "Z3ki (147)"
        )

    asyncio.run(run())


def test_user_label_falls_back_to_recent_rooms_then_id():
    bot = _bot(recent={"ch": {"99": "Alice"}})

    async def run():
        assert await MaxwellBot._user_label(bot, "99") == "Alice (99)"
        assert await MaxwellBot._user_label(bot, "123") == "123"

    asyncio.run(run())
