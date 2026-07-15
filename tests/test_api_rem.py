import asyncio
import base64
import importlib


def test_rem_status_payload_shape(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import api.api_server as api_server

    api = importlib.reload(api_server)
    status = api._load_rem_status()
    assert {
        "enabled",
        "interval_s",
        "last_run",
        "events_buffered",
        "last_audit_preview",
        "running",
    } <= set(status)


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
        res = await api._auth_middleware_unless_login(Req(), handler)
        assert res.status == 401
        token = base64.b64encode(b"admin:pw").decode()
        Req.headers = {"Authorization": f"Basic {token}"}
        assert await api._auth_middleware_unless_login(Req(), handler) == "ok"

    asyncio.run(run())


def test_string_false_controls_stay_false(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import api.api_server as api_server

    api = importlib.reload(api_server)

    assert api._sanitize_control({"bot_enabled": "false"})["bot_enabled"] == False  # noqa: E712
    assert api._sanitize_control({"bot_enabled": "true"})["bot_enabled"] == True  # noqa: E712


def test_set_presence_keeps_command_lifecycle_status(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import api.api_server as api_server

    api = importlib.reload(api_server)

    class Req:
        async def json(self):
            return {"type": "set_presence", "status": "idle", "activity_text": "busy"}

    async def run():
        res = await api.commands_post(Req())
        assert res.status == 200
        commands = api._load_commands()
        assert commands[-1]["status"] == "pending"
        assert commands[-1]["presence_status"] == "idle"

    asyncio.run(run())
