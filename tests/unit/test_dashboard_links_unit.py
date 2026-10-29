"""Link-mode dashboard: /dashboard links, bearer sessions, Premium panels."""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import user_install as ui
from api.auth import _auth_middleware_unless_login
from api.dashboard import register_dashboard
from api.dashboard_auth import DashboardConfig
from message_quota import MessageQuota
from plugins.web_dashboard.links import DashboardError, DashboardLinks

ROOT = Path(__file__).resolve().parents[2]
LINK = DashboardConfig(
    "http://localhost", "", "", bytes.fromhex("ab" * 32), "test-bot-token", mode="link"
)
UID = "100"


@asynccontextmanager
async def link_app(tmp_path, monkeypatch, *, plan=None):
    monkeypatch.setenv("MAXWELL_BYOK_ENCRYPTION_KEY", "cd" * 32)
    monkeypatch.setenv("MAXWELL_ADMIN_USER", "operator")
    monkeypatch.setenv("MAXWELL_ADMIN_PASSWORD", "operator-secret")
    monkeypatch.setenv("MAXWELL_PUBLIC_BASE_URL", "http://localhost")
    for key in (
        "MAXWELL_DEV_MODE",
        "MAXWELL_DEV_GUILD_IDS",
        "MAXWELL_STORAGE_PREMIUM_MB",
    ):
        monkeypatch.delenv(key, raising=False)
    if plan:
        MessageQuota(tmp_path / "message_quota.sqlite3").configure(UID, plan=plan)
    app = web.Application(middlewares=[_auth_middleware_unless_login])
    dashboard = register_dashboard(app, lambda: tmp_path, LINK)
    async with TestClient(TestServer(app), headers={"Host": "localhost"}) as client:
        yield dashboard, client


async def sign_in(dashboard, client, guilds=()):
    token = dashboard.sessions.mint_link({"id": UID, "name": "Zeki"}, list(guilds))
    response = await client.post(
        "/api/dashboard/redeem", json={"token": token}, headers={"Origin": LINK.origin}
    )
    assert response.status == 200
    body = await response.json()
    return {"Authorization": "Bearer " + body["token"], "Origin": LINK.origin}


def test_link_config_comes_from_the_public_base_url(tmp_path, monkeypatch):
    for key in (
        "MAXWELL_DASHBOARD_URL",
        "MAXWELL_DASHBOARD_SECRET",
        "MAXWELL_DASHBOARD_LINKS",
        "DISCORD_BOT_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DISCORD_TOKEN", "tok")
    monkeypatch.setenv("MAXWELL_PUBLIC_BASE_URL", "https://maxwell.example.com/dev/")
    cfg = DashboardConfig.from_env(tmp_path)
    assert (
        cfg.link_mode
        and cfg.origin == "https://maxwell.example.com"
        and cfg.base_path == "/dev"
    )
    assert cfg.url == "https://maxwell.example.com/dev/dashboard/"
    # The bot and the API are separate processes; both must land on one key.
    assert DashboardConfig.from_env(tmp_path).key == cfg.key
    assert (tmp_path / "dashboard" / "session.key").is_file()
    monkeypatch.setenv("MAXWELL_DASHBOARD_LINKS", "0")
    assert DashboardConfig.from_env(tmp_path) is None
    monkeypatch.delenv("MAXWELL_DASHBOARD_LINKS")
    monkeypatch.setenv("MAXWELL_PUBLIC_BASE_URL", "http://maxwell.example.com")
    assert DashboardConfig.from_env(tmp_path) is None


