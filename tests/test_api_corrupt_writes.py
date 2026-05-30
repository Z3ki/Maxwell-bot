import asyncio

import api.api_server as api


class FakeRequest:
    def __init__(self, body=None, query=None):
        self._body = body if body is not None else {}
        self.query = query or {}
        self.match_info = {}
        self.headers = {"Authorization": "Basic ignored"}
        self.remote = "127.0.0.1"

    async def json(self):
        return self._body


def _json(resp):
    return resp.text


def test_context_post_refuses_corrupt_shared_context(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "DATA_DIR", tmp_path)
    (tmp_path / "shared_context.json").write_text("{ broken", encoding="utf-8")

    async def run():
        resp = await api.context_post(FakeRequest({"content": "new fact"}))
        assert resp.status == 409
        assert (tmp_path / "shared_context.json").read_text(encoding="utf-8") == "{ broken"

    asyncio.run(run())


def test_commands_post_refuses_corrupt_command_queue(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "DATA_DIR", tmp_path)
    (tmp_path / "bot_commands.json").write_text("{ broken", encoding="utf-8")

    async def run():
        resp = await api.commands_post(FakeRequest({"type": "reload_controls"}))
        assert resp.status == 409
        assert (tmp_path / "bot_commands.json").read_text(encoding="utf-8") == "{ broken"

    asyncio.run(run())


def test_rem_enable_refuses_corrupt_rem_control(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "DATA_DIR", tmp_path)
    (tmp_path / "rem_control.json").write_text("{ broken", encoding="utf-8")

    async def run():
        resp = await api.rem_enable(FakeRequest())
        assert resp.status == 409
        assert (tmp_path / "rem_control.json").read_text(encoding="utf-8") == "{ broken"

    asyncio.run(run())
