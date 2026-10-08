"""Exercise dashboard HTTP boundaries with real sessions and settings stores."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlsplit

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from api.auth import _auth_middleware_unless_login
from api.dashboard import can_manage, register_dashboard
from api.dashboard_auth import DashboardConfig
from plugins.maxwell_extras.dashboard_settings import save_server_settings
from plugins.maxwell_extras.user_preferences import UserPreferenceStore

CONFIG = DashboardConfig(
    "http://localhost",
    "123",
    "test-oauth-secret",
    bytes.fromhex("ab" * 32),
    "test-bot-token",
)
SECRET = "test-personal-api-key-123456789"


@asynccontextmanager
async def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("MAXWELL_BYOK_ENCRYPTION_KEY", "cd" * 32)
    monkeypatch.setenv("MAXWELL_ADMIN_USER", "operator")
    monkeypatch.setenv("MAXWELL_ADMIN_PASSWORD", "operator-secret")
    app = web.Application(middlewares=[_auth_middleware_unless_login])
    dashboard = register_dashboard(app, lambda: tmp_path, CONFIG)

    async def operator(request):
        return web.json_response({"private": True})

    app.router.add_get("/api/control", operator)
    async with TestClient(TestServer(app), headers={"Host": "localhost"}) as client:
        cookie = dashboard.sessions.create(
            {"id": "100", "name": "Zeki"}, "discord-access-secret", 3600
        )
        session = dashboard.sessions.get(cookie)
        headers = {
            "Cookie": CONFIG.cookie + "=" + cookie,
            "Origin": CONFIG.origin,
            "X-CSRF-Token": session["csrf"],
        }
        yield dashboard, client, headers


@pytest.mark.parametrize(
    "guild,expected",
    [
        ({"owner": True}, True),
        ({"permissions": "8"}, True),
        ({"permissions": "32"}, True),
        ({"permissions": "1024"}, False),
        ({"permissions": "bad"}, False),
        ({}, False),
    ],
)
def test_server_access_uses_discord_owner_admin_or_manage_server(guild, expected):
    assert can_manage(guild) is expected


def test_auth_csrf_isolation_logout_and_shared_personal_settings(tmp_path, monkeypatch):
    async def run():
        async with setup(tmp_path, monkeypatch) as (dashboard, client, headers):
            assert (await client.get("/api/dashboard/personal")).status == 401
            response = await client.get("/api/dashboard/session", headers=headers)
            session = await response.json()
            assert session["user"] == {"id": "100", "name": "Zeki"}
            assert "discord-access-secret" not in json.dumps(session)
            payload = {
                "defaults": {
                    "language": "Spanish",
                    "visibility": "private",
                    "context": 1000,
                },
                "personality": "Keep it short",
            }
            for bad in [
                {"Cookie": headers["Cookie"]},
                {**headers, "Origin": "https://evil.example"},
                {**headers, "X-CSRF-Token": "wrong"},
            ]:
                assert (
                    await client.put(
                        "/api/dashboard/personal", json=payload, headers=bad
                    )
                ).status == 403
            response = await client.put(
                "/api/dashboard/personal", json=payload, headers=headers
            )
            assert response.status == 200
            assert response.headers["Cache-Control"] == "no-store"
            store = UserPreferenceStore(
                tmp_path / "plugins/maxwell_extras/user_preferences.json"
            )
            assert store.get("100")["defaults"]["visibility"] == "private"
            assert store.get("200")["defaults"]["language"] == ""
            store.set_default("100", "language", "English")
            response = await client.get("/api/dashboard/personal", headers=headers)
            assert (await response.json())["defaults"]["language"] == "English"
            assert (
                await client.put(
                    "/api/dashboard/personal",
                    json={**payload, "user_id": "200"},
                    headers=headers,
                )
            ).status == 400
            assert (await client.get("/api/control", headers=headers)).status == 401
            assert (
                await client.get(
                    "/api/dashboard/personal",
                    headers={**headers, "Host": "untrusted.example"},
                )
            ).status == 403
            assert (
                await client.post("/api/dashboard/logout", json={}, headers=headers)
            ).status == 200
            assert (
                await client.get("/api/dashboard/personal", headers=headers)
            ).status == 401
            assert b"discord-access-secret" not in dashboard.sessions.path.read_bytes()

    asyncio.run(run())


def test_oauth_is_browser_bound_single_use_and_rotates_session(tmp_path, monkeypatch):
    async def run():
        async with setup(tmp_path, monkeypatch) as (dashboard, client, headers):
            calls = []

            async def discord(path, *, token=None, form=None):
                calls.append(path)
                if form:
                    assert form["client_secret"] == CONFIG.client_secret
                    assert form["redirect_uri"] == CONFIG.callback
                    return {
                        "access_token": "new-discord-token",
                        "scope": "identify guilds",
                        "expires_in": 3600,
                    }
                return {"id": "100", "username": "Zeki"}

            dashboard.discord = discord
            response = await client.get("/api/dashboard/login", allow_redirects=False)
            assert response.status == 302
            query = parse_qs(urlsplit(response.headers["Location"]).query)
            assert query["scope"] == ["identify guilds"]
            assert "test-oauth-secret" not in response.headers["Location"]
            browser = response.cookies[CONFIG.flow_cookie].value
            client.session.cookie_jar.clear()
            assert response.cookies[CONFIG.flow_cookie]["httponly"]
            url = (
                "/api/dashboard/callback?code=temporary-code&state=" + query["state"][0]
            )
            # A stolen state without its initiating browser cookie cannot log in.
            response = await client.get(
                url,
                headers={"Cookie": CONFIG.flow_cookie + "=wrong"},
                allow_redirects=False,
            )
            assert response.headers["Location"] == "/dashboard/?login=cancelled"
            assert not calls
            cookie_headers = {
                "Cookie": headers["Cookie"] + "; " + CONFIG.flow_cookie + "=" + browser
            }
            response = await client.get(
                url, headers=cookie_headers, allow_redirects=False
            )
            assert response.headers["Location"] == "/dashboard/"
            new_cookie = response.cookies[CONFIG.cookie].value
            assert new_cookie not in {
                "new-discord-token",
                headers["Cookie"].split("=", 1)[1],
            }
            assert dashboard.sessions.get(headers["Cookie"].split("=", 1)[1]) is None
            assert dashboard.sessions.get(new_cookie)["user"]["id"] == "100"
            assert "new-discord-token" not in await response.text()
            response = await client.get(
                url, headers=cookie_headers, allow_redirects=False
            )
            assert response.headers["Location"] == "/dashboard/?login=cancelled"
            assert calls == ["/oauth2/token", "/users/@me"]

    asyncio.run(run())


def test_server_permissions_are_fresh_and_cross_server_channels_are_rejected(
    tmp_path, monkeypatch
):
    async def run():
        async with setup(tmp_path, monkeypatch) as (dashboard, client, headers):
            user_guilds = [
                {"id": "55", "name": "My server", "permissions": "32"},
                {"id": "66", "name": "Other", "permissions": "0"},
            ]
            installed = True

            async def discord(path, *, token=None, form=None):
                if path.startswith("/users/@me/guilds"):
                    return user_guilds if token else [{"id": "55"}]
                if not installed:
                    raise web.HTTPForbidden(text="Maxwell is not installed")
                if path.endswith("/channels"):
                    return [
                        {"id": "550", "name": "general", "type": 0},
                        {"id": "551", "name": "voice", "type": 2},
                    ]
                assert path == "/guilds/55"
                return {"id": "55"}

            dashboard.discord = discord
            response = await client.get("/api/dashboard/servers", headers=headers)
            assert (await response.json())["servers"] == [
                {"id": "55", "name": "My server", "installed": True}
            ]
            assert (
                await client.get("/api/dashboard/servers/66", headers=headers)
            ).status == 403
            assert (
                await client.put(
                    "/api/dashboard/servers/55",
                    json={"channel": "660"},
                    headers=headers,
                )
            ).status == 400
            save_server_settings(tmp_path, "66", {"channel": "660", "progress": "off"})
            response = await client.put(
                "/api/dashboard/servers/55",
                json={
                    "channel": "550",
                    "progress": "on",
                    "ticket": True,
                    "capabilities": [],
                },
                headers=headers,
            )
            assert response.status == 200
            saved = (await response.json())["settings"]
            assert saved["channel"] == "550" and saved["ticket"] is True
            control = json.loads((tmp_path / "bot_control.json").read_text())
            assert control["guild_solo_channel"] == {"55": "550", "66": "660"}
            user_guilds[0]["permissions"] = "0"
            assert (
                await client.get("/api/dashboard/servers/55", headers=headers)
            ).status == 403
            assert (
                await client.put(
                    "/api/dashboard/servers/55", json={"channel": ""}, headers=headers
                )
            ).status == 403
            user_guilds[0]["permissions"] = "8"
            installed = False
            assert (
                await client.get("/api/dashboard/servers/55", headers=headers)
            ).status == 403

    asyncio.run(run())


def test_connection_never_exposes_key_preserves_edits_and_blocks_wrong_owner(
    tmp_path, monkeypatch
):
    async def run():
        async with setup(tmp_path, monkeypatch) as (dashboard, client, headers):
            payload = {"provider": "openai", "model": "test-model", "api_key": SECRET}
            response = await client.put(
                "/api/dashboard/connection", json=payload, headers=headers
            )
            assert response.status == 200
            assert SECRET not in await response.text()
            assert dashboard.vault.get("100")["api_key"] == SECRET
            assert not dashboard.vault.has_credential("200")
            response = await client.put(
                "/api/dashboard/connection",
                json={"provider": "openai", "model": "new-model", "api_key": ""},
                headers=headers,
            )
            assert response.status == 200
            assert dashboard.vault.get("100")["api_key"] == SECRET
            assert (
                await client.put(
                    "/api/dashboard/connection",
                    json={"provider": "groq", "model": "model", "api_key": ""},
                    headers=headers,
                )
            ).status == 400
            assert dashboard.vault.get("100")["provider"] == "openai"
            assert (
                await client.put(
                    "/api/dashboard/connection",
                    json={**payload, "user_id": "200"},
                    headers=headers,
                )
            ).status == 400
            assert (
                await client.put(
                    "/api/dashboard/connection",
                    json={
                        **payload,
                        "provider": "custom",
                        "base_url": "https://127.0.0.1/v1",
                    },
                    headers=headers,
                )
            ).status == 400
            assert (
                await client.delete("/api/dashboard/connection", headers=headers)
            ).status == 200
            assert not dashboard.vault.has_credential("100")

    asyncio.run(run())


def test_corrupt_settings_and_invalid_inputs_are_not_overwritten(tmp_path, monkeypatch):
    async def run():
        async with setup(tmp_path, monkeypatch) as (_, client, headers):
            for payload in [
                {"defaults": {"context": -1}},
                {"defaults": {"visibility": "anything"}},
                {"personality": "a" * 801},
                {"defaults": {"language": []}},
                [],
            ]:
                assert (
                    await client.put(
                        "/api/dashboard/personal", json=payload, headers=headers
                    )
                ).status == 400
            assert (
                await client.put(
                    "/api/dashboard/personal",
                    data='{"bad":',
                    headers={**headers, "Content-Type": "application/json"},
                )
            ).status == 400
            assert (
                await client.put(
                    "/api/dashboard/personal",
                    json={"personality": "a" * 17000},
                    headers=headers,
                )
            ).status == 413
            path = tmp_path / "plugins/maxwell_extras/user_preferences.json"
            path.write_text('{"users":')
            assert (
                await client.put(
                    "/api/dashboard/personal",
                    json={"personality": "hello"},
                    headers=headers,
                )
            ).status == 400
            assert path.read_text() == '{"users":'

    asyncio.run(run())


def test_static_dashboard_and_safe_disabled_setup(tmp_path, monkeypatch):
    async def run():
        monkeypatch.delenv("MAXWELL_DASHBOARD_URL", raising=False)
        app = web.Application(middlewares=[_auth_middleware_unless_login])
        register_dashboard(app, lambda: tmp_path)
        async with TestClient(TestServer(app)) as client:
            response = await client.get("/dashboard/")
            assert response.status == 200
            assert "Continue with Discord" in await response.text()
            assert "unsafe-inline" not in response.headers["Content-Security-Policy"]
            assert (await client.get("/dashboard/dashboard.js")).status == 200
            assert (await client.get("/dashboard/unknown.js")).status == 404
            response = await client.get("/api/dashboard/session")
            assert await response.json() == {"configured": False, "user": None}
            assert (await client.get("/api/dashboard/login")).status == 503

    asyncio.run(run())


def test_production_config_requires_dedicated_host_and_secure_cookie(monkeypatch):
    for key, value in {
        "MAXWELL_DASHBOARD_URL": "https://dashboard.example.com",
        "MAXWELL_PUBLIC_BASE_URL": "https://sites.example.com",
        "MAXWELL_DASHBOARD_SECRET": "ab" * 32,
        "DISCORD_CLIENT_ID": "123",
        "DISCORD_CLIENT_SECRET": "secret",
        "DISCORD_BOT_TOKEN": "token",
    }.items():
        monkeypatch.setenv(key, value)
    config = DashboardConfig.from_env()
    assert config.secure and config.cookie.startswith("__Host-")
    for value in [
        "http://dashboard.example.com",
        "https://sites.example.com",
        "https://dashboard.example.com/dashboard/",
        "https://evil:password@dashboard.example.com",
    ]:
        monkeypatch.setenv("MAXWELL_DASHBOARD_URL", value)
        with pytest.raises(ValueError):
            DashboardConfig.from_env()


def test_control_writer_preserves_web_edits_from_another_guild(tmp_path):
    from plugins.maxwell_extras.dashboard_settings import merge_control_change

    path = tmp_path / "bot_control.json"
    save_server_settings(tmp_path, "55", {"capabilities": []})
    original = json.loads(path.read_text())["guild_disabled_capabilities"]
    save_server_settings(tmp_path, "66", {"capabilities": []})
    current = merge_control_change(
        path, "guild_disabled_capabilities", {}, original, {}
    )
    assert set(current["guild_disabled_capabilities"]) == {"66"}
    path.write_text('{"broken":')
    with pytest.raises(ValueError):
        save_server_settings(tmp_path, "55", {"channel": "550"})
    assert path.read_text() == '{"broken":'


def test_expired_state_and_session_are_rejected(tmp_path, monkeypatch):
    import api.dashboard_auth as auth

    sessions = auth.DashboardSessions(tmp_path / "sessions.sqlite3", CONFIG.key)
    state, browser = sessions.begin()
    cookie = sessions.create({"id": "100"}, "token", 10)
    now = auth.time.time()
    monkeypatch.setattr(auth.time, "time", lambda: now + 301)
    assert not sessions.consume(state, browser)
    assert sessions.get(cookie) is None


def _preference_writer(path, uid):
    store = UserPreferenceStore(path)
    for index in range(20):
        store.set_default(uid, "language", f"language-{index}")


def test_discord_and_web_preferences_survive_separate_process_writes(tmp_path):
    import multiprocessing

    ctx = multiprocessing.get_context("fork")
    path = tmp_path / "prefs.json"
    processes = [
        ctx.Process(target=_preference_writer, args=(path, uid))
        for uid in ("100", "200")
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join()
        assert process.exitcode == 0
    store = UserPreferenceStore(path)
    assert store.get("100")["defaults"]["language"] == "language-19"
    assert store.get("200")["defaults"]["language"] == "language-19"