def test_link_is_single_use_and_the_session_is_bearer_only(tmp_path, monkeypatch):
    async def run():
        async with link_app(tmp_path, monkeypatch) as (dashboard, client):
            token = dashboard.sessions.mint_link(
                {"id": UID, "name": "Zeki"}, [{"id": "55", "name": "Guild"}]
            )
            # No Origin: rejected before the token is touched.
            assert (
                await client.post("/api/dashboard/redeem", json={"token": token})
            ).status == 403
            response = await client.post(
                "/api/dashboard/redeem",
                json={"token": token},
                headers={"Origin": LINK.origin},
            )
            assert response.status == 200
            assert "Set-Cookie" not in response.headers
            body = await response.json()
            assert body["user"] == {"id": UID, "name": "Zeki"}
            again = await client.post(
                "/api/dashboard/redeem",
                json={"token": token},
                headers={"Origin": LINK.origin},
            )
            assert again.status == 401
            assert "already used" in (await again.json())["error"]

            auth = {"Authorization": "Bearer " + body["token"]}
            assert (
                await client.get("/api/dashboard/personal", headers=auth)
            ).status == 200
            # A cookie carrying the same value is not a session in link mode.
            cookie = {"Cookie": LINK.cookie + "=" + body["token"]}
            assert (
                await client.get("/api/dashboard/personal", headers=cookie)
            ).status == 401
            session = await (
                await client.get("/api/dashboard/session", headers=auth)
            ).json()
            assert session["mode"] == "link" and session["user"]["id"] == UID
            servers = await (
                await client.get("/api/dashboard/servers", headers=auth)
            ).json()
            assert servers["servers"] == [
                {"id": "55", "name": "Guild", "installed": True}
            ]

            payload = {"defaults": {"detail": "deep"}, "personality": "dry"}
            assert (
                await client.put("/api/dashboard/personal", json=payload, headers=auth)
            ).status == 403
            saved = await client.put(
                "/api/dashboard/personal",
                json=payload,
                headers={**auth, "Origin": LINK.origin},
            )
            assert saved.status == 200
            assert (await saved.json())["defaults"]["detail"] == "deep"

            assert (
                await client.post(
                    "/api/dashboard/logout",
                    json={},
                    headers={**auth, "Origin": LINK.origin},
                )
            ).status == 200
            assert (
                await client.get("/api/dashboard/personal", headers=auth)
            ).status == 401
            assert (
                await client.post(
                    "/api/dashboard/redeem",
                    json={"token": "x" * 200},
                    headers={"Origin": LINK.origin},
                )
            ).status == 401

    asyncio.run(run())


def test_link_mode_server_access_is_checked_live(tmp_path, monkeypatch):
    member_roles = ["7"]

    async def discord(path, *, token=None, form=None):
        assert token is None  # bot token only; link sessions carry no OAuth token
        if path == "/guilds/55":
            return {
                "id": "55",
                "name": "Guild",
                "owner_id": "1",
                "roles": [
                    {"id": "55", "permissions": "0"},
                    {"id": "7", "permissions": "32"},
                ],
            }
        if path == "/guilds/55/members/" + UID:
            return {"roles": list(member_roles)}
        if path == "/guilds/55/channels":
            return [{"id": "9", "name": "general", "type": 0}]
        raise AssertionError(path)

    async def run():
        async with link_app(tmp_path, monkeypatch) as (dashboard, client):
            dashboard.discord = discord
            auth = await sign_in(dashboard, client, [{"id": "55", "name": "Guild"}])
            response = await client.get("/api/dashboard/servers/55", headers=auth)
            assert response.status == 200
            assert (await response.json())["channels"] == [
                {"id": "9", "name": "general"}
            ]
            member_roles[:] = []  # Manage Server revoked after the link was minted
            assert (
                await client.get("/api/dashboard/servers/55", headers=auth)
            ).status == 403
            member_roles[:] = ["7"]
            monkeypatch.setenv("MAXWELL_DEV_MODE", "1")
            monkeypatch.setenv("MAXWELL_DEV_GUILD_IDS", "66")
            assert (
                await client.get("/api/dashboard/servers/55", headers=auth)
            ).status == 403
            servers = await (
                await client.get("/api/dashboard/servers", headers=auth)
            ).json()
            assert servers["servers"] == []

    asyncio.run(run())


