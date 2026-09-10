"""Hermetic regressions for the API and generated-site audit."""

import asyncio
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import api.api_server as api
import api.auth as auth
import site_backend
import site_server
import site_test


def test_discord_session_revoked_with_admin_membership(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "DATA_DIR", tmp_path)
    monkeypatch.setattr(auth, "DISCORD_ALLOWED_USER_IDS", set())
    monkeypatch.delenv("MAXWELL_OWNER_IDS", raising=False)
    monkeypatch.setattr(auth, "_DISCORD_TOKENS", {})
    monkeypatch.setattr(api, "_DISCORD_TOKENS", auth._DISCORD_TOKENS)
    path = tmp_path / "admins.json"
    path.write_text('["42"]')
    auth._set_discord_token("session", {"user_id": "42", "username": "admin"})
    request = SimpleNamespace(headers={"X-Discord-Token": "session"})
    assert auth._discord_token_authed(request)
    path.write_text("[]")
    assert not auth._discord_token_authed(request)
    assert asyncio.run(api.discord_auth_verify(request)).status == 401
    assert "session" not in auth._DISCORD_TOKENS


@pytest.mark.parametrize(
    "raw",
    [
        '{"kv":',
        "[]",
        '{"kv": [], "collections": {}}',
        '{"kv": {}, "collections": {"notes": [null]}}',
    ],
)
@pytest.mark.parametrize(
    "operation",
    [
        lambda p: site_backend.kv_set(p, "demo", "new", 1),
        lambda p: site_backend.kv_bump(p, "demo", "counter"),
        lambda p: site_backend.kv_delete(p, "demo", "old"),
        lambda p: site_backend.items_add(p, "demo", "notes", "new"),
        lambda p: site_backend.items_delete(p, "demo", "notes", all_items=True),
    ],
)
def test_store_mutation_preserves_corrupt_data(tmp_path, raw, operation):
    path = site_backend.store_path(tmp_path, "demo")
    path.parent.mkdir()
    path.write_text(raw)
    with pytest.raises(site_backend.SiteBackendError, match="refusing to overwrite"):
        operation(tmp_path)
    assert path.read_text() == raw


@pytest.mark.parametrize("raw", ['{"demo":', "[]", '{"demo": null}'])
def test_registry_mutation_preserves_corrupt_data(tmp_path, raw):
    path = site_server.registry_path(tmp_path)
    path.write_text(raw)
    with pytest.raises(site_server.SiteServerError, match="refusing to overwrite"):
        site_server._write_entry(tmp_path, "other", {"port": 8801})
    assert path.read_text() == raw


@pytest.mark.parametrize(
    "path",
    [
        "%2e%2e/other/",
        "a/%2E%2E/../other/",
        "https://example.test/bot/demo/../other/",
        "https://example.test/bot/demo/%2e%2e/other/",
        "%252e%252e/other/",
    ],
)
def test_probe_url_rejects_encoded_and_absolute_traversal(path):
    with pytest.raises(ValueError):
        site_test.page_url("https://example.test/bot", "demo", path)


