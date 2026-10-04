"""Behavioral cancellation and shutdown regressions for independent users."""

import asyncio
from types import SimpleNamespace

import pytest

from concurrency_safety import ChannelWorkQueues
from message_pipeline import ReplyQueue


def _message(mid, channel="room", author="user"):
    return SimpleNamespace(
        id=mid,
        channel=SimpleNamespace(id=channel),
        author=SimpleNamespace(id=author),
    )


def _run(scenario):
    # Broken cancellation must fail the test rather than hang the test suite.
    return asyncio.run(asyncio.wait_for(scenario(), timeout=5))


def test_callback_cancellation_preserves_other_users_waiting_in_room():
    async def scenario():
        queues = ChannelWorkQueues()
        started, release = asyncio.Event(), asyncio.Event()
        seen = []

        async def first():
            started.set()
            await release.wait()
            raise asyncio.CancelledError

        async def second():
            seen.append("second user")
            return 42

        first_task = asyncio.create_task(queues.submit(1, 2, first))
        await started.wait()
        second_task = asyncio.create_task(queues.submit(1, 2, second))
        await asyncio.sleep(0)
        release.set()
        results = await asyncio.gather(first_task, second_task, return_exceptions=True)
        assert isinstance(results[0], asyncio.CancelledError)
        assert results[1] == 42
        assert seen == ["second user"]
        assert not queues._workers and not queues._queues
        await queues.close()

    _run(scenario)


def test_cancelling_running_submitter_stops_only_its_callback():
    async def scenario():
        queues = ChannelWorkQueues()
        started, stopped = asyncio.Event(), asyncio.Event()

        async def first():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        first_task = asyncio.create_task(queues.submit(1, 2, first))
        await started.wait()
        second_task = asyncio.create_task(
            queues.submit(1, 2, lambda: asyncio.sleep(0, result="next user"))
        )
        await asyncio.sleep(0)
        first_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first_task
        await stopped.wait()
        assert await second_task == "next user"
        assert not queues._workers and not queues._queues
        await queues.close()

    _run(scenario)


def test_cancelling_pending_submitter_releases_capacity_immediately():
    async def scenario():
        queues = ChannelWorkQueues(max_pending=1)
        started, release = asyncio.Event(), asyncio.Event()
        seen = []

        async def work(name):
            seen.append(name)
            if name == "running":
                started.set()
                await release.wait()
            return name

        first = asyncio.create_task(queues.submit(1, 2, lambda: work("running")))
        await started.wait()
        cancelled = asyncio.create_task(queues.submit(1, 2, lambda: work("cancelled")))
        await asyncio.sleep(0)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        replacement = asyncio.create_task(
            queues.submit(1, 2, lambda: work("replacement"))
        )
        await asyncio.sleep(0)
        assert not replacement.done()
        release.set()
        assert await asyncio.gather(first, replacement) == ["running", "replacement"]
        assert seen == ["running", "replacement"]
        await queues.close()

    _run(scenario)


@pytest.mark.parametrize("failure", ["raise", "non_awaitable"])
def test_callback_construction_failure_is_reported_without_losing_next_user(failure):
    async def scenario():
        queues = ChannelWorkQueues()

        def broken():
            if failure == "raise":
                raise RuntimeError("callback construction failed")
            return 123

        first = asyncio.create_task(queues.submit(1, 2, broken))
        second = asyncio.create_task(
            queues.submit(1, 2, lambda: asyncio.sleep(0, result="survived"))
        )
        results = await asyncio.gather(first, second, return_exceptions=True)
        assert isinstance(results[0], RuntimeError if failure == "raise" else TypeError)
        assert results[1] == "survived"
        await queues.close()

    _run(scenario)