def test_free_users_see_the_plan_but_cannot_write_premium_panels(tmp_path, monkeypatch):
    async def run():
        async with link_app(tmp_path, monkeypatch) as (dashboard, client):
            auth = await sign_in(dashboard, client)
            plan = await (await client.get("/api/dashboard/plan", headers=auth)).json()
            assert plan["plan"] == "free"
            assert plan["storage"]["quota_bytes"] == 0
            on = {row["id"] for row in plan["features"] if row["on"]}
            assert on == {"subagents"}
            files = await (
                await client.get("/api/dashboard/files", headers=auth)
            ).json()
            assert files["status"]["quota_bytes"] == 0 and files["entries"] == []
            upload = await client.post(
                "/api/dashboard/files/upload?path=a.txt", data=b"hi", headers=auth
            )
            assert upload.status == 403
            body = {"name": "x", "prompt": "hi", "cron": "0 9 * * *", "timezone": "UTC"}
            assert (
                await client.post("/api/dashboard/schedules", json=body, headers=auth)
            ).status == 403
            listing = await (
                await client.get("/api/dashboard/schedules", headers=auth)
            ).json()
            assert listing["premium"] is False and listing["schedules"] == []

    asyncio.run(run())


def test_message_usage_reaches_the_browser_as_a_percentage_only(tmp_path, monkeypatch):
    (tmp_path / "bot_control.json").write_text(
        json.dumps(
            {
                "message_quota_enabled": True,
                "message_quota_limit": 150,
                "message_quota_window_seconds": 3600,
            }
        )
    )
    ledger = MessageQuota(tmp_path / "message_quota.sqlite3")
    for _ in range(4):
        ledger.charge(UID, 150, 3600)

    async def run():
        async with link_app(tmp_path, monkeypatch) as (dashboard, client):
            auth = await sign_in(dashboard, client)
            response = await client.get("/api/dashboard/plan", headers=auth)
            raw = await response.text()
            messages = json.loads(raw)["messages"]
            assert messages["unlimited"] is False
            assert messages["percent_used"] == 3  # 4 of 150, rounded
            assert 0 < messages["resets_in"] <= 3600
            assert set(messages) == {"unlimited", "exempt", "percent_used", "resets_in"}
            assert "150" not in raw and "3600" not in raw

    asyncio.run(run())


def test_premium_files_console_round_trip(tmp_path, monkeypatch):
    async def run():
        async with link_app(tmp_path, monkeypatch, plan="premium") as (
            dashboard,
            client,
        ):
            auth = await sign_in(dashboard, client)
            plan = await (await client.get("/api/dashboard/plan", headers=auth)).json()
            assert plan["plan"] == "premium"
            assert plan["storage"]["quota_bytes"] == 100 * 1024 * 1024

            def upload(path, data):
                return client.post(
                    "/api/dashboard/files/upload",
                    params={"path": path},
                    data=data,
                    headers=auth,
                )

            assert (
                await upload("public/cat-3d/index.html", b"<h1>cat</h1>")
            ).status == 200
            handle = await client.put(
                "/api/dashboard/files/handle", json={"handle": "7up"}, headers=auth
            )
            listing = await handle.json()
            assert listing["public_root"] == "http://localhost/u/7up/"
            urls = {row["path"]: row["public_url"] for row in listing["entries"]}
            assert urls["public/cat-3d/index.html"] == "http://localhost/u/7up/cat-3d/"

            text = await client.get(
                "/api/dashboard/files/text",
                params={"path": "public/cat-3d/index.html"},
                headers=auth,
            )
            assert (await text.json())["text"] == "<h1>cat</h1>"
            moved = await client.post(
                "/api/dashboard/files/move",
                json={
                    "src": "public/cat-3d/index.html",
                    "dst": "public/cat/index.html",
                },
                headers=auth,
            )
            assert "public/cat/index.html" in {
                row["path"] for row in (await moved.json())["entries"]
            }

            assert (await upload("build/addon.mcaddon", b"PK\0\0binary")).status == 200
            binary = await client.get(
                "/api/dashboard/files/text",
                params={"path": "build/addon.mcaddon"},
                headers=auth,
            )
            assert binary.status == 400
            link = await client.post(
                "/api/dashboard/files/link",
                json={"path": "build/addon.mcaddon", "download": True},
                headers=auth,
            )
            url = (await link.json())["url"]
            assert "/u/_s/" + UID + "/" in url and url.endswith("?download=1")
            public = await client.post(
                "/api/dashboard/files/link",
                json={"path": "public/cat/index.html"},
                headers=auth,
            )
            assert (await public.json())["url"] == "http://localhost/u/7up/cat/"

            made = await client.post(
                "/api/dashboard/files/mkdir", json={"path": "notes"}, headers=auth
            )
            assert {"path": "notes", "type": "dir"}.items() <= next(
                row for row in (await made.json())["entries"] if row["path"] == "notes"
            ).items()
            assert (await upload("../escape.txt", b"x")).status == 400
            gone = await client.delete(
                "/api/dashboard/files", params={"path": "build"}, headers=auth
            )
            assert not any(
                row["path"].startswith("build")
                for row in (await gone.json())["entries"]
            )

            monkeypatch.setattr("api.dashboard_panels.UPLOAD_LIMIT", 10)
            assert (await upload("big.bin", b"x" * 11)).status == 413

    asyncio.run(run())


