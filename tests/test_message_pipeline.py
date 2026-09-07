"""The inbound path must not drop messages and must not answer one twice.

These are the two failure modes the production logs showed: 586 messages
dropped on a channel-lock timeout over four days, and a burst of identical
replies in one channel.
"""

import asyncio
import json
import sqlite3

import pytest

import message_pipeline
from message_pipeline import InboundDedup, ReplyQueue, RequestJournal, Watermarks


# --------------------------------------------------------------------------
# dedup
# --------------------------------------------------------------------------


def test_dedup_accepts_once_then_rejects():
    dedup = InboundDedup()
    assert dedup.check_and_add(111) is True
    assert dedup.check_and_add(111) is False
    assert dedup.check_and_add("111") is False
    assert dedup.check_and_add(222) is True


def test_dedup_ignores_missing_ids():
    """A synthetic message with no id is still real traffic."""
    dedup = InboundDedup()
    assert dedup.check_and_add(None) is True
    assert dedup.check_and_add(None) is True
    assert dedup.check_and_add("") is True


def test_dedup_is_bounded_and_evicts_oldest():
    dedup = InboundDedup(capacity=64)
    for i in range(200):
        dedup.check_and_add(i)
    assert len(dedup) <= 64
    # The newest ids survive, so redelivery of a recent message is still caught.
    assert dedup.check_and_add(199) is False


def test_dedup_forget_allows_reprocessing():
    dedup = InboundDedup()
    dedup.check_and_add(5)
    dedup.forget(5)
    assert dedup.check_and_add(5) is True


# --------------------------------------------------------------------------
# reply queue
# --------------------------------------------------------------------------


def _msg(mid, channel="c1"):
    class _M:
        def __init__(self):
            self.id = mid
            self.channel = type("Ch", (), {"id": channel})()

    return _M()


def test_queue_serializes_and_answers_everything():
    """A second message during a slow turn waits — it is not dropped."""

    async def scenario():
        seen = []
        gate = asyncio.Event()

        async def handler(message, content):
            seen.append((message.id, content))
            if message.id == 1:
                await gate.wait()

        q = ReplyQueue()
        q.bind(handler)
        assert q.submit("c1", _msg(1), "first", directed=True) == "started"
        await asyncio.sleep(0)
        assert q.submit("c1", _msg(2), "second", directed=True) == "queued"
        assert q.submit("c1", _msg(3), "third", directed=True) == "queued"
        gate.set()
        for _ in range(50):
            await asyncio.sleep(0)
            if len(seen) == 3:
                break
        assert [m for m, _ in seen] == [1, 2, 3]

    asyncio.run(scenario())


def test_queue_does_not_drop_directed_messages_under_load():
    """The old lock-timeout path dropped these outright."""

    async def scenario():
        seen = []
        gate = asyncio.Event()

        async def handler(message, content):
            if message.id == 0:
                await gate.wait()
            seen.append(message.id)

        q = ReplyQueue(max_directed=8)
        q.bind(handler)
        q.submit("c1", _msg(0), "blocker", directed=True)
        await asyncio.sleep(0)
        for i in range(1, 8):
            q.submit("c1", _msg(i), f"ping {i}", directed=True)
        gate.set()
        for _ in range(200):
            await asyncio.sleep(0)
            if len(seen) == 8:
                break
        assert sorted(seen) == list(range(8))

    asyncio.run(scenario())


def test_queue_coalesces_soft_chatter_to_one_turn():
    """Background chatter must not become one LLM turn per line."""

    async def scenario():
        seen = []
        gate = asyncio.Event()

        async def handler(message, content):
            if message.id == 0:
                await gate.wait()
            seen.append(message.id)

        q = ReplyQueue()
        q.bind(handler)
        q.submit("c1", _msg(0), "blocker", directed=True)
        await asyncio.sleep(0)
        assert q.submit("c1", _msg(1), "chatter", directed=False) == "queued"
        assert q.submit("c1", _msg(2), "chatter", directed=False) == "coalesced"
        assert q.submit("c1", _msg(3), "chatter", directed=False) == "coalesced"
        assert q.depth("c1") == 1
        gate.set()
        for _ in range(100):
            await asyncio.sleep(0)
            if len(seen) == 2:
                break
        # One turn for the whole burst, and it is the NEWEST line.
        assert seen == [0, 3]

    asyncio.run(scenario())


