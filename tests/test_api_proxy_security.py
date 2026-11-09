"""Generated backends cannot receive Maxwell credentials or control its origin."""

import asyncio

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import api.api_server as api


@pytest.mark.parametrize("websocket", [False, True])
@pytest.mark.parametrize("operator", [False, True])
@pytest.mark.parametrize("prefix", ["", "/dev"])
def test_proxy_filters_maxwell_credentials_but_preserves_site_auth(monkeypatch, websocket, operator, prefix):
    monkeypatch.setenv("MAXWELL_ADMIN_USER", "operator")
    monkeypatch.setenv("MAXWELL_ADMIN_PASSWORD", "operator-secret")
    monkeypatch.setenv("MAXWELL_PUBLIC_BASE_URL", "https://example.test" + prefix)

    async def run():
        captured = []

        async def backend(request):
            captured.append(dict(request.headers))
            if websocket:
                socket = web.WebSocketResponse()
                await socket.prepare(request)
                await socket.send_str("ok")
                await socket.close()
                return socket
            return web.Response(text="ok", headers={
                "Service-Worker-Allowed": "/",
                "Set-Cookie": "site_session=next; Path=/; Domain=example.test; HttpOnly",
            })

        upstream = web.Application()
        upstream.router.add_get("/test", backend)
        async with TestServer(upstream) as server:
            monkeypatch.setattr(api, "_site_server_enabled", lambda slug: True)
            monkeypatch.setattr(api.site_server, "port_for", lambda *args: server.port)
            frontend = web.Application()
            frontend.router.add_get("/bot/{slug}/api/{path:.*}", api.site_proxy)
            async with TestClient(TestServer(frontend)) as client:
                headers = {
                    "Authorization": aiohttp.BasicAuth(
                        "operator" if operator else "site-user",
                        "operator-secret" if operator else "site-secret",
                    ).encode(),
                    "Cookie": (
                        "__Host-maxwell_session=private-session; site_session=site-value; "
                        "__Host-maxwell_login=private-flow; maxwell_dev_session=dev-session; "
                        "maxwell_dev_login=dev-flow"
                    ),
                }
                if websocket:
                    async with client.ws_connect("/bot/demo/api/test", headers=headers) as socket:
                        assert (await socket.receive()).data == "ok"
                else:
                    response = await client.get("/bot/demo/api/test", headers=headers)
                    assert response.status == 200
                    assert await response.text() == "ok"
                    assert "Service-Worker-Allowed" not in response.headers
                    assert response.headers["Set-Cookie"] == (
                        "site_session=next; HttpOnly; Path=" + prefix + "/bot/demo/"
                    )
                assert len(captured) == 1
                assert captured[0].get("Cookie") == "site_session=site-value"
                assert captured[0].get("Authorization") == (
                    None if operator else headers["Authorization"]
                )

    asyncio.run(run())
