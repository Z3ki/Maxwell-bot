"""Admission, isolation and restart state under contention and malformed input."""

import asyncio
import inspect
import json
import weakref
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from concurrency_safety import FairSemaphore, KeyedLocks, ToolConcurrency
from message_pipeline import RequestJournal, Watermarks


def _run(scenario):
    return asyncio.run(asyncio.wait_for(scenario(), timeout=5))


def test_lock_fetched_before_acquire_survives_idle_cache_eviction():
    async def scenario():
        locks = KeyedLocks(max_idle=16)
        original = locks.get("room")
        # No acquire has run yet. This is the gap created by wrapping
        # lock.acquire() in wait_for in the Discord admission path.
        for index in range(80):
            locks.get(f"other-room-{index}")
        assert locks.get("room") is original
        assert len(locks) <= 16
        await original.acquire()
        acquired = asyncio.Event()

        async def next_user():
            async with locks.get("room"):
                acquired.set()

        waiter = asyncio.create_task(next_user())
        await asyncio.sleep(0)
        assert not acquired.is_set()
        original.release()
        await waiter
        assert acquired.is_set()

    _run(scenario)


def test_periodic_idle_prune_cannot_split_externally_held_lock_references():
    locks = KeyedLocks()
    held_references = {key: locks.get(key) for key in ("room-a", "room-b")}
    assert locks.prune(all_idle=True) == 2
    assert len(locks) == 0
    assert all(locks.get(key) is lock for key, lock in held_references.items())


def test_lock_registry_reclaims_keys_without_callers_or_cache_entries():
    locks = KeyedLocks(max_idle=16)
    references = [weakref.ref(locks.get(f"room-{index}")) for index in range(1000)]
    locks.prune(all_idle=True)
    assert len(locks) == 0
    assert all(reference() is None for reference in references)


def test_fair_admission_load_survives_cancellations_and_capacity_shrink():
    async def scenario():
        gate = FairSemaphore(3)
        for index in range(3):
            await gate.acquire(1, key=f"holder-{index}", priority="user")
        order = []
        running = peak = 0
        all_waiting = asyncio.Event()

        # Python 3.11's wait_for schedules Condition.wait in a child task.
        # One event-loop turn does not guarantee that every caller has
        # entered the admission queue. Observe real condition waits instead
        # of depending on a particular interpreter's scheduling order.
        condition_wait = gate._cond.wait

        async def observe_condition_wait():
            if gate.waiting == len(tasks):
                all_waiting.set()
            return await condition_wait()

        gate._cond.wait = observe_condition_wait

        async def caller(room):
            nonlocal running, peak
            await gate.acquire(3, key=str(room), priority="user")
            try:
                running += 1
                peak = max(peak, running)
                order.append(room)
                await asyncio.sleep(0)
            finally:
                running -= 1
                await gate.release()

        tasks = [
            asyncio.create_task(caller(room)) for room in range(32) for _ in range(6)
        ]
        await all_waiting.wait()
        assert gate.waiting == len(tasks)
        gate._cond.wait = condition_wait
        cancelled = tasks[::7]
        for task in cancelled:
            task.cancel()
        await asyncio.gather(*cancelled, return_exceptions=True)
        assert gate.waiting == len(tasks) - len(cancelled)
        await gate.set_capacity(1)
        await gate.release()
        await gate.release()
        await asyncio.sleep(0)
        assert gate.active == 1
        assert not order
        await gate.release()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        assert sum(
            isinstance(result, asyncio.CancelledError) for result in results
        ) == len(cancelled)
        assert all(
            result is None or isinstance(result, asyncio.CancelledError)
            for result in results
        )
        assert len(order) == len(tasks) - len(cancelled)
        assert len(set(order[:32])) == 32
        assert peak == 1
        assert gate.active == 0 and gate.waiting == 0

    _run(scenario)


def test_tool_deadline_includes_admission_wait_and_closes_unstarted_coroutine():
    async def scenario():
        gates = ToolConcurrency(web=1)
        gate = gates.gate("web")
        await gate.acquire()
        invoked = []

        async def operation():
            invoked.append(True)
            return 42

        coroutine = operation()
        task = asyncio.create_task(gates.run("web", coroutine, timeout=0.01))
        # The outer deadline only bounds a broken implementation's hang.
        # The tool's own timeout must expire without releasing the holder.
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), timeout=0.2)
        assert task.done()
        assert inspect.getcoroutinestate(coroutine) == inspect.CORO_CLOSED
        assert not invoked
        assert gates.stats()["web"]["waiting"] == 0
        assert gates.stats()["web"]["free"] == 0
        gate.release()
        assert await gates.run("web", operation(), timeout=1) == 42

    _run(scenario)