def test_queue_resubmitting_same_message_does_not_double_reply():
    """Gateway redelivery of a queued message must not queue it twice."""

    async def scenario():
        seen = []
        gate = asyncio.Event()

        async def handler(message, content):
            if message.id == 0:
                await gate.wait()
            seen.append(message.id)

        q = ReplyQueue()
        q.bind(handler)
        q.submit("c1", _msg(0), "blocker", directed=True)
        await asyncio.sleep(0)
        q.submit("c1", _msg(7), "hello", directed=True)
        assert q.submit("c1", _msg(7), "hello", directed=True) == "duplicate"
        assert q.depth("c1") == 1
        gate.set()
        for _ in range(100):
            await asyncio.sleep(0)
            if len(seen) == 2:
                break
        assert seen == [0, 7]

    asyncio.run(scenario())


def test_queue_deduplicates_running_messages_and_tracks_owned_ids():
    async def scenario():
        seen = []
        started = asyncio.Event()
        gate = asyncio.Event()

        async def handler(message, content):
            seen.append(message.id)
            started.set()
            await gate.wait()

        q = ReplyQueue()
        q.bind(handler)
        q.submit("c1", _msg(1), "first", directed=True)
        assert q.contains("c1", 1)
        assert not q.contains("other", 1)
        assert not q.contains("c1", None)
        await started.wait()
        assert q.contains("c1", "1")
        assert q.submit("c1", _msg(1), "replayed", directed=True) == "duplicate"
        assert q.depth("c1") == 0
        q.submit("c1", _msg(2), "second", directed=True)
        assert q.contains("c1", 2)
        gate.set()
        pump = q._channels["c1"].pump
        await pump
        assert seen == [1, 2]
        assert not q.contains("c1", 1)
        assert not q.contains("c1", 2)
        await q.close()

    asyncio.run(scenario())


def test_queue_survives_a_failing_turn():
    """One bad message must not silence the room."""

    async def scenario():
        seen = []

        async def handler(message, content):
            if message.id == 1:
                raise RuntimeError("provider exploded")
            seen.append(message.id)

        q = ReplyQueue()
        q.bind(handler)
        q.submit("c1", _msg(1), "boom", directed=True)
        q.submit("c1", _msg(2), "after", directed=True)
        for _ in range(100):
            await asyncio.sleep(0)
            if seen:
                break
        assert seen == [2]

    asyncio.run(scenario())


def test_queue_survives_a_cancelled_turn():
    """',stop' cancels one turn; queued traffic still gets answered."""

    async def scenario():
        seen = []
        started = asyncio.Event()

        async def handler(message, content):
            if message.id == 1:
                started.set()
                await asyncio.sleep(30)
            seen.append(message.id)

        q = ReplyQueue()
        q.bind(handler)
        q.submit("c1", _msg(1), "slow", directed=True)
        await started.wait()
        q.submit("c1", _msg(2), "next", directed=True)
        assert q.cancel_channel("c1") is True
        for _ in range(200):
            await asyncio.sleep(0)
            if seen:
                break
        assert seen == [2]

    asyncio.run(scenario())


def test_drop_soft_keeps_directed_pings():
    """Same-user interrupt must not let a queued watch line steal the next slot."""

    async def scenario():
        seen = []
        started = asyncio.Event()
        release = asyncio.Event()

        async def handler(message, content):
            if message.id == 1:
                started.set()
                await release.wait()
            seen.append(message.id)

        q = ReplyQueue()
        q.bind(handler)
        q.submit("c1", _msg(1), "running", directed=True)
        await started.wait()
        q.submit("c1", _msg(2), "watch chatter", directed=False)
        q.submit("c1", _msg(3), "hard ping", directed=True)
        assert q.drop_soft("c1") == 1
        release.set()
        for _ in range(200):
            await asyncio.sleep(0)
            if 3 in seen:
                break
        assert 2 not in seen
        assert 3 in seen

    asyncio.run(scenario())


