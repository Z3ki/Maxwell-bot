"""Gated Discord Custom Install Link."""

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import aiohttp
import pytest
from aiohttp import web

import api.api_server as api
import api.auth as auth


def _query(**kwargs):
    return SimpleNamespace(get=lambda key, default=None: kwargs.get(key, default))


def test_oauth_next_path_whitelist():
    assert api._oauth_next_path("/install") == "/install/"
    assert api._oauth_next_path("/install/") == "/install/"
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


def test_bot_install_authorize_url(monkeypatch):
    monkeypatch.setattr(api, "DISCORD_CLIENT_ID", "1472755214623703190")
    monkeypatch.setenv("DISCORD_BOT_PERMISSIONS", "2048")
    url = api._bot_install_authorize_url(guild_id="123456789012345678")
    parsed = urlsplit(url)
    assert parsed.scheme == "https"
    assert parsed.netloc == "discord.com"
    assert parsed.path == "/oauth2/authorize"
    q = parse_qs(parsed.query)
    assert q["client_id"] == ["1472755214623703190"]
    assert q["permissions"] == ["2048"]
    assert q["integration_type"] == ["0"]
    assert q["guild_id"] == ["123456789012345678"]
    assert "bot" in q["scope"][0]


def test_bot_install_strips_administrator(monkeypatch):
    monkeypatch.setattr(api, "DISCORD_CLIENT_ID", "1472755214623703190")
    monkeypatch.setenv("DISCORD_BOT_PERMISSIONS", "8")
    url = api._bot_install_authorize_url()
    q = parse_qs(urlsplit(url).query)
    assert q["permissions"] == ["0"]


def test_bot_install_default_is_normal_add(monkeypatch):
    monkeypatch.setattr(api, "DISCORD_CLIENT_ID", "1472755214623703190")
    monkeypatch.delenv("DISCORD_BOT_PERMISSIONS", raising=False)
    url = api._bot_install_authorize_url()
    q = parse_qs(urlsplit(url).query)
    assert q["permissions"] == ["0"]


def test_install_authorize_requires_admin(monkeypatch):
    monkeypatch.setattr(api, "DISCORD_CLIENT_ID", "1472755214623703190")
    monkeypatch.setattr(auth, "_discord_token_authed", lambda request: False)
    monkeypatch.setattr(api, "_has_admin_auth", lambda request: False)
    request = SimpleNamespace(query=_query(context="guild"), headers={})
    resp = asyncio.run(api.install_authorize(request))
    assert resp.status == 401


def test_install_authorize_returns_url_for_admin(monkeypatch):
    monkeypatch.setattr(api, "DISCORD_CLIENT_ID", "1472755214623703190")
    monkeypatch.setattr(api, "_has_admin_auth", lambda request: True)
    request = SimpleNamespace(
        query=_query(context="guild", guild_id="123456789012345678"),
        headers={"X-Discord-Token": "session"},
    )
    resp = asyncio.run(api.install_authorize(request))
    assert resp.status == 200
    data = json.loads(resp.body)
    assert data["ok"] is True
    assert data["authorize_url"].startswith("https://discord.com/oauth2/authorize?")
    assert "123456789012345678" in data["authorize_url"]
    assert data["context"] == "guild"
    assert parse_qs(urlsplit(data["authorize_url"]).query)["integration_type"] == ["0"]


def test_install_authorize_rejects_user_install(monkeypatch):
    monkeypatch.setattr(api, "DISCORD_CLIENT_ID", "1472755214623703190")
    monkeypatch.setattr(api, "_has_admin_auth", lambda request: True)
    request = SimpleNamespace(
        query=_query(context="user"),
        headers={"X-Discord-Token": "session"},
    )
    resp = asyncio.run(api.install_authorize(request))
    assert resp.status == 400
    data = json.loads(resp.body)
    assert "server" in data["error"]


def test_oauth_state_stores_install_next():
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
        assert api._oauth_state_payload(entry)["next"] == "/install/"
        assert api._oauth_state_payload(entry)["guild_id"] == "123456789012345678"

    asyncio.run(run())


def test_oauth_callback_redirects_install_admins(monkeypatch, tmp_path):
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
        assert location.startswith(
            "https://admin.example.test/install/?guild_id=123456789012345678#discord_token="
        )

    asyncio.run(run())


def test_oauth_callback_denies_non_admin_to_install(monkeypatch):
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
        assert raised.value.location.endswith("/install/#error=unauthorized")

    asyncio.run(run())


def test_install_page_exists():
    html = (
        Path(__file__).resolve().parents[1] / "web/install/index.html"
    ).read_text(encoding="utf-8")
    assert "/api/install/authorize" in html
    assert "next=/install/" in html or 'next: "/install/"' in html
    assert "Only Maxwell admins" in html
    assert "Add to my apps" not in html
    assert 'context: "guild"' in html or "context: 'guild'" in html
