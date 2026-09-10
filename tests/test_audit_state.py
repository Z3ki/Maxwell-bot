"""State, concurrency, and memory regressions. All external I/O is mocked."""

import asyncio
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import numpy as np
import pytest

from autonomy import AutonomyStore, _truncate_keep_tail
from autonomy_social import FloorSettings
from concurrency_safety import ChannelWorkQueues, KeyedLocks
from context_budget import TIER_ORDER, allocate
from email_inbox import MailPollState, _fetch_new_sync
from inbox import InboxStore
from jobs import BackgroundJobManager
from knowledge_graph import KnowledgeGraph
from rag_memory import EMBED_DIM, EMBED_MAX_CHARS, RAGMemoryManager
from rem import RemStore, run_rem_once


def test_floor_settings_honor_zero_and_reject_infinity():
    settings = FloorSettings.from_control(
        {
            "autonomy_floor_cooldown_seconds": 0,
            "autonomy_floor_hold_release_seconds": 0,
            "autonomy_floor_mid_flow_messages": float("inf"),
        }
    )
    assert settings.cooldown_seconds == 0
    assert settings.hold_release_seconds == 0
    assert settings.mid_flow_min_messages == FloorSettings.mid_flow_min_messages


@pytest.mark.parametrize("payload", ['{"actions": "bad"}', '{"actions": ["bad"]}'])
def test_malformed_rem_actions_do_not_consume_slice(tmp_path, payload):
    async def run():
        memory = SimpleNamespace(get_long_term_memory=list)
        log = SimpleNamespace(
            drain_slice=AsyncMock(return_value=[{"content": "a fact"}])
        )
        provider = SimpleNamespace(
            generate_chat_completion=AsyncMock(return_value={"content": payload})
        )
        await run_rem_once(
            memory_manager=memory,
            rem_log=log,
            provider=provider,
            data_dir=str(tmp_path),
            model="fake",
        )
        state = await RemStore(str(tmp_path)).load_state()
        assert not state.get("last_rem_run_ts")
        assert state["running"] is False

    asyncio.run(run())


@pytest.mark.parametrize("raw", ['{"items":', "[]", '{"items": {}}'])
def test_inbox_mutation_preserves_corrupt_file(tmp_path, raw):
    async def run():
        store = InboxStore(str(tmp_path))
        store.path.write_text(raw)
        with pytest.raises(ValueError):
            await store.upsert({"id": "new"})
        assert store.path.read_text() == raw

    asyncio.run(run())


@pytest.mark.parametrize(
    "raw", ['{"goals":', "[]", '{"goals": {}}', '{"goals": [null]}']
)
def test_goal_mutation_preserves_corrupt_file(tmp_path, raw):
    async def run():
        store = AutonomyStore(str(tmp_path))
        store.goals_file.write_text(raw)
        with pytest.raises(ValueError):
            await store.add_goal("new")
        assert store.goals_file.read_text() == raw

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["cancel", "error"])
def test_background_task_setup_failure_releases_runtime_and_capacity(tmp_path, failure):
    async def run():
        manager = BackgroundJobManager(str(tmp_path / "jobs.json"))
        job = manager.create(guild_id="g", channel_id="c", user_id="u", goal="test")
        manager.attach_runtime(job.id, message=object())

        async def worker():
            raise RuntimeError("setup failed")

        task = asyncio.create_task(worker())
        manager.track_task(job.id, task)
        if failure == "cancel":
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)
        assert manager.active_count() == 0
        assert not manager._runtime
        assert not manager._tasks
        assert job.status == ("cancelled" if failure == "cancel" else "error")

    asyncio.run(run())


def test_entity_profiles_exclude_restricted_or_expired_shared_facts(memory):
    async def run():
        for visibility in ("private", "admin_only", "shared"):
            await memory.add_shared_context(
                {"scope": "user:123", "content": visibility, "visibility": visibility}
            )
        await memory.add_shared_context(
            {
                "scope": "dm:123",
                "content": "expired",
                "expires_at": "2000-01-01T00:00:00+00:00",
            }
        )
        assert [r["content"] for r in await memory.get_entity_facts("123")] == [
            "shared"
        ]

    asyncio.run(run())


def test_entity_budget_does_not_force_oversized_first_fact(memory):
    async def run():
        await memory.add_entity_fact("123", "a very long durable fact")
        assert await memory.get_entity_facts("123", budget=1) == []

    asyncio.run(run())


def test_shared_facts_in_different_scopes_survive_restart(memory):
    async def run():
        first = await memory.add_shared_context(
            {"scope": "user:1", "content": "prefers tea"}
        )
        second = await memory.add_shared_context(
            {"scope": "user:2", "content": "prefers tea"}
        )
        assert {r["id"] for r in await memory.list_shared_context()} == {first, second}
        fresh = RAGMemoryManager(str(memory.data_dir))
        try:
            assert {r["id"] for r in await fresh.list_shared_context()} == {
                first,
                second,
            }
        finally:
            fresh._db.close()

    asyncio.run(run())


