"""Official Discord bot transport helpers."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from discord_account import (
    account_ids,
    configured_bot_token,
)
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
    assert synced["global"] == 1
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
    assert "bot_invite_url" in text or "invite" in text