def test_premium_schedules_lifecycle_and_downgrade(tmp_path, monkeypatch):
    async def run():
        async with link_app(tmp_path, monkeypatch, plan="premium") as (
            dashboard,
            client,
        ):
            auth = await sign_in(dashboard, client)
            body = {
                "name": "Digest",
                "prompt": "send me the news",
                "cron": "0 9 * * *",
                "timezone": "UTC",
            }
            created = await (
                await client.post("/api/dashboard/schedules", json=body, headers=auth)
            ).json()
            assert created["premium"] is True
            [row] = created["schedules"]
            assert row["deliver"] == "dm" and row["enabled"] and row["next_run"]
            both = {**body, "at": "in 2h"}
            assert (
                await client.post("/api/dashboard/schedules", json=both, headers=auth)
            ).status == 400
            path = "/api/dashboard/schedules/" + row["id"]
            assert (
                await client.put(path, json={"failures": 0}, headers=auth)
            ).status == 400
            paused = await (
                await client.put(path, json={"enabled": False}, headers=auth)
            ).json()
            assert (
                paused["schedules"][0]["enabled"] is False
                and paused["schedules"][0]["next_run"] is None
            )
            edited = await client.put(
                path, json={"cron": "30 8 * * 1-5", "name": "Weekdays"}, headers=auth
            )
            row = (await edited.json())["schedules"][0]
            assert (
                row["spec"] == "30 8 * * 1-5"
                and row["name"] == "Weekdays"
                and row["enabled"]
            )

            MessageQuota(tmp_path / "message_quota.sqlite3").configure(UID, plan="free")
            # Lapsed Premium can still stop and clean up, never revive.
            assert (
                await client.put(path, json={"enabled": False}, headers=auth)
            ).status == 200
            assert (
                await client.put(path, json={"enabled": True}, headers=auth)
            ).status == 403
            assert (
                await client.post("/api/dashboard/schedules", json=body, headers=auth)
            ).status == 403
            deleted = await (await client.delete(path, headers=auth)).json()
            assert deleted["schedules"] == []
            assert (await client.delete(path, headers=auth)).status == 404

    asyncio.run(run())


class _Guild(SimpleNamespace):
    def get_member(self, uid):
        return self.members.get(uid)


def _member(*, admin=False, manage=False):
    return SimpleNamespace(
        guild_permissions=SimpleNamespace(administrator=admin, manage_guild=manage)
    )


def _links_env(monkeypatch):
    for key in (
        "MAXWELL_DASHBOARD_URL",
        "MAXWELL_DASHBOARD_SECRET",
        "MAXWELL_DASHBOARD_LINKS",
        "DISCORD_BOT_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DISCORD_TOKEN", "tok")
    monkeypatch.setenv("MAXWELL_PUBLIC_BASE_URL", "https://maxwell.example.com/dev")