def test_browser_profiles_unique_in_same_millisecond(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(site_test.time, "time", lambda: 1000)
    first = site_test._new_profile_dir()
    second = site_test._new_profile_dir()
    assert first != second


def test_public_stream_does_not_exhaust_admin_capacity(monkeypatch):
    async def scenario():
        monkeypatch.setattr(api, "_API_CONCURRENCY_SEM", asyncio.Semaphore(1))
        monkeypatch.setattr(api, "_API_GLOBAL_LIMITER", None)
        entered = asyncio.Event()
        finish = asyncio.Event()

        async def stream_handler(request):
            entered.set()
            await finish.wait()
            return web.Response()

        stream = asyncio.create_task(
            api._reliability_middleware(
                SimpleNamespace(path="/bot/demo/api/stream", method="GET"),
                stream_handler,
            )
        )
        await entered.wait()
        try:
            result = await asyncio.wait_for(
                api._reliability_middleware(
                    SimpleNamespace(path="/api/control", method="GET"), api.health_check
                ),
                timeout=0.2,
            )
            assert result.status == 200
        finally:
            finish.set()
            await stream

    asyncio.run(scenario())


def test_proxy_limits_chunked_uploads(monkeypatch):
    async def scenario():
        received = []

        async def upstream_handler(request):
            try:
                received.append(await request.read())
            except ConnectionResetError:
                pass
            return web.Response(text="ok")

        upstream = web.Application()
        upstream.router.add_post("/upload", upstream_handler)
        async with TestServer(upstream) as upstream_server:
            monkeypatch.setattr(api, "_site_server_enabled", lambda slug: True)
            monkeypatch.setattr(
                site_server, "port_for", lambda *args: upstream_server.port
            )
            monkeypatch.setattr(api, "SITE_UPLOAD_MAX", 8)
            app = web.Application()
            app.router.add_post("/bot/{slug}/api/{path:.*}", api.site_proxy)
            async with TestClient(TestServer(app)) as client:

                async def chunks():
                    yield b"12345678"
                    yield b"9"

                response = await client.post("/bot/demo/api/upload", data=chunks())
                assert response.status == 413
                assert not any(len(body) > 8 for body in received)

    asyncio.run(scenario())


def test_proxy_preserves_encoded_path_and_query(monkeypatch):
    async def scenario():
        async def upstream_handler(request):
            return web.json_response(
                {"path": request.path, "query": dict(request.query)}
            )

        upstream = web.Application()
        upstream.router.add_get("/{path:.*}", upstream_handler)
        async with TestServer(upstream) as upstream_server:
            monkeypatch.setattr(api, "_site_server_enabled", lambda slug: True)
            monkeypatch.setattr(
                site_server, "port_for", lambda *args: upstream_server.port
            )
            app = web.Application()
            app.router.add_get("/bot/{slug}/api/{path:.*}", api.site_proxy)
            async with TestClient(TestServer(app)) as client:
                response = await client.get("/bot/demo/api/part%23one?q=a%2Bb%26c%23d")
                assert await response.json() == {
                    "path": "/part#one",
                    "query": {"q": "a+b&c#d"},
                }

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "error",
    [
        web.HTTPFound("/admin/#discord_token=test"),
        web.HTTPNotFound(),
        web.HTTPForbidden(),
    ],
)
def test_reliability_middleware_preserves_http_exceptions(monkeypatch, error):
    async def scenario():
        monkeypatch.setattr(api, "_API_GLOBAL_LIMITER", None)

        async def handler(request):
            raise error

        with pytest.raises(type(error)):
            await api._reliability_middleware(
                SimpleNamespace(path="/api/auth/discord/callback", method="GET"),
                handler,
            )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "checks",
    [
        """
    const id = "1234567890abcdef1234567890abcdef";
    S.context = [{id, scope: "global", content: "fact"}];
    context();
    assert($('context').innerHTML.includes('data-del-ctx="' + id + '"'));
    """,
        """
    S.auth = {user: "admin", pass: "pass"};
    startPolling();
    assert(timers.size === 1);
    logout(false);
    assert(timers.size === 0);
    await loadAll(true);
    assert(fetches === 0);
    """,
    ],
)
def test_dashboard_identifiers_and_logout(checks):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    html = (Path(__file__).resolve().parents[1] / "web/admin/index.html").read_text()
    script = html.split("<script>", 1)[1].split('      $("loginForm").onsubmit', 1)[0]
    prelude = """
    const assert = require('node:assert/strict');
    const elements = new Map();
    const timers = new Set();
    let fetches = 0;
    global.document = {
      getElementById: id => {
        if (!elements.has(id)) elements.set(id, {innerHTML: '', classList: {add(){}, remove(){}}});
        return elements.get(id);
      },
      querySelectorAll: () => [],
    };
    global.localStorage = {removeItem(){}};
    global.setInterval = () => {const timer = {}; timers.add(timer); return timer;};
    global.clearInterval = timer => timers.delete(timer);
    global.setTimeout = () => 0;
    global.fetch = async () => {fetches++; throw Error('unexpected fetch');};
    """
    result = subprocess.run(
        [node],
        input=prelude
        + script
        + "\n(async () => {"
        + checks
        + "\n})().catch(e => {console.error(e); process.exitCode=1;});",
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
