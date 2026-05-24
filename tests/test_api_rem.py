import base64
import asyncio
import importlib


def test_rem_status_payload_shape(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import api.api_server as api_server
    api = importlib.reload(api_server)
    status = api._load_rem_status()
    assert {"enabled", "interval_s", "last_run", "events_buffered", "last_audit_preview", "running"} <= set(status)


def test_api_mutation_auth_middleware(monkeypatch):
    monkeypatch.setenv("MAXWELL_ADMIN_USER", "admin")
    monkeypatch.setenv("MAXWELL_ADMIN_PASSWORD", "pw")
    import api.api_server as api_server
    api = importlib.reload(api_server)

    class Req:
        method = "POST"
        path = "/api/rem/run"
        headers = {}

    async def handler(request):
        return "ok"

    async def run():
        mw = await api._auth_middleware_unless_login(None, handler)
        res = await mw(Req())
        assert res.status == 401
        token = base64.b64encode(b"admin:pw").decode()
        Req.headers = {"Authorization": f"Basic {token}"}
        assert await mw(Req()) == "ok"
    asyncio.run(run())
