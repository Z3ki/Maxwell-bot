"""State and concurrency regressions. All external I/O is mocked."""

import asyncio
import json
import sqlite3

import pytest

from concurrency_safety import ChannelWorkQueues, KeyedLocks
from context_budget import TIER_ORDER, allocate
from inbox import InboxStore
from knowledge_graph import KnowledgeGraph


@pytest.mark.parametrize("raw", ['{"items":', "[]", '{"items": {}}'])
def test_inbox_mutation_preserves_corrupt_file(tmp_path, raw):
    async def run():
        store = InboxStore(str(tmp_path))
        store.path.write_text(raw)
        with pytest.raises(ValueError):
            await store.upsert({"id": "new"})
        assert store.path.read_text() == raw

    asyncio.run(run())


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