def test_legacy_shared_hashes_migrate_without_losing_scopes(memory):
    async def run():
        first = await memory.add_shared_context(
            {"scope": "user:1", "content": "prefers tea"}
        )
        memory._db.execute("UPDATE vectors SET content_hash='' WHERE id=?", (first,))
        fresh = RAGMemoryManager(str(memory.data_dir))
        try:
            await fresh.add_shared_context(
                {"scope": "user:2", "content": "prefers tea"}
            )
            assert len(await fresh.list_shared_context()) == 2
            await fresh.add_shared_context(
                {"scope": "user:1", "content": "prefers tea"}
            )
            assert len(await fresh.list_shared_context()) == 2
        finally:
            fresh._db.close()

    asyncio.run(run())


def test_private_global_facts_without_owner_fail_closed(memory):
    async def run():
        await memory.add_shared_context(
            {"content": "private REM fact", "visibility": "private"}
        )
        assert await memory.get_relevant_shared_context(user_id="stranger") == []
        assert len(await memory.get_relevant_shared_context(is_admin=True)) == 1
        await memory.add_shared_context(
            {"content": "my fact", "scope": "user:123", "visibility": "private"}
        )
        assert [
            r["content"]
            for r in await memory.get_relevant_shared_context(user_id="123")
        ] == ["my fact"]

    asyncio.run(run())


def test_pending_embedding_failures_do_not_starve_later_rows(memory, monkeypatch):
    attempted = []

    async def fail(row_id, text):
        attempted.append(row_id)
        return False

    monkeypatch.setattr(memory, "_embed_and_store", fail)
    for index in range(20):
        memory._db.execute(
            "INSERT INTO vectors (id, kind, content, timestamp, created_at) VALUES (?, 'ltm', ?, '', 0)",
            (str(index), "x" * (EMBED_MAX_CHARS + 1)),
        )
    asyncio.run(memory._embed_pending_all(batch_size=4))
    assert len(attempted) == len(set(attempted)) == 20