def test_queue_channels_are_independent():
    """One slow room must not hold up another."""

    async def scenario():
        seen = []
        gate = asyncio.Event()

        async def handler(message, content):
            if message.channel.id == "slow":
                await gate.wait()
            seen.append(message.channel.id)

        q = ReplyQueue()
        q.bind(handler)
        q.submit("slow", _msg(1, "slow"), "x", directed=True)
        q.submit("fast", _msg(2, "fast"), "y", directed=True)
        for _ in range(50):
            await asyncio.sleep(0)
            if "fast" in seen:
                break
        assert "fast" in seen
        assert "slow" not in seen
        gate.set()
        for _ in range(50):
            await asyncio.sleep(0)
            if "slow" in seen:
                break
        assert "slow" in seen

    asyncio.run(scenario())


def test_queue_reports_active_and_depth():
    async def scenario():
        gate = asyncio.Event()

        async def handler(message, content):
            await gate.wait()

        q = ReplyQueue()
        q.bind(handler)
        assert q.any_active() is False
        q.submit("c1", _msg(1), "x", directed=True)
        await asyncio.sleep(0)
        assert q.active("c1") is True
        assert q.any_active() is True
        q.submit("c1", _msg(2), "y", directed=True)
        assert q.depth("c1") == 1
        gate.set()
        for _ in range(100):
            await asyncio.sleep(0)
            if not q.any_active():
                break
        assert q.any_active() is False

    asyncio.run(scenario())


def test_queue_bound_evicts_soft_before_directed():
    async def scenario():
        drops = []
        gate = asyncio.Event()

        async def handler(message, content):
            await gate.wait()

        q = ReplyQueue(
            max_directed=2,
            on_drop=lambda cid, e, why: drops.append((e.message_id, why)),
        )
        q.bind(handler)
        q.submit("c1", _msg(0), "blocker", directed=True)
        await asyncio.sleep(0)
        q.submit("c1", _msg(1), "soft", directed=False)
        q.submit("c1", _msg(2), "ping", directed=True)
        q.submit("c1", _msg(3), "ping", directed=True)
        # The soft entry is what gets evicted, not either ping.
        assert drops and drops[0][0] == "1"
        gate.set()

    asyncio.run(scenario())


def test_queue_directed_overflow_defers_only_new_requests():
    async def scenario():
        seen = []
        drops = []
        gate = asyncio.Event()
        started = asyncio.Event()

        async def handler(message, content):
            seen.append(message.id)
            started.set()
            await gate.wait()

        q = ReplyQueue(
            max_directed=2,
            on_drop=lambda cid, entry, why: drops.append((cid, entry.message_id, why)),
        )
        q.bind(handler)
        q.submit("c1", _msg(1), "running", directed=True)
        await started.wait()
        assert q.submit("c1", _msg(2), "waiting", directed=True) == "queued"
        assert q.submit("c1", _msg(3), "waiting", directed=True) == "queued"
        q._channels["c1"].queue[0].enqueued_at -= 999
        for mid in range(4, 14):
            assert q.submit("c1", _msg(mid), "overflow", directed=True) == "deferred"
            assert not q.contains("c1", mid)
            assert q.depth("c1") == 2
        assert drops == [("c1", str(mid), "deferred") for mid in range(4, 14)]
        assert q.contains("c1", 2)
        assert q.contains("c1", 3)
        assert q.submit("c1", _msg(2), "redelivery", directed=True) == "duplicate"
        gate.set()
        await q._channels["c1"].pump
        assert seen == [1, 2, 3]
        assert q.submit("c1", _msg(4), "retry", directed=True) == "started"
        await q._channels["c1"].pump
        assert seen == [1, 2, 3, 4]
        await q.close()

    asyncio.run(scenario())


def test_queue_soft_overflow_cannot_evict_directed_requests():
    async def scenario():
        drops = []

        async def handler(message, content):
            return None

        q = ReplyQueue(
            max_directed=1,
            on_drop=lambda cid, entry, why: drops.append((entry.message_id, why)),
        )
        q.bind(handler)
        q.submit("c1", _msg(1), "directed", directed=True)
        assert q.submit("c1", _msg(2), "soft", directed=False) == "dropped"
        assert drops == [("2", "queue full")]
        assert q.contains("c1", 1)
        assert not q.contains("c1", 2)
        await q.close()

    asyncio.run(scenario())


