"""Official Discord bot transport helpers."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from urllib.parse import parse_qs, urlparse

from discord_account import (
    account_ids,
    application_client_id,
    bot_oauth_install_urls,
    configured_bot_token,
)
from bot import LEAN_TOOL_PROTOCOL, MaxwellBot, TOOL_PROTOCOL


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
    assert removed["global"] == 0
    assert ("PUT", "https://discord.com/api/v10/applications/99/commands", []) in calls


def test_sync_application_commands_puts_payload(monkeypatch):
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
            return FakeResp(200, {"id": "99"} if url.endswith("/@me") else [])

        def put(self, url, **kwargs):
            calls.append(("PUT", url, kwargs.get("json")))
            return FakeResp(200, kwargs.get("json") or [])

    import discord_account as mod
    import aiohttp
    from user_install import USER_INSTALL_COMMANDS

    monkeypatch.setattr(aiohttp, "ClientSession", FakeSession)
    synced = asyncio.run(
        mod.sync_application_commands("tok", USER_INSTALL_COMMANDS)
    )
    assert synced["global"] == len(USER_INSTALL_COMMANDS)
    assert (
        "PUT",
        "https://discord.com/api/v10/applications/99/commands",
        USER_INSTALL_COMMANDS,
    ) in calls


def test_user_install_integration_config_adds_user_context():
    from discord_account import user_install_integration_config

    out = user_install_integration_config({"0": {}})
    assert "0" in out
    assert out["1"]["oauth2_install_params"]["scopes"] == ["applications.commands"]
    already = {
        "0": {},
        "1": {"oauth2_install_params": {"scopes": ["applications.commands"]}},
    }
    assert user_install_integration_config(already)["1"] is already["1"]


def test_ensure_user_install_context_patches_when_missing(monkeypatch):
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
            return FakeResp(200, {"id": "99", "integration_types_config": {"0": {}}})

        def patch(self, url, **kwargs):
            calls.append(("PATCH", url, kwargs.get("json")))
            return FakeResp(200, kwargs.get("json") or {})

    import discord_account as mod
    import aiohttp

    monkeypatch.setattr(aiohttp, "ClientSession", FakeSession)
    assert asyncio.run(mod.ensure_user_install_context("tok")) is True
    patch = [c for c in calls if c[0] == "PATCH"]
    assert patch
    body = patch[0][2]["integration_types_config"]
    assert "1" in body
    assert "applications.commands" in body["1"]["oauth2_install_params"]["scopes"]


def test_ensure_user_install_context_skips_patch_when_present(monkeypatch):
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
            return FakeResp(
                200,
                {
                    "integration_types_config": {
                        "0": {},
                        "1": {
                            "oauth2_install_params": {
                                "scopes": ["applications.commands"],
                                "permissions": "0",
                            }
                        },
                    }
                },
            )

        def patch(self, url, **kwargs):
            calls.append(("PATCH", url, kwargs.get("json")))
            return FakeResp(200, {})

    import discord_account as mod
    import aiohttp

    monkeypatch.setattr(aiohttp, "ClientSession", FakeSession)
    assert asyncio.run(mod.ensure_user_install_context("tok")) is True
    assert not any(c[0] == "PATCH" for c in calls)


def test_configured_bot_token_prefers_bot_token():
    assert configured_bot_token("bot-tok", "legacy") == "bot-tok"
    assert configured_bot_token("", "legacy") == "legacy"
    assert configured_bot_token("Bot abc", "") == "abc"
    assert configured_bot_token("", "") == ""


def test_account_ids_include_bot_user():
    bot = SimpleNamespace(
        user=SimpleNamespace(id=111),
        _account_ids={111},
    )
    assert account_ids(bot) == {111}


def test_author_is_self_uses_bot_id():
    bot = SimpleNamespace(
        user=SimpleNamespace(id=111),
        _account_ids={111},
        _peer=None,
    )
    bot._self_ids = MaxwellBot._self_ids.__get__(bot)
    bot._author_is_self = MaxwellBot._author_is_self.__get__(bot)
    assert bot._author_is_self(SimpleNamespace(author=SimpleNamespace(id=111)))
    assert not bot._author_is_self(SimpleNamespace(author=SimpleNamespace(id=333)))


def test_protocol_does_not_offer_selfbot_join():
    text = TOOL_PROTOCOL.lower()
    assert "join_server" not in text
    assert "bot_invite_url" in text
    assert "bot_invite_url" in LEAN_TOOL_PROTOCOL.lower()


def test_bot_oauth_install_urls_app_and_server():
    urls = bot_oauth_install_urls("123456789012345678")
    assert set(urls) == {"app", "server"}
    app = parse_qs(urlparse(urls["app"]).query)
    assert app["client_id"] == ["123456789012345678"]
    assert app["integration_type"] == ["1"]
    assert app["scope"] == ["applications.commands"]
    server = parse_qs(urlparse(urls["server"]).query)
    assert server["integration_type"] == ["0"]
    assert "bot" in server["scope"][0]
    assert "applications.commands" in server["scope"][0]
    assert "permissions" not in server

    assert set(bot_oauth_install_urls("123456789012345678", kind="app")) == {
        "app"
    }
    q = parse_qs(
        urlparse(
            bot_oauth_install_urls(
                "123456789012345678",
                kind="server",
                permissions="8",
                guild_id="99",
            )["server"]
        ).query
    )
    assert q["permissions"] == ["8"]
    assert q["guild_id"] == ["99"]
    assert q["disable_guild_select"] == ["true"]


def test_application_client_id_prefers_bot_application_id(monkeypatch):
    monkeypatch.delenv("DISCORD_CLIENT_ID", raising=False)
    bot = SimpleNamespace(
        application_id=111,
        user=SimpleNamespace(id=222),
        config=SimpleNamespace(DISCORD_CLIENT_ID="333"),
    )
    assert application_client_id(bot) == "111"
    bot.application_id = None
    assert application_client_id(bot) == "222"
    bot.user = None
    assert application_client_id(bot) == "333"
    bot.config = None
    assert application_client_id(bot) == ""


def test_bot_invite_url_tool_returns_oauth_links(monkeypatch):
    from plugins.discord_messages.impl import BotInviteUrlTool

    monkeypatch.delenv("DISCORD_CLIENT_ID", raising=False)
    bot = SimpleNamespace(
        application_id=1472755214623703190,
        user=None,
        config=SimpleNamespace(DISCORD_CLIENT_ID=""),
    )
    out = asyncio.run(
        BotInviteUrlTool(bot).execute(SimpleNamespace(), kind="both")
    )
    assert "Add as app" in out
    assert "Add to a server" in out
    assert "1472755214623703190" in out
    assert "integration_type=1" in out
    assert "integration_type=0" in out

    missing = asyncio.run(
        BotInviteUrlTool(
            SimpleNamespace(application_id=None, user=None, config=None)
        ).execute(SimpleNamespace())
    )
    assert missing.startswith("Error:")