def test_tool_deadline_after_admission_cancels_operation_and_releases_budget():
    async def scenario():
        gates = ToolConcurrency(shell=1)
        stopped = asyncio.Event()

        async def operation():
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        with pytest.raises(TimeoutError):
            await gates.run("shell", operation(), timeout=0.01)
        assert stopped.is_set()
        assert gates.stats()["shell"]["free"] == 1
        assert (
            await gates.run("shell", asyncio.sleep(0, result="next"), timeout=1)
            == "next"
        )

    _run(scenario)


@pytest.mark.parametrize("operation_type", ["coroutine", "future"])
def test_cancelling_tool_admission_disposes_owned_operation_and_waiter(operation_type):
    async def scenario():
        gates = ToolConcurrency(provider=1)
        gate = gates.gate("provider")
        await gate.acquire()
        operation = (
            asyncio.sleep(0, result=42)
            if operation_type == "coroutine"
            else asyncio.get_running_loop().create_future()
        )
        task = asyncio.create_task(gates.run("provider", operation, timeout=1))
        await asyncio.sleep(0)
        assert gates.stats()["provider"]["waiting"] == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        if operation_type == "coroutine":
            assert inspect.getcoroutinestate(operation) == inspect.CORO_CLOSED
        else:
            assert operation.cancelled()
        assert gates.stats()["provider"]["waiting"] == 0
        assert gates.stats()["provider"]["free"] == 0
        gate.release()

    _run(scenario)


def test_tool_exception_releases_own_budget_while_other_budget_is_saturated():
    async def scenario():
        gates = ToolConcurrency(media=1, web=1)
        await gates.gate("media").acquire()

        async def broken():
            raise RuntimeError("upstream failed")

        with pytest.raises(RuntimeError, match="upstream failed"):
            await gates.run("web", broken(), timeout=1)
        assert gates.stats()["web"]["free"] == 1
        assert gates.stats()["media"]["free"] == 0
        assert await gates.run("web", asyncio.sleep(0, result=42), timeout=1) == 42
        gates.gate("media").release()

    _run(scenario)


@pytest.mark.parametrize("value", [0, -7, None, {}, [], "garbage", float("inf")])
def test_watermark_restore_rejects_invalid_message_ids(tmp_path, value):
    path = tmp_path / "watermarks.json"
    path.write_text(json.dumps({"channels": {"bad": value, "good": "500"}}))
    marks = Watermarks(str(path))
    marks.load()
    assert marks.get("bad") is None
    assert marks.get("good") == 500
    assert len(marks) == 1


def test_watermark_restore_keeps_channel_bound_and_recent_rooms(tmp_path):
    path = tmp_path / "watermarks.json"
    path.write_text(json.dumps({"channels": {str(i): i + 1 for i in range(1000)}}))
    marks = Watermarks(str(path), max_channels=32)
    marks.load()
    assert len(marks) <= 32
    assert marks.get("999") == 1000
    assert marks.get("0") is None


def test_watermark_reload_never_moves_live_channel_backwards(tmp_path):
    path = tmp_path / "watermarks.json"
    path.write_text(json.dumps({"channels": {"room": "100", "   ": "600"}}))
    marks = Watermarks(str(path))
    marks.note("room", 500)
    marks.load()
    assert marks.get("room") == 500
    assert len(marks) == 1
    marks.save()
    restored = Watermarks(str(path))
    restored.load()
    assert restored.get("room") == 500


def test_concurrent_journal_receipts_claim_message_once_and_preserve_owner(tmp_path):
    journal = RequestJournal(tmp_path / "requests.sqlite3")
    ready = Barrier(8)

    def accept(index):
        ready.wait(timeout=3)
        return index, journal.accept(123, index + 1, index + 100, directed=True)

    with ThreadPoolExecutor(max_workers=8) as executor:
        outcomes = list(executor.map(accept, range(8)))
    winners = [index for index, inserted in outcomes if inserted]
    assert len(winners) == 1
    record = journal.get(123)
    assert record["channel_id"] == str(winners[0] + 1)
    assert record["author_id"] == str(winners[0] + 100)
    assert record["status"] == "received" and record["attempts"] == 0
    assert [row["message_id"] for row in journal.pending()] == ["123"]


def test_concurrent_journal_attempts_are_atomic_and_survive_restart(tmp_path):
    path = tmp_path / "requests.sqlite3"
    journal = RequestJournal(path)
    journal.accept(123, 456, 789, directed=True)

    def attempt(_index):
        journal.begin(123)

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(attempt, range(80)))
    assert journal.get(123)["attempts"] == 80
    restored = RequestJournal(path)
    restored.recover()
    record = restored.get(123)
    assert record["attempts"] == 80
    assert record["status"] == "deferred"
    assert record["channel_id"] == "456" and record["author_id"] == "789"
