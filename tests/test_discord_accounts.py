"""User account + official bot account share one Maxwell brain."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from discord_account import (
    USER_ONLY_TOOLS,
    CompanionClient,
    account_ids,
    client_sees_channel,
    discord_bot_client,
    discord_user_client,
    pick_primary,
    resolve_accounts,
    user_only_unavailable,
    user_tools_enabled,
)
from bot_tools import JoinServerTool, ServerSetupTool
from bot import MaxwellBot, TOOL_PROTOCOL


def test_clear_application_commands_puts_empty_list(monkeypatch):
    calls = []

    class FakeResp:
        def __init__(self, status, payload):
            self.status = status
            self._payload = payload

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def json(self):
            return self._payload

        async def text(self):
            return ""

    class FakeSession:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def get(self, url, **kwargs):
            calls.append(("GET", url))
            if url.endswith("/oauth2/applications/@me"):
                return FakeResp(200, {"id": "99"})
            if url.endswith("/commands"):
                return FakeResp(200, [{"id": "1", "name": "help"}, {"id": "2", "name": "stop"}])
            return FakeResp(200, [])

        def put(self, url, **kwargs):
            calls.append(("PUT", url, kwargs.get("json")))
            return FakeResp(200, [])

    import discord_account as mod
    import aiohttp

    monkeypatch.setattr(aiohttp, "ClientSession", FakeSession)
    removed = asyncio.run(mod.clear_application_commands("tok"))
    assert removed["global"] == 2
    assert ("PUT", "https://discord.com/api/v10/applications/99/commands", []) in calls


def test_ensure_bot_http_token_prefixes_authorization():
    from discord_account import _ensure_bot_http_token

    http = SimpleNamespace(_bot_account=True, token="abc.def.ghi")
    _ensure_bot_http_token(http)
    assert http.token == "Bot abc.def.ghi"
    assert http._raw_token == "abc.def.ghi"
    _ensure_bot_http_token(http)
    assert http.token == "Bot abc.def.ghi"


def test_user_only_tools_are_the_invite_and_onboarding_ones():
    assert USER_ONLY_TOOLS == frozenset({"join_server", "server_setup"})


def test_user_tools_enabled_follows_planned_kinds():
    assert user_tools_enabled({"user"}) is True
    assert user_tools_enabled({"user", "bot"}) is True
    assert user_tools_enabled({"bot"}) is False
    assert user_tools_enabled(set()) is False


def test_discord_user_client_treats_test_doubles_as_user():
    fake = SimpleNamespace()
    assert discord_user_client(fake) is fake


def test_discord_user_client_none_when_bot_only():
    bot = SimpleNamespace(account_kind="bot", _peer=None)
    assert discord_user_client(bot) is None
    assert discord_bot_client(bot) is bot


def test_discord_user_client_finds_peer():
    user = SimpleNamespace(account_kind="user")
    bot = SimpleNamespace(account_kind="bot", _peer=user)
    assert discord_user_client(bot) is user
    assert discord_bot_client(bot) is bot


def test_account_ids_include_primary_and_peer():
    bot = SimpleNamespace(
        user=SimpleNamespace(id=111),
        _account_ids={111},
        _peer=SimpleNamespace(user=SimpleNamespace(id=222), account_kind="bot"),
    )
    assert account_ids(bot) == {111, 222}


def test_pick_primary_prefers_user():
    from discord_account import AccountLogin

    user = AccountLogin(kind="user", token="u", label="U")
    official = AccountLogin(kind="bot", token="b", label="B")
    primary, companion = pick_primary([official, user])
    assert primary is user
    assert companion is official
    primary, companion = pick_primary([official])
    assert primary is official
    assert companion is None


def test_resolve_accounts_runs_user_then_bot(monkeypatch):
    async def fake_probe(token, *, bot_account):
        if token == "user-tok" and not bot_account:
            return True, "max (1)"
        if token == "bot-tok" and bot_account:
            return True, "maxbot (2)"
        return False, "rejected (401)"

    monkeypatch.setattr("discord_account._login_probe", fake_probe)

    found = asyncio.run(resolve_accounts("user-tok", "bot-tok", "auto"))
    assert [(a.kind, a.label) for a in found] == [("user", "max (1)"), ("bot", "maxbot (2)")]


def test_resolve_accounts_detects_bot_token_in_user_field(monkeypatch):
    async def fake_probe(token, *, bot_account):
        if not bot_account:
            return False, "rejected (401)"
        if bot_account and token == "actually-bot":
            return True, "maxbot (9)"
        return False, "rejected (401)"

    monkeypatch.setattr("discord_account._login_probe", fake_probe)
    found = asyncio.run(resolve_accounts("actually-bot", "", "auto"))
    assert len(found) == 1
    assert found[0].kind == "bot"


def test_resolve_accounts_mode_bot_skips_user(monkeypatch):
    calls = []

    async def fake_probe(token, *, bot_account):
        calls.append((token, bot_account))
        return True, "ok"

    monkeypatch.setattr("discord_account._login_probe", fake_probe)
    found = asyncio.run(resolve_accounts("user-tok", "bot-tok", "bot"))
    assert all(bot_account for _, bot_account in calls)
    assert [a.kind for a in found] == ["bot"]


def test_join_server_refuses_bot_only_account():
    bot = SimpleNamespace(
        account_kind="bot",
        _peer=None,
        _is_admin=lambda _uid: True,
    )
    message = SimpleNamespace(author=SimpleNamespace(id=1))
    out = asyncio.run(
        JoinServerTool(bot).execute(message, invite="https://discord.gg/coolserver")
    )
    assert "user account" in out.lower()
    assert "join_server" in out


def test_server_setup_refuses_bot_only_account():
    bot = SimpleNamespace(account_kind="bot", _peer=None, guilds=[])
    message = SimpleNamespace(
        author=SimpleNamespace(id=1),
        guild=SimpleNamespace(id=1, name="x", me=None),
    )
    out = asyncio.run(ServerSetupTool(bot).execute(message))
    assert "user account" in out.lower()


def test_author_is_self_includes_peer_id():
    bot = SimpleNamespace(
        user=SimpleNamespace(id=111),
        _account_ids={111, 222},
        _peer=SimpleNamespace(user=SimpleNamespace(id=222)),
    )
    bot._self_ids = MaxwellBot._self_ids.__get__(bot)
    bot._author_is_self = MaxwellBot._author_is_self.__get__(bot)
    assert bot._author_is_self(SimpleNamespace(author=SimpleNamespace(id=111)))
    assert bot._author_is_self(SimpleNamespace(author=SimpleNamespace(id=222)))
    assert not bot._author_is_self(SimpleNamespace(author=SimpleNamespace(id=333)))


def test_directly_addressed_accepts_either_account_mention():
    bot = SimpleNamespace(
        user=SimpleNamespace(id=111),
        _account_ids={111, 222},
        _peer=SimpleNamespace(user=SimpleNamespace(id=222)),
    )
    bot._self_ids = MaxwellBot._self_ids.__get__(bot)
    bot._directly_addressed = MaxwellBot._directly_addressed.__get__(bot)
    ping_bot = SimpleNamespace(
        channel=SimpleNamespace(),
        mentions=[SimpleNamespace(id=222)],
        reference=None,
    )
    assert bot._directly_addressed(ping_bot) is True


def test_companion_skips_channels_the_primary_already_sees():
    primary = SimpleNamespace(
        user=SimpleNamespace(id=1),
        _account_ids={1, 2},
        on_message=AsyncMock(),
    )
    companion = SimpleNamespace(
        primary=primary,
        account_kind="bot",
        user=SimpleNamespace(id=2),
    )
    shared = SimpleNamespace(
        author=SimpleNamespace(id=99),
        channel=SimpleNamespace(id=50),
    )

    def sees(client, channel_id):
        return client is primary and int(channel_id) == 50

    import discord_account as mod

    orig = mod.client_sees_channel
    mod.client_sees_channel = sees
    try:
        asyncio.run(CompanionClient.on_message(companion, shared))
    finally:
        mod.client_sees_channel = orig
    primary.on_message.assert_not_called()


def test_companion_forwards_channels_only_it_can_see():
    primary = SimpleNamespace(
        user=SimpleNamespace(id=1),
        _account_ids={1, 2},
        on_message=AsyncMock(),
    )
    companion = SimpleNamespace(
        primary=primary,
        account_kind="bot",
        user=SimpleNamespace(id=2),
    )
    exclusive = SimpleNamespace(
        author=SimpleNamespace(id=99),
        channel=SimpleNamespace(id=77),
    )

    import discord_account as mod

    orig = mod.client_sees_channel

    def sees(client, channel_id):
        return False

    mod.client_sees_channel = sees
    try:
        asyncio.run(CompanionClient.on_message(companion, exclusive))
    finally:
        mod.client_sees_channel = orig
    primary.on_message.assert_awaited_once_with(exclusive)


def test_companion_ignores_the_other_account_as_author():
    primary = SimpleNamespace(
        user=SimpleNamespace(id=1),
        _account_ids={1, 2},
        on_message=AsyncMock(),
    )
    companion = SimpleNamespace(primary=primary, account_kind="bot")
    own = SimpleNamespace(
        author=SimpleNamespace(id=1),
        channel=SimpleNamespace(id=77),
    )
    asyncio.run(CompanionClient.on_message(companion, own))
    primary.on_message.assert_not_called()


def test_protocol_mentions_bot_account_limits():
    text = TOOL_PROTOCOL.lower()
    assert "official bot" in text
    assert "bot_invite_url" in text.lower() or "BOT_INVITE_URL" in TOOL_PROTOCOL


def test_user_only_unavailable_names_the_tool():
    text = user_only_unavailable("join_server")
    assert "join_server" in text
    assert "DISCORD_TOKEN" in text


def test_user_rest_gate_serializes_and_queues_global_cooldown():
    from discord_account import UserRestGate

    gate = UserRestGate(min_interval=0.0, max_inflight=1)
    order = []

    async def run():
        async def one(i):
            async with gate.slot():
                order.append(i)
                await asyncio.sleep(0.02)

        await asyncio.gather(one(1), one(2), one(3))
        gate.note_global(0.12)
        started = asyncio.get_running_loop().time()
        async with gate.slot():
            elapsed = asyncio.get_running_loop().time() - started
        assert elapsed >= 0.1

    asyncio.run(run())
    assert order == [1, 2, 3]


def test_user_account_request_retries_rate_limited(monkeypatch):
    from discord.errors import RateLimited
    from discord_account import UserRestGate
    import discord_account as mod

    gate = UserRestGate(min_interval=0.0, max_inflight=1)
    monkeypatch.setattr(mod, "_USER_REST", gate)
    monkeypatch.setattr(mod, "_USER_REST_429_RETRIES", 3)
    calls = {"n": 0}

    async def original(_http, _route, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RateLimited(0.01)
        return "ok"

    out = asyncio.run(mod.user_account_request(SimpleNamespace(), original, "route"))
    assert out == "ok"
    assert calls["n"] == 2


def test_user_account_request_does_not_retry_forbidden():
    from discord_account import user_account_request

    class Forbidden(Exception):
        status = 403

    async def original(_http, _route, **_kwargs):
        raise Forbidden("nope")

    async def run():
        try:
            await user_account_request(SimpleNamespace(), original, "route")
        except Forbidden:
            return "caught"
        return "missed"

    assert asyncio.run(run()) == "caught"


def test_client_sees_channel_false_without_client():
    assert client_sees_channel(None, 1) is False


def test_turn_tool_names_drops_user_only_without_user_account():
    bot = SimpleNamespace(
        account_kind="bot",
        _planned_kinds={"bot"},
        _peer=None,
        tools={
            "join_server": object(),
            "server_setup": object(),
            "send_message": object(),
            "react": object(),
        },
        _control={"disabled_tools": []},
        plugin_manager=None,
    )
    names = MaxwellBot._turn_tool_names(bot, "discord", None, "hello")
    assert "join_server" not in names
    assert "server_setup" not in names
    assert "send_message" in names
    assert "react" in names