def test_dashboard_links_mint_only_for_real_users_with_manageable_guilds(
    tmp_path, monkeypatch
):
    _links_env(monkeypatch)
    guilds = [
        _Guild(id=55, name="Managed", owner_id=1, members={100: _member(manage=True)}),
        _Guild(id=56, name="Owned", owner_id=100, members={100: _member()}),
        _Guild(id=57, name="Member only", owner_id=1, members={100: _member()}),
        _Guild(
            id=58,
            name="Out of dev scope",
            owner_id=1,
            members={100: _member(admin=True)},
        ),
        _Guild(id=59, name="Not a member", owner_id=1, members={}),
    ]
    bot = SimpleNamespace(
        config=SimpleNamespace(DATA_DIR=str(tmp_path)),
        guilds=guilds,
        _dev_scope_allows=lambda context, guild_id=None: guild_id != 58,
    )
    links = DashboardLinks(bot)
    assert links.available and links.url == "https://maxwell.example.com/dev/dashboard/"
    user = SimpleNamespace(id=100, name="zeki", global_name="Zeki", bot=False)
    url = asyncio.run(links.link_for(user))
    assert url.startswith("https://maxwell.example.com/dev/dashboard/#t=")
    payload = links.sessions.redeem_link(url.split("#t=", 1)[1])
    assert payload["user"] == {"id": "100", "name": "Zeki"}
    assert payload["guilds"] == [
        {"id": "55", "name": "Managed"},
        {"id": "56", "name": "Owned"},
    ]
    with pytest.raises(DashboardError):
        asyncio.run(links.link_for(SimpleNamespace(id=5, name="robot", bot=True)))
    for _ in range(3):
        asyncio.run(links.link_for(user))
    with pytest.raises(DashboardError, match="wait a minute"):
        asyncio.run(links.link_for(user))


def test_dashboard_links_are_off_without_a_public_address(tmp_path, monkeypatch):
    _links_env(monkeypatch)
    monkeypatch.delenv("MAXWELL_PUBLIC_BASE_URL")
    links = DashboardLinks(
        SimpleNamespace(config=SimpleNamespace(DATA_DIR=str(tmp_path)), guilds=[])
    )
    assert not links.available and links.url == ""
    with pytest.raises(DashboardError):
        asyncio.run(links.link_for(SimpleNamespace(id=100, name="zeki", bot=False)))


def test_web_dashboard_plugin_registers_and_removes_the_command(tmp_path, monkeypatch):
    from maxwell_core.plugins.manager import PluginManager

    _links_env(monkeypatch)
    shutil.copytree(
        ROOT / "plugins" / "web_dashboard", tmp_path / "plugins" / "web_dashboard"
    )
    data = tmp_path / "data"
    bot = SimpleNamespace(
        tools={}, config=SimpleNamespace(DATA_DIR=str(data)), guilds=[]
    )
    manager = PluginManager(
        bot,
        plugins_dir=str(tmp_path / "plugins"),
        data_dir=str(data),
        state_file=str(data / "plugins.json"),
    )
    # The loader re-imports plugins.web_dashboard from the copy; put the real
    # modules back afterwards so later tests don't get the tmp_path clone.
    saved = {
        name: mod
        for name, mod in sys.modules.items()
        if name.startswith("plugins.web_dashboard")
    }
    try:
        assert "web_dashboard" in manager.load_plugins()
        assert type(bot._dashboard_links).__name__ == "DashboardLinks"
        assert "dashboard" in {cmd["name"] for cmd in ui.USER_INSTALL_COMMANDS}
    finally:
        asyncio.run(manager.teardown())
        for name in [
            name for name in sys.modules if name.startswith("plugins.web_dashboard")
        ]:
            del sys.modules[name]
        sys.modules.update(saved)
    assert bot._dashboard_links is None
    assert "dashboard" not in {cmd["name"] for cmd in ui.USER_INSTALL_COMMANDS}
