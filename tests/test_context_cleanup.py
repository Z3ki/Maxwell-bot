import asyncio
import json
import os
from types import SimpleNamespace

import pytest

discord = pytest.importorskip("discord")

from context_cleanup import ContextCleanupEngine  # noqa: E402


class FakeMemory:
    def __init__(self, entries=None):
        self.entries = list(entries or [])

    async def list_shared_context(self, limit=200):
        return [dict(e) for e in self.entries[:limit]]

    async def remove_shared_context(self, context_id):
        before = len(self.entries)
        self.entries = [e for e in self.entries if e["id"] != context_id]
        return len(self.entries) < before

    async def update_shared_context(self, context_id, updates):
        for e in self.entries:
            if e["id"] == context_id:
                e.update(updates)
                return True
        return False

    async def add_shared_context(self, entry):
        ne = {"id": f"new_{len(self.entries)}", **entry}
        self.entries.append(ne)
        return ne["id"]


class FakeProvider:
    def __init__(self, payload):
        self.available = True
        self._payload = payload
        self.calls = 0

    async def generate_response(self, messages, timeout=60, **kwargs):
        self.calls += 1
        return self._payload


class FakeBot:
    def __init__(self, data_dir, memory, provider):
        self.config = SimpleNamespace(DATA_DIR=data_dir)
        self.memory = memory
        self.ai_provider = provider
        self._control = {"autonomy_model": "", "ai_timeout_seconds": 120}

    async def _get_autonomy_provider(self):
        return self.ai_provider

    async def _acquire_ai_slot(self, timeout=30):
        pass

    async def _release_ai_slot(self):
        pass


def _entries():
    return [
        {"id": "a1", "scope": "global", "visibility": "shared", "importance": 8, "content": "User likes pizza"},
        {"id": "a2", "scope": "global", "visibility": "shared", "importance": 7, "content": "user likes pizza"},
        {"id": "a3", "scope": "user:123", "visibility": "private", "importance": 5, "content": "TODO truncated"},
        {"id": "a4", "scope": "global", "visibility": "shared", "importance": 9, "content": "User name is Alice"},
    ]


def test_run_once_applies_delete_edit_merge(tmp_path):
    os.environ["DATA_DIR"] = str(tmp_path)
    payload = json.dumps({
        "audit": "removed 1 dupe, edited 1, merged 1",
        "ops": [
            {"kind": "delete", "id": "a2", "reason": "near-duplicate of a1"},
            {"kind": "edit", "id": "a3", "content": "TODO item pending", "importance": 6, "reason": "fix truncation"},
            {"kind": "merge", "keep_id": "a1", "delete_ids": ["a4"], "content": "User likes pizza and is named Alice", "importance": 9, "reason": "consolidate identity + preference"},
        ],
    })
    memory = FakeMemory(_entries())
    bot = FakeBot(str(tmp_path), memory, FakeProvider(payload))
    engine = ContextCleanupEngine(bot)
    engine.enabled = True

    result = asyncio.run(engine.run_once())

    assert result["ops"] == 3
    ids = [e["id"] for e in memory.entries]
    assert "a2" not in ids  # deleted
    assert "a4" not in ids  # merged away
    a1 = [e for e in memory.entries if e["id"] == "a1"][0]
    assert a1["content"] == "User likes pizza and is named Alice"
    assert a1["importance"] == 9
    a3 = [e for e in memory.entries if e["id"] == "a3"][0]
    assert a3["content"] == "TODO item pending"
    assert a3["importance"] == 6

    state = asyncio.run(engine.store.load_state())
    assert state["passes_total"] == 1
    assert state["ops_applied_total"] == 3
    log = asyncio.run(engine.store.load_log())
    assert len(log) == 1
    assert "dupe" in log[0]["audit"]


def test_run_once_empty_store_no_call(tmp_path):
    os.environ["DATA_DIR"] = str(tmp_path)
    provider = FakeProvider('{"audit":"noop","ops":[]}')
    memory = FakeMemory([])
    bot = FakeBot(str(tmp_path), memory, provider)
    engine = ContextCleanupEngine(bot)
    result = asyncio.run(engine.run_once())
    assert result["skipped"] is False
    assert result["ops"] == 0
    assert provider.calls == 0  # no LLM call on empty store


def test_run_once_rejects_unknown_kinds(tmp_path):
    os.environ["DATA_DIR"] = str(tmp_path)
    payload = json.dumps({
        "audit": "noop",
        "ops": [
            {"kind": "nuke_everything", "id": "a1"},
            {"kind": "delete", "id": "doesnotexist"},
            {"kind": "edit", "id": "a1", "content": "  "},  # empty content dropped
        ],
    })
    memory = FakeMemory(_entries())
    bot = FakeBot(str(tmp_path), memory, FakeProvider(payload))
    engine = ContextCleanupEngine(bot)
    result = asyncio.run(engine.run_once())
    # nothing applied
    assert result["ops"] == 0
    ids = [e["id"] for e in memory.entries]
    assert ids == ["a1", "a2", "a3", "a4"]


def test_status_shape(tmp_path):
    os.environ["DATA_DIR"] = str(tmp_path)
    memory = FakeMemory(_entries())
    bot = FakeBot(str(tmp_path), memory, FakeProvider('{"audit":"x","ops":[]}'))
    engine = ContextCleanupEngine(bot)
    asyncio.run(engine.run_once())
    status = asyncio.run(engine.status())
    assert {"enabled", "interval_seconds", "running", "last_run", "last_audit",
            "ops_applied_total", "passes_total", "log"} <= set(status)
    assert status["passes_total"] == 1


def test_parse_plan_non_list_ops_returns_no_ops_keeps_audit(tmp_path):
    # Regression: a non-list `ops` (null/object/string) previously caused
    # _parse_plan to swap its return values, treating the audit text as an op
    # (AttributeError swallowed by apply()) and writing "[]" as the audit.
    engine = ContextCleanupEngine(FakeBot(str(tmp_path), FakeMemory(), FakeProvider("")))
    ops, audit = engine._parse_plan('{"audit": "no work needed", "ops": null}', _entries())
    assert ops == []
    assert audit == "no work needed"