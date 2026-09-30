"""Hermetic regressions for the API and generated-site audit."""

import asyncio
from collections import defaultdict
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import api.api_server as api
import api.auth as auth
import site_backend
import site_server
import site_test


def test_operator_api_rejects_discord_token_without_basic_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("MAXWELL_ADMIN_USER", "operator")
    monkeypatch.setenv("MAXWELL_ADMIN_PASSWORD", "secret")
    monkeypatch.setattr(auth, "_auth_failures", defaultdict(list))
    monkeypatch.setattr(api, "DATA_DIR", tmp_path)

    async def scenario():
        app = web.Application(middlewares=[auth._auth_middleware_unless_login])
        app.router.add_get("/api/control", api.control_get)
        async with TestClient(TestServer(app)) as client:
            response = await client.get(
                "/api/control", headers={"X-Discord-Token": "old-session"}
            )
            assert response.status == 401
            assert await response.json() == {"error": "unauthorized"}
            response = await client.get(
                "/api/control",
                auth=aiohttp.BasicAuth("operator", "secret"),
            )
            assert response.status == 200

    asyncio.run(scenario())


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
        web.HTTPFound("/api/github/oauth/complete"),
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
                SimpleNamespace(path="/api/github/oauth/callback", method="GET"),
                handler,
            )

    asyncio.run(scenario())

