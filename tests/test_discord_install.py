"""Discord dashboard OAuth. Bot add is Discord's own authorize URL."""

import asyncio
import time
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp import web

import api.api_server as api
import api.auth as auth


def _query(**kwargs):
    return SimpleNamespace(get=lambda key, default=None: kwargs.get(key, default))


def test_oauth_next_path_is_always_dashboard():
    assert api._oauth_next_path("/install") == "/admin/"
    assert api._oauth_next_path("/install/") == "/admin/"
    assert api._oauth_next_path("/admin") == "/admin/"
    assert api._oauth_next_path("/admin/") == "/admin/"
    assert api._oauth_next_path("https://evil.example/phish") == "/admin/"
    assert api._oauth_next_path("/api/auth/discord/callback") == "/admin/"
    assert api._oauth_next_path("") == "/admin/"


def test_discord_snowflake():
    assert api._discord_snowflake("1472755214623703190") == "1472755214623703190"
    assert api._discord_snowflake("nope") == ""
    assert api._discord_snowflake("123") == ""
    assert api._discord_snowflake("1472755214623703190/../x") == ""


def test_oauth_state_ignores_install_next():
    async def run():
        api._DISCORD_STATES.clear()
        request = SimpleNamespace(
            query=_query(next="/install/", guild_id="123456789012345678"),
            scheme="https",
            host="maxwell.example.test",
        )
        resp = await api.discord_auth_state(request)
        assert resp.status == 200
        assert len(api._DISCORD_STATES) == 1
        entry = next(iter(api._DISCORD_STATES.values()))
        assert api._oauth_state_payload(entry)["next"] == "/admin/"

    asyncio.run(run())


def test_oauth_callback_redirects_admins_to_dashboard(monkeypatch, tmp_path):
    async def run():
        api._DISCORD_STATES.clear()
        monkeypatch.setattr(api, "DISCORD_CLIENT_ID", "cid")
        monkeypatch.setattr(api, "DISCORD_CLIENT_SECRET", "secret")
        monkeypatch.setattr(auth, "DISCORD_ALLOWED_USER_IDS", set())
        monkeypatch.setenv("MAXWELL_OWNER_IDS", "")
        monkeypatch.setattr(api, "_load_bot_admins", lambda: {"42"})
        (tmp_path / "admins.json").write_text('["42"]')
        monkeypatch.setattr(auth, "_data_dir", lambda: tmp_path)
        state = "abc"
        api._DISCORD_STATES[state] = {
            "issued": time.time(),
            "next": "/install/",
            "guild_id": "123456789012345678",
        }

        class FakeResp:
            def __init__(self, status, payload):
                self.status = status
                self._payload = payload

            async def text(self):
                return ""

            async def json(self):
                return self._payload

        class FakeSess:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def post(self, url, data=None, headers=None):
                return FakeResp(200, {"access_token": "tok"})

            async def get(self, url, headers=None):
                return FakeResp(
                    200,
                    {
                        "id": "42",
                        "username": "admin",
                        "discriminator": "0",
                        "avatar": None,
                    },
                )

        monkeypatch.setattr(aiohttp, "ClientSession", lambda: FakeSess())
        request = SimpleNamespace(
            query=_query(code="x", state=state),
            scheme="https",
            host="admin.example.test",
        )
        monkeypatch.setenv("DISCORD_REDIRECT_BASE", "https://admin.example.test")
        with pytest.raises(web.HTTPFound) as raised:
            await api.discord_auth_callback(request)
        location = raised.value.location
        assert location.startswith("https://admin.example.test/admin/#discord_token=")

    asyncio.run(run())


def test_oauth_callback_denies_non_admin(monkeypatch):
    async def run():
        api._DISCORD_STATES.clear()
        monkeypatch.setattr(api, "DISCORD_CLIENT_ID", "cid")
        monkeypatch.setattr(api, "DISCORD_CLIENT_SECRET", "secret")
        monkeypatch.setattr(api, "_load_bot_admins", lambda: {"99"})
        state = "nope"
        api._DISCORD_STATES[state] = {
            "issued": time.time(),
            "next": "/install/",
            "guild_id": "",
        }

        class FakeResp:
            def __init__(self, status, payload):
                self.status = status
                self._payload = payload

            async def text(self):
                return ""

            async def json(self):
                return self._payload

        class FakeSess:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def post(self, url, data=None, headers=None):
                return FakeResp(200, {"access_token": "tok"})

            async def get(self, url, headers=None):
                return FakeResp(
                    200,
                    {
                        "id": "1",
                        "username": "rando",
                        "discriminator": "0",
                        "avatar": None,
                    },
                )

        monkeypatch.setattr(aiohttp, "ClientSession", lambda: FakeSess())
        monkeypatch.setenv("DISCORD_REDIRECT_BASE", "https://admin.example.test")
        request = SimpleNamespace(
            query=_query(code="x", state=state),
            scheme="https",
            host="admin.example.test",
        )
        with pytest.raises(web.HTTPFound) as raised:
            await api.discord_auth_callback(request)
        assert raised.value.location.endswith("/admin/#error=unauthorized")

    asyncio.run(run())


def test_install_page_is_gone():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "web"
    assert not (root / "install" / "index.html").exists()
    home = (root / "index.html").read_text(encoding="utf-8")
    assert "/install" not in home
