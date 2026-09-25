import asyncio
import json

import api.api_server as api
from rag_memory import MemoryRequester, _memory_row_visible


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


def test_context_post_does_not_touch_legacy_json(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "DATA_DIR", tmp_path)
    (tmp_path / "shared_context.json").write_text("{ broken", encoding="utf-8")

    class _Cur:
        rowcount = 1

    monkeypatch.setattr(api, "_rag_exec", lambda *a, **k: _Cur())

    async def run():
        resp = await api.context_post(FakeRequest({"content": "new fact"}))
        assert resp.status == 200
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


def test_operator_memory_add_is_explicit_public_global_memory(monkeypatch):
    captured = {}

    class _Cur:
        rowcount = 1

    def record(sql, params=(), **kwargs):
        captured["sql"] = sql
        captured["params"] = params
        return _Cur()

    monkeypatch.setattr(api, "_rag_exec", record)

    async def run():
        response = await api.memory_add(FakeRequest({"content": "operator fact"}))
        assert response.status == 200

    asyncio.run(run())
    sql = captured["sql"]
    params = captured["params"]
    assert "'global'" in sql
    metadata = json.loads(params[3])
    assert metadata["visibility"] == "public"
    assert metadata["public_approved"] is True
    assert metadata["source_kind"] == "operator_public"

    requester = MemoryRequester(
        user_id="u1", channel_id="c1", guild_id="g1",
        is_dm=False, channel_is_public=True,
    )
    assert _memory_row_visible(
        {"kind": "ltm", "scope": "global", "metadata": metadata}, requester
    )
