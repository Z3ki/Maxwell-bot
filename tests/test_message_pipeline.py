"""The inbound path must not drop messages and must not answer one twice.

These are the two failure modes the production logs showed: 586 messages
dropped on a channel-lock timeout over four days, and a burst of identical
replies in one channel.
"""

import asyncio

import pytest

from message_pipeline import InboundDedup, ReplyQueue, Watermarks


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


def test_queue_drops_stale_entries():
    async def scenario():
        drops = []
        gate = asyncio.Event()

        async def handler(message, content):
            await gate.wait()

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
        assert ("1", "stale") in drops
        gate.set()

    asyncio.run(scenario())


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