def test_queue_coalescing_reports_superseded_id_and_keeps_burst():
    async def scenario():
        drops = []

        async def handler(message, content):
            return None

        q = ReplyQueue(
            on_drop=lambda cid, entry, why: drops.append((entry.message_id, why))
        )
        q.bind(handler)
        first, second = _msg(1), _msg(2)
        q.submit("c1", first, "first", directed=False)
        assert q.submit("c1", second, "second", directed=False) == "coalesced"
        assert drops == [("1", "superseded")]
        assert not q.contains("c1", 1)
        assert q.contains("c1", 2)
        assert q._channels["c1"].queue[0].burst == [first, second]
        await q.close()

    asyncio.run(scenario())


def test_queue_retains_stale_directed_entries():
    async def scenario():
        seen = []
        drops = []
        gate = asyncio.Event()

        async def handler(message, content):
            await gate.wait()
            seen.append(message.id)

        q = ReplyQueue(
            max_age=10.0, on_drop=lambda cid, e, why: drops.append((e.message_id, why))
        )
        q.bind(handler)
        q.submit("c1", _msg(0), "blocker", directed=True)
        await asyncio.sleep(0)
        q.submit("c1", _msg(1), "old", directed=True)
        # Backdate the queued entry past max_age, then poke the queue.
        state = q._channels["c1"]  # noqa: SLF001 - white-box on purpose
        state.queue[0].enqueued_at -= 999
        q.submit("c1", _msg(2), "new", directed=True)
        assert not drops
        assert q.contains("c1", 1)
        gate.set()
        await state.pump
        assert seen == [0, 1, 2]
        await q.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("submit_new", [False, True])
def test_queue_expires_stale_soft_entries_at_submission_and_dispatch(submit_new):
    async def scenario():
        seen = []
        drops = []
        started = asyncio.Event()
        gate = asyncio.Event()

        async def handler(message, content):
            started.set()
            await gate.wait()
            seen.append(message.id)

        q = ReplyQueue(
            max_age=10,
            on_drop=lambda cid, entry, why: drops.append((entry.message_id, why)),
        )
        q.bind(handler)
        q.submit("c1", _msg(1), "running", directed=True)
        await started.wait()
        q.submit("c1", _msg(2), "old soft", directed=False)
        state = q._channels["c1"]
        state.queue[0].enqueued_at -= 999
        if submit_new:
            q.submit("c1", _msg(3), "new directed", directed=True)
            assert not q.contains("c1", 2)
        gate.set()
        await state.pump
        assert drops == [("2", "stale")]
        assert seen == ([1, 3] if submit_new else [1])
        await q.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("start_handlers", [False, True])
def test_queue_close_cancels_all_channels_without_respawning(start_handlers):
    async def scenario():
        seen = []
        finished = []
        pumps = []
        started = [asyncio.Event(), asyncio.Event()]

        async def handler(message, content):
            seen.append(message.id)
            started[message.id - 1].set()
            try:
                await asyncio.Event().wait()
            finally:
                finished.append(message.id)

        def track(task):
            pumps.append(task)
            return task

        q = ReplyQueue()
        q.bind(handler, task_factory=track)
        for mid in (1, 2):
            cid = f"c{mid}"
            q.submit(cid, _msg(mid, cid), "running", directed=True)
            q.submit(cid, _msg(mid + 10, cid), "waiting", directed=True)
        if start_handlers:
            await asyncio.gather(*(event.wait() for event in started))
        await q.close()
        await asyncio.sleep(0)
        assert len(pumps) == 2
        assert all(task.done() for task in pumps)
        assert sorted(seen) == ([1, 2] if start_handlers else [])
        assert sorted(finished) == sorted(seen)
        assert not q.any_active()
        assert q.stats()["channels_tracked"] == 0
        assert not q.contains("c1", 1)
        assert q.submit("c1", _msg(99), "after close", directed=True) == "dropped"
        await q.close()

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# durable request journal
# --------------------------------------------------------------------------


def test_journal_accept_stores_only_metadata_and_preserves_duplicates(tmp_path):
    journal = RequestJournal(tmp_path / "nested" / "requests.sqlite3")
    assert journal.accept(101, 202, 303, directed=True)
    initial = journal.get(101)
    assert initial == {
        "id": "101",
        "message_id": "101",
        "channel_id": "202",
        "author_id": "303",
        "status": "received",
        "directed": True,
        "attempts": 0,
        "effects_started": False,
        "response_id": None,
        "reason": "",
        "created_at": initial["created_at"],
        "updated_at": initial["created_at"],
    }
    assert not journal.accept("101", "other", "other", directed=False)
    assert journal.get("101") == initial
    journal.update(101, "delivered", effects_started=True, response_id=404)
    delivered = journal.get(101)
    assert delivered["response_id"] == "404"
    assert not journal.accept(101, 202)
    assert journal.get(101) == delivered
    assert journal.get("unknown") is None
    assert journal.pending() == []