@pytest.mark.parametrize("queue_type", [ChannelWorkQueues, ReplyQueue])
@pytest.mark.parametrize("close_mode", ["concurrent", "cancelled_caller"])
def test_shutdown_preserves_async_cleanup_and_can_be_joined_again(
    queue_type, close_mode
):
    async def scenario():
        queue = queue_type()
        started = asyncio.Event()
        cleaning, cleanup_release, cleaned = (
            asyncio.Event(),
            asyncio.Event(),
            asyncio.Event(),
        )

        async def work(*args):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await cleanup_release.wait()
                cleaned.set()

        if isinstance(queue, ReplyQueue):
            queue.bind(work)
            queue.submit("room", _message(1), "work", directed=True)
            queue.submit("room", _message(2), "pending", directed=True)
            submitters = []
        else:
            submitters = [asyncio.create_task(queue.submit(1, 2, work))]
        await started.wait()
        if isinstance(queue, ChannelWorkQueues):
            submitters.append(asyncio.create_task(queue.submit(1, 2, work)))
            await asyncio.sleep(0)

        first_close = asyncio.create_task(queue.close())
        await cleaning.wait()
        if close_mode == "cancelled_caller":
            first_close.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first_close
        second_close = asyncio.create_task(queue.close())
        await asyncio.sleep(0)
        assert not cleaned.is_set()
        assert not second_close.done()
        cleanup_release.set()
        if close_mode == "concurrent":
            await first_close
        await second_close
        assert cleaned.is_set()
        results = await asyncio.gather(*submitters, return_exceptions=True)
        assert all(isinstance(result, asyncio.CancelledError) for result in results)
        if isinstance(queue, ReplyQueue):
            assert queue.outstanding == 0
            assert queue.stats()["channels_tracked"] == 0
        else:
            assert not queue._workers and not queue._queues
        await queue.close()

    _run(scenario)


def test_guilds_with_same_channel_number_still_run_independently():
    async def scenario():
        queues = ChannelWorkQueues()
        started = [asyncio.Event(), asyncio.Event()]
        release = asyncio.Event()

        async def callback(index):
            started[index].set()
            await release.wait()
            return index

        tasks = [
            asyncio.create_task(queues.submit(guild, 99, lambda i=i: callback(i)))
            for i, guild in enumerate((1, 2))
        ]
        await asyncio.gather(*(event.wait() for event in started))
        release.set()
        assert await asyncio.gather(*tasks) == [0, 1]
        await queues.close()

    _run(scenario)


@pytest.mark.parametrize("queue_type", [ChannelWorkQueues, ReplyQueue])
def test_shutdown_joins_cleanup_of_a_request_that_was_already_cancelled(queue_type):
    async def scenario():
        queue = queue_type()
        started, cleaning, release, cleaned = (
            asyncio.Event(),
            asyncio.Event(),
            asyncio.Event(),
            asyncio.Event(),
        )

        async def work(*args):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()
                cleaned.set()

        if isinstance(queue, ReplyQueue):
            queue.bind(work)
            queue.submit("room", _message(1), "work", directed=True)
            submitter = None
        else:
            submitter = asyncio.create_task(queue.submit(1, 2, work))
        await started.wait()
        if isinstance(queue, ReplyQueue):
            assert queue.cancel_channel("room")
        else:
            submitter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await submitter
        await cleaning.wait()
        close = asyncio.create_task(queue.close())
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(close), timeout=0.01)
        assert not close.done()
        release.set()
        await close
        assert cleaned.is_set()

    _run(scenario)


def test_work_queue_load_keeps_fifo_after_many_pending_cancellations():
    async def scenario():
        queues = ChannelWorkQueues(max_pending=32)
        rooms, turns = 12, 24
        seen = {room: [] for room in range(rooms)}
        active = dict.fromkeys(seen, 0)
        peak = dict.fromkeys(seen, 0)

        async def callback(room, turn):
            active[room] += 1
            peak[room] = max(peak[room], active[room])
            seen[room].append(turn)
            await asyncio.sleep(0)
            active[room] -= 1
            return room, turn

        tasks = {
            (room, turn): asyncio.create_task(
                queues.submit(1, room, lambda r=room, t=turn: callback(r, t))
            )
            for room in range(rooms)
            for turn in range(turns)
        }
        await asyncio.sleep(0)
        cancelled = {key for key in tasks if key[1] % 4 == 0}
        for key in cancelled:
            tasks[key].cancel()
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        for key, result in zip(tasks, results, strict=True):
            if key in cancelled:
                assert isinstance(result, asyncio.CancelledError)
            else:
                assert result == key
        assert all(peak[room] == 1 and active[room] == 0 for room in seen)
        assert all(seen[room] == [t for t in range(turns) if t % 4] for room in seen)
        assert not queues._workers and not queues._queues
        await queues.close()

    _run(scenario)