def test_batch_embedding_does_not_overwrite_edited_content(memory, monkeypatch):
    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def json(self):
            memory._db.execute("UPDATE vectors SET content='new fact' WHERE id='old'")
            return {"embeddings": [[1.0] * EMBED_DIM]}

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def post(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr("rag_memory.aiohttp.ClientSession", Session)
    memory._db.execute(
        "INSERT INTO vectors (id, kind, content, timestamp, created_at) VALUES ('old', 'ltm', 'old fact', '', 0)"
    )
    asyncio.run(memory._embed_pending_all())
    assert (
        memory._db.execute("SELECT embedding FROM vectors WHERE id='old'").fetchone()[0]
        is None
    )


def test_zero_tail_budget_is_empty():
    assert _truncate_keep_tail("secret context", 0) == ""
    assert _truncate_keep_tail("secret context", -1) == ""


def test_rounding_does_not_exceed_tier_caps():
    plan = allocate(
        7,
        weights=dict.fromkeys(TIER_ORDER, 1),
        minimums=dict.fromkeys(TIER_ORDER, 0),
        caps={"recent": 1},
    )
    assert plan.budget_for("recent") <= 1
    assert sum(t.budget for t in plan.tiers.values()) == 7


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan")])
def test_nonfinite_budgets_fall_back(value):
    assert allocate(value).total == 0


def test_keyed_locks_keeps_new_lock_when_older_locks_are_held():
    async def run():
        locks = KeyedLocks(max_idle=16)
        held = [locks.get(str(i)) for i in range(16)]
        for lock in held:
            await lock.acquire()
        newest = locks.get("new")
        assert locks.get("new") is newest
        for lock in held:
            lock.release()

    asyncio.run(run())


def test_close_before_worker_starts_wakes_submitter():
    async def run():
        queues = ChannelWorkQueues()
        submitter = asyncio.create_task(queues.submit(1, 1, lambda: asyncio.sleep(0)))
        await asyncio.sleep(0)
        await queues.close()
        await asyncio.sleep(0)
        try:
            assert submitter.done()
            assert submitter.cancelled()
        finally:
            submitter.cancel()
            await asyncio.gather(submitter, return_exceptions=True)

    asyncio.run(run())


def test_cancelled_last_queued_item_does_not_leak_worker():
    async def run():
        queues = ChannelWorkQueues()
        started, release = asyncio.Event(), asyncio.Event()

        async def blocking():
            started.set()
            await release.wait()

        first = asyncio.create_task(queues.submit(1, 1, blocking))
        await started.wait()
        second = asyncio.create_task(queues.submit(1, 1, blocking))
        await asyncio.sleep(0)
        second.cancel()
        await asyncio.gather(second, return_exceptions=True)
        release.set()
        await first
        for _ in range(5):
            await asyncio.sleep(0)
        try:
            assert not queues._workers
            assert not queues._queues
        finally:
            await queues.close()

    asyncio.run(run())


class FakeIMAP:
    def __init__(self, failed_uid=None):
        self.failed_uid = failed_uid
        self.readonly = False
        self.fetched = []

    def select(self, mailbox, readonly=False):
        self.readonly = readonly
        return "OK", [b"3"]

    def uid(self, operation, *args):
        if operation == "SEARCH":
            return "OK", [b"1 2 3"]
        uid = int(args[0])
        self.fetched.append(uid)
        if uid == self.failed_uid:
            return "NO", []
        return "OK", [
            (b"HEADER", b"From: Ada <ada@example.com>\r\nSubject: Test\r\n\r\n")
        ]

    def close(self):
        pass

    def logout(self):
        pass


def test_mail_poll_selects_readonly_to_prevent_close_expunge(monkeypatch):
    conn = FakeIMAP()
    monkeypatch.setattr("email_inbox._connect", lambda *args: conn)
    assert len(_fetch_new_sync("localhost", 993, "u", "p", 0, 8)) == 3
    assert conn.readonly


def test_mail_fetch_failure_cannot_skip_uid_forever(monkeypatch):
    conn = FakeIMAP(failed_uid=2)
    monkeypatch.setattr("email_inbox._connect", lambda *args: conn)
    mails = _fetch_new_sync("localhost", 993, "u", "p", 0, 8)
    assert [mail["uid"] for mail in mails] == [1]
    assert conn.fetched == [1, 2]


def test_failed_watermark_save_can_be_retried(tmp_path, monkeypatch):
    def fail(*args):
        raise OSError("disk full")

    async def run():
        state = MailPollState(tmp_path)
        with monkeypatch.context() as patch:
            patch.setattr("email_inbox._atomic_json_write_sync", fail)
            with pytest.raises(OSError):
                await state.save(10)
        assert state.last_uid == 0
        await state.save(10)
        assert json.loads(state.path.read_text())["last_uid"] == 10

    asyncio.run(run())


@pytest.mark.parametrize("table", ["graph_nodes", "graph_edges"])
def test_graph_properties_remain_valid_json(table):
    db = sqlite3.connect(":memory:")
    try:
        graph = KnowledgeGraph(db)
        props = {"description": "x" * 5000}
        if table == "graph_nodes":
            graph.upsert_node("thing:a", "thing", "a", props)
        else:
            graph.upsert_edge("thing:a", "USES", "thing:b", props)
        stored = db.execute(f"SELECT props FROM {table}").fetchone()[0]
        assert json.loads(stored) == props
    finally:
        db.close()


@pytest.fixture
def memory(tmp_path, monkeypatch):
    monkeypatch.setattr(RAGMemoryManager, "_spawn", lambda self, coro: coro.close())
    mgr = RAGMemoryManager(str(tmp_path))

    async def embed(text):
        return np.array([1.0, 0.0], dtype=np.float32)

    monkeypatch.setattr(mgr, "_embed", embed)
    yield mgr
    mgr._db.close()


def test_long_message_embeddings_still_match_full_content_hash(memory):
    async def run():
        text = "long message " * 1000
        await memory.add_to_channel_memory(
            "123", {"message_id": "456", "content": text}
        )
        assert await memory._embed_and_store("456", text)
        assert memory._db.execute(
            "SELECT embedding FROM vectors WHERE id='456'"
        ).fetchone()[0]

    asyncio.run(run())


def test_entity_salted_hash_does_not_prevent_embedding(memory):
    async def run():
        fid, _ = await memory.add_entity_fact("123", "prefers tea")
        assert await memory._embed_and_store(fid, "prefers tea")
        assert memory._db.execute(
            "SELECT embedding FROM vectors WHERE id=?", (fid,)
        ).fetchone()[0]

    asyncio.run(run())


def test_shared_metadata_updates_are_merged_together(memory):
    async def run():
        fid = await memory.add_shared_context(
            {"content": "a fact", "visibility": "shared"}
        )
        assert await memory.update_shared_context(
            fid,
            {"visibility": "admin_only", "tags": ["audit"], "expires_at": "2099-01-01"},
        )
        row = (await memory.list_shared_context())[0]
        assert row["visibility"] == "admin_only"
        assert row["tags"] == ["audit"]
        assert row["expires_at"] == "2099-01-01"

    asyncio.run(run())


def test_shared_content_edit_updates_hash_and_rejects_stale_embedding(memory):
    async def run():
        fid = await memory.add_shared_context({"content": "old fact"})
        before = memory._db.execute(
            "SELECT content_hash FROM vectors WHERE id=?", (fid,)
        ).fetchone()[0]
        assert await memory.update_shared_context(fid, {"content": "new fact"})
        after = memory._db.execute(
            "SELECT content_hash FROM vectors WHERE id=?", (fid,)
        ).fetchone()[0]
        assert after != before
        assert not await memory._embed_and_store(fid, "old fact")
        assert await memory._embed_and_store(fid, "new fact")

    asyncio.run(run())


@pytest.mark.parametrize("budget", [0, 1, 5])
def test_shared_context_respects_even_tiny_budgets(memory, budget):
    async def run():
        await memory.add_shared_context({"content": "a long durable fact"})
        assert await memory.get_relevant_shared_context(budget=budget) == []

    asyncio.run(run())
