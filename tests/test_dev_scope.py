"""The Dev application never handles Discord traffic outside its test guild."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot import MaxwellBot
from discord_account import sync_application_commands


GUILD_ID = 1528838979976298616


def _bot():
    return SimpleNamespace(
        config=SimpleNamespace(
            MAXWELL_DEV_MODE=True, MAXWELL_DEV_GUILD_IDS=frozenset({str(GUILD_ID)})
        )
    )


def test_dev_scope_accepts_only_configured_guild():
    bot = _bot()
    assert MaxwellBot._dev_scope_allows(bot, SimpleNamespace(guild_id=GUILD_ID))
    assert MaxwellBot._dev_scope_allows(
        bot, SimpleNamespace(channel=SimpleNamespace(guild=SimpleNamespace(id=GUILD_ID)))
    )
    assert not MaxwellBot._dev_scope_allows(bot, SimpleNamespace(guild_id=42))
    assert not MaxwellBot._dev_scope_allows(bot, SimpleNamespace(guild=None))
    assert not MaxwellBot._dev_scope_allows(bot, None)


def test_dev_ingress_drops_other_guilds_and_dms_before_side_effects():
    bot = _bot()
    for guild_id in (None, 42):
        message = SimpleNamespace(
            id=1, guild=SimpleNamespace(id=guild_id) if guild_id else None
        )
        interaction = SimpleNamespace(guild_id=guild_id)
        assert asyncio.run(MaxwellBot.on_message(bot, message)) is None
        assert asyncio.run(MaxwellBot.on_interaction(bot, interaction)) is None
    assert not hasattr(bot, "_inbound_processing")


def test_dev_recovery_does_not_fetch_outside_guild():
    bot = _bot()
    bot._recovery_cursors = {"123": 1}
    bot._recovery_lock = asyncio.Lock()
    bot.get_channel = lambda _id: SimpleNamespace(guild=SimpleNamespace(id=42))
    bot._fetch_inbound_channel = AsyncMock()
    bot._watermarks = SimpleNamespace(save=lambda: None)
    bot._failed_receipt_floors = {}
    asyncio.run(MaxwellBot._recover_missed_messages(bot, settle=False))
    bot._fetch_inbound_channel.assert_not_awaited()
    assert bot._recovery_cursors == {}


def test_dev_slash_sync_is_guild_only(monkeypatch):
    calls = []

    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

    class Session:
        def __init__(self, **_):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        def put(self, url, **kwargs):
            calls.append((url, kwargs.get("json")))
            return Response()

        def get(self, *_args, **_kwargs):
            raise AssertionError("guild-only sync should not fetch global commands")

    import aiohttp

    monkeypatch.setattr(aiohttp, "ClientSession", Session)
    payload = [{"name": "maxwell", "description": "Dev bot"}]
    result = asyncio.run(
        sync_application_commands(
            "fake-token", payload, application_id=99, guild_ids=[GUILD_ID], guild_only=True
        )
    )
    assert result == {"global": 0, "guild": 1}
    assert calls == [
        ("https://discord.com/api/v10/applications/99/commands", []),
        (f"https://discord.com/api/v10/applications/99/guilds/{GUILD_ID}/commands", payload),
    ]