def test_journal_pending_order_limit_and_effects_exclusion(tmp_path, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(message_pipeline.time, "time", lambda: now[0])
    journal = RequestJournal(tmp_path / "requests.sqlite3")
    statuses = (
        "received",
        "delivered",
        "queued",
        "suppressed",
        "deferred",
        "failed",
        "running",
        "superseded",
    )
    for index, status in enumerate(statuses, start=1):
        now[0] += 1
        journal.accept(index, "channel")
        journal.update(index, status)
    journal.accept(9, "channel")
    journal.update(9, "running", effects_started=True)
    expected = ["1", "3", "5", "7"]
    assert [record["message_id"] for record in journal.pending()] == expected
    assert [record["message_id"] for record in journal.pending(2)] == expected[:2]
    assert journal.pending(0) == []
    assert journal.pending(-1) == []


def test_journal_pending_cursor_reaches_rooms_beyond_a_busy_prefix(tmp_path):
    journal = RequestJournal(tmp_path / "requests.sqlite3")
    with journal._transaction(write=True) as connection:
        connection.executemany(
            """
            INSERT INTO requests (
                message_id, channel_id, status, created_at, updated_at
            ) VALUES (?, 'busy', 'queued', ?, ?)
            """,
            [(str(index), index, index) for index in range(1001)],
        )
    journal.accept("later", "available")
    first = journal.pending(1000)
    assert len(first) == 1000
    assert all(row["channel_id"] == "busy" for row in first)
    cursor = first[-1]["created_at"], first[-1]["message_id"]
    next_page = journal.pending(1000, after=cursor)
    assert [row["message_id"] for row in next_page] == ["1000", "later"]
    cursor = next_page[-1]["created_at"], next_page[-1]["message_id"]
    assert journal.pending(1000, after=cursor) == []
    assert journal.pending(1)[0]["message_id"] == "0"


def test_journal_pending_cursor_handles_ties_and_status_changes(tmp_path, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(message_pipeline.time, "time", lambda: now[0])
    journal = RequestJournal(tmp_path / "requests.sqlite3")
    for mid in ("20", "2", "10", "1"):
        journal.accept(mid, "channel")
    page = journal.pending(2)
    assert [row["message_id"] for row in page] == ["1", "10"]
    cursor = page[-1]["created_at"], page[-1]["message_id"]
    now[0] += 10
    journal.update("10", "delivered")
    journal.update("2", "deferred")
    assert [row["message_id"] for row in journal.pending(2, after=cursor)] == ["2", "20"]
    assert journal.get("2")["updated_at"] > cursor[0]


@pytest.mark.parametrize("snapshot", [0.0, 100.0])
def test_journal_pending_snapshot_bounds_sweep_without_excluding_boundary(
    tmp_path, monkeypatch, snapshot
):
    now = [snapshot - 1]
    monkeypatch.setattr(message_pipeline.time, "time", lambda: now[0])
    journal = RequestJournal(tmp_path / "requests.sqlite3")
    journal.accept("1", "channel")
    now[0] = snapshot
    journal.accept("2", "channel")
    journal.accept("3", "channel")
    page = journal.pending(1, before=snapshot)
    assert [row["message_id"] for row in page] == ["1"]
    cursor = page[-1]["created_at"], page[-1]["message_id"]

    now[0] = snapshot + 1
    journal.accept("4", "channel")
    journal.update("2", "deferred")
    page = journal.pending(100, after=cursor, before=snapshot)
    assert [row["message_id"] for row in page] == ["2", "3"]
    assert journal.get("2")["updated_at"] > snapshot
    cursor = page[-1]["created_at"], page[-1]["message_id"]
    assert journal.pending(100, after=cursor, before=snapshot) == []
    assert [row["message_id"] for row in journal.pending(before=snapshot)] == [
        "1", "2", "3"
    ]
    assert [row["message_id"] for row in journal.pending()] == ["1", "2", "3", "4"]


@pytest.mark.parametrize(
    "status",
    [
        "received", "queued", "deferred", "running",
        "delivered", "suppressed", "failed", "superseded",
    ],
)
@pytest.mark.parametrize("effects_started", [False, True])
def test_journal_restart_recovers_only_safe_unfinished_work(
    tmp_path, status, effects_started
):
    path = tmp_path / "requests.sqlite3"
    journal = RequestJournal(path)
    journal.accept(101, 202, 303, directed=True)
    if status == "running":
        journal.begin(101)
    journal.update(
        101,
        status,
        reason="original",
        effects_started=effects_started,
        response_id=404 if status == "delivered" else None,
    )
    original = journal.get(101)

    restarted = RequestJournal(path)
    assert restarted.get(101) == original
    restarted.recover()
    record = restarted.get(101)
    assert record["attempts"] == original["attempts"]
    assert record["directed"] is True
    assert record["effects_started"] is effects_started
    if status in ("delivered", "suppressed", "failed", "superseded"):
        assert record == original
        assert restarted.pending() == []
    elif effects_started:
        assert record["status"] == "failed"
        assert record["reason"] == "interrupted_after_effect"
        assert restarted.pending() == []
    else:
        assert record["status"] == "deferred"
        assert [row["message_id"] for row in restarted.pending()] == ["101"]
    restarted.recover()
    assert restarted.get(101) == record


def test_journal_begin_counts_attempts_and_requires_explicit_terminal_reset(tmp_path):
    journal = RequestJournal(tmp_path / "requests.sqlite3")
    journal.accept(1, 2)
    journal.begin(1)
    assert journal.get(1)["status"] == "running"
    assert journal.get(1)["attempts"] == 1
    journal.update(1, "deferred", reason="retry")
    journal.begin(1)
    assert journal.get(1)["attempts"] == 2
    journal.update(1, "running", directed=True, effects_started=True)
    with pytest.raises(ValueError, match="effects-started"):
        journal.begin(1)
    journal.update(1, "delivered", response_id=3)
    terminal = journal.get(1)
    with pytest.raises(ValueError, match="terminal"):
        journal.begin(1)
    assert journal.get(1) == terminal
    assert terminal["directed"] is True
    assert terminal["effects_started"] is True
    journal.update(1, "received", reason="edited_mention", effects_started=False)
    journal.begin(1)
    assert journal.get(1)["attempts"] == 3
    assert journal.get(1)["effects_started"] is False


def test_journal_transition_log_is_structured_and_contains_duration(
    tmp_path, caplog, monkeypatch
):
    now = [100.0]
    monkeypatch.setattr(message_pipeline.time, "time", lambda: now[0])
    caplog.set_level("INFO", logger="message_pipeline")
    journal = RequestJournal(tmp_path / "requests.sqlite3")
    journal.accept(101, 202)
    now[0] = 102.5
    journal.update(101, "deferred", reason="capacity", directed=True)
    event = json.loads(caplog.records[-1].getMessage().split(" ", 1)[1])
    assert event["message_id"] == "101"
    assert event["channel_id"] == "202"
    assert event["previous_status"] == "received"
    assert event["status"] == "deferred"
    assert event["duration_ms"] == 2500
    assert event["reason"] == "capacity"


def test_journal_retention_is_bounded_without_pruning_pending(tmp_path):
    journal = RequestJournal(tmp_path / "requests.sqlite3")
    terminal_statuses = ("delivered", "suppressed", "failed", "superseded")
    for status in ("received", "queued", "deferred", "running"):
        journal.accept(status, "channel")
        journal.update(status, status)
    with journal._transaction(write=True) as connection:
        connection.execute("UPDATE requests SET created_at = 0, updated_at = 0")
        connection.executemany(
            """
            INSERT INTO requests (
                message_id, channel_id, status, created_at, updated_at
            ) VALUES (?, 'channel', ?, ?, ?)
            """,
            [
                (
                    f"terminal-{index}",
                    terminal_statuses[index % 4],
                    index + 1,
                    index + 1,
                )
                for index in range(10_002)
            ],
        )
    journal.accept("new", "channel")
    journal.update("new", "delivered")
    with journal._transaction() as connection:
        count = connection.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
    assert count == 10_004
    assert {row["message_id"] for row in journal.pending()} == {
        "received", "queued", "deferred", "running"
    }
    assert journal.get("terminal-0") is None
    assert journal.get("terminal-2") is None
    assert journal.get("terminal-3") is not None
    assert journal.get("new")["status"] == "delivered"


def test_journal_database_errors_are_visible_and_never_reset_data(tmp_path):
    corrupt = tmp_path / "corrupt.sqlite3"
    original = b"not a SQLite database"
    corrupt.write_bytes(original)
    with pytest.raises(sqlite3.DatabaseError):
        RequestJournal(corrupt)
    assert corrupt.read_bytes() == original
    with pytest.raises(sqlite3.OperationalError):
        RequestJournal(tmp_path)


def test_journal_operations_close_connections_even_after_errors(tmp_path, monkeypatch):
    opened = []
    closed = []
    connect = sqlite3.connect

    class TrackingConnection(sqlite3.Connection):
        def close(self):
            closed.append(self)
            super().close()

    def tracked_connect(*args, **kwargs):
        connection = connect(*args, factory=TrackingConnection, **kwargs)
        opened.append(connection)
        return connection

    monkeypatch.setattr(message_pipeline.sqlite3, "connect", tracked_connect)
    journal = RequestJournal(tmp_path / "requests.sqlite3")
    journal.accept(1, 2)
    assert not journal.accept(1, 2)
    journal.get(1)
    journal.begin(1)
    journal.pending()
    journal.recover()
    journal.update(1, "delivered")
    with pytest.raises(ValueError):
        journal.begin(1)
    with pytest.raises(KeyError):
        journal.update("missing", "queued")
    assert opened == closed
    for connection in opened:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")
    journal.close()
    journal.close()
    assert journal.get(1)["status"] == "delivered"
    assert opened == closed


def test_journal_failed_write_rolls_back_and_invalid_status_is_rejected(
    tmp_path, monkeypatch
):
    journal = RequestJournal(tmp_path / "requests.sqlite3")
    journal.accept(1, 2)
    original = journal.get(1)

    def fail_prune(connection):
        raise sqlite3.OperationalError("write failed")

    monkeypatch.setattr(journal, "_prune", fail_prune)
    with pytest.raises(sqlite3.OperationalError, match="write failed"):
        journal.update(1, "delivered", response_id=3)
    assert journal.get(1) == original
    with pytest.raises(ValueError, match="Unknown request status"):
        journal.update(1, "invalid")
    assert journal.get(1) == original


# --------------------------------------------------------------------------
# watermarks
# --------------------------------------------------------------------------


def test_watermark_tracks_highest_id(tmp_path):
    wm = Watermarks(str(tmp_path / "wm.json"))
    wm.note("c1", 100)
    wm.note("c1", 50)  # older event must not move the mark backwards
    wm.note("c1", 200)
    assert wm.get("c1") == 200
    assert wm.get("nope") is None


def test_watermark_round_trips_to_disk(tmp_path):
    path = str(tmp_path / "wm.json")
    wm = Watermarks(path)
    wm.note("c1", 12345678901234567890)
    wm.save()
    again = Watermarks(path)
    again.load()
    assert again.get("c1") == 12345678901234567890


def test_watermark_load_tolerates_garbage(tmp_path):
    path = tmp_path / "wm.json"
    path.write_text("not json at all")
    wm = Watermarks(str(path))
    wm.load()
    assert len(wm) == 0


def test_watermark_is_bounded_keeping_recent_rooms(tmp_path):
    wm = Watermarks(str(tmp_path / "wm.json"), max_channels=32)
    for i in range(1, 200):
        wm.note(f"c{i}", i * 1000)
    assert len(wm) <= 32
    # Snowflakes are time-ordered, so the highest ids are the live rooms.
    assert wm.get("c199") == 199000


def test_watermark_ignores_bad_values(tmp_path):
    wm = Watermarks(str(tmp_path / "wm.json"))
    wm.note("c1", None)
    wm.note("c1", "abc")
    wm.note("", 5)
    assert len(wm) == 0


@pytest.mark.parametrize("bad", [0, -1])
def test_watermark_rejects_nonpositive(tmp_path, bad):
    wm = Watermarks(str(tmp_path / "wm.json"))
    wm.note("c1", bad)
    assert wm.get("c1") is None