@pytest.mark.parametrize("failure", ["raise", "non_awaitable"])
def test_reply_handler_construction_failure_releases_capacity_and_next_turn(failure):
    async def scenario():
        queue = ReplyQueue(max_outstanding=2)
        seen = []

        async def successful(message):
            seen.append(message.id)

        def handler(message, content):
            if message.id == 1:
                if failure == "raise":
                    raise RuntimeError("handler construction failed")
                return 123
            return successful(message)

        queue.bind(handler)
        queue.submit("room", _message(1), "broken", directed=True)
        queue.submit("room", _message(2), "valid", directed=True)
        await queue._channels["room"].pump
        assert seen == [2]
        assert queue.outstanding == 0
        assert queue.stats()["channels_tracked"] == 0
        assert (
            queue.submit("room", _message(3), "capacity reusable", directed=True)
            == "started"
        )
        await queue._channels["room"].pump
        assert seen == [2, 3]
        await queue.close()

    _run(scenario)


def test_directed_reply_replaces_soft_turn_when_global_capacity_is_full():
    async def scenario():
        drops, seen = [], []
        started, release = asyncio.Event(), asyncio.Event()
        queue = ReplyQueue(
            max_outstanding=2,
            on_drop=lambda cid, entry, reason: drops.append((entry.message.id, reason)),
        )

        async def handler(message, content):
            seen.append(message.id)
            if message.id == 1:
                started.set()
                await release.wait()

        queue.bind(handler)
        queue.submit("room", _message(1), "running", directed=True)
        await started.wait()
        queue.submit("room", _message(2), "soft", directed=False)
        assert queue.full
        assert queue.submit("room", _message(3), "directed", directed=True) == "queued"
        assert drops == [(2, "queue full")]
        assert queue.outstanding == 2
        release.set()
        await queue._channels["room"].pump
        assert seen == [1, 3]
        assert queue.outstanding == 0
        await queue.close()

    _run(scenario)


def test_same_user_interrupt_preserves_other_users_and_refreshed_duplicate():
    async def scenario():
        queue = ReplyQueue(max_outstanding=5)
        started, stopped = asyncio.Event(), asyncio.Event()
        seen = []

        async def handler(message, content):
            if message.id == 1:
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    stopped.set()
            seen.append((message.id, message.author.id, content))

        queue.bind(handler)
        queue.submit("room", _message(1, author="a"), "active", directed=True)
        await started.wait()
        for mid, author in ((2, "a"), (3, "b"), (4, "a"), (5, "c")):
            queue.submit(
                "room", _message(mid, author=author), "original", directed=True
            )
        assert (
            queue.submit("room", _message(3, author="b"), "edited", directed=True)
            == "duplicate"
        )
        dropped = queue.drop_author("room", "a")
        assert [entry.message.id for entry in dropped] == [2, 4]
        assert queue.outstanding == 3
        assert queue.cancel_channel("room")
        await queue._channels["room"].pump
        assert stopped.is_set()
        assert seen == [(3, "b", "edited"), (5, "c", "original")]
        assert queue.outstanding == 0
        await queue.close()

    _run(scenario)


def test_reply_queue_load_retries_overflow_once_without_losing_fifo_or_capacity():
    async def scenario():
        rooms, turns = 24, 18
        queue = ReplyQueue(max_directed=4, max_outstanding=64)
        seen = {room: [] for room in range(rooms)}
        active = dict.fromkeys(seen, 0)
        peak = dict.fromkeys(seen, 0)
        deferred = []

        async def handler(message, content):
            room, turn = message.channel.id, message.id % turns
            active[room] += 1
            peak[room] = max(peak[room], active[room])
            seen[room].append(turn)
            try:
                await asyncio.sleep(0)
                if room == 0 and turn == 1:
                    raise RuntimeError("one provider failure during load")
            finally:
                active[room] -= 1

        queue.bind(handler)
        for room in range(rooms):
            for turn in range(turns):
                message = _message(
                    room * turns + turn, channel=room, author=f"user-{turn}"
                )
                outcome = queue.submit(str(room), message, "request", directed=True)
                if outcome == "deferred":
                    deferred.append((str(room), message))
                else:
                    assert outcome in {"started", "queued"}
                    assert (
                        queue.submit(str(room), message, "redelivery", directed=True)
                        == "duplicate"
                    )
        assert queue.outstanding == 64
        assert len(deferred) == rooms * turns - 64
        await asyncio.gather(*(state.pump for state in queue._channels.values()))
        assert queue.outstanding == 0
        for channel, message in deferred:
            assert queue.submit(channel, message, "retry", directed=True) == "started"
            await queue._channels[channel].pump
        assert all(seen[room] == list(range(turns)) for room in seen)
        assert all(peak[room] == 1 and active[room] == 0 for room in seen)
        assert queue.outstanding == 0
        assert queue.stats()["channels_tracked"] == 0
        await queue.close()

    _run(scenario)
