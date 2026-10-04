"""Bot adapters must preserve request isolation and asynchronous cleanup."""

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from bot import MaxwellBot, _await_task_done
from message_pipeline import ReplyQueue, RequestJournal


def _run(scenario):
    return asyncio.run(asyncio.wait_for(scenario(), timeout=5))


def _message(mid, author):
    return SimpleNamespace(
        id=mid,
        channel=SimpleNamespace(id=22),
        author=SimpleNamespace(id=author),
    )


def _bot(tmp_path):
    bot = SimpleNamespace(
        _request_journal=RequestJournal(tmp_path / "requests.sqlite3"),
        _active_requests={},
        _active_request_user={},
        _active_request_messages={},
        _cancel_watch_debounce=Mock(),
    )
    bot._record_request_outcome = lambda message, status, reason: (
        MaxwellBot._record_request_outcome(bot, message, status, reason)
    )
    bot._reply_queue = ReplyQueue(
        on_drop=lambda cid, entry, reason: MaxwellBot._on_reply_queue_drop(
            bot, cid, entry, reason
        )
    )
    return bot


def _submit(bot, message):
    journal = bot._request_journal
    journal.accept(message.id, message.channel.id, message.author.id, directed=True)
    journal.update(message.id, "queued")
    return bot._reply_queue.submit("22", message, "question", directed=True)


@pytest.mark.parametrize("replacements", [2, 8])
def test_repeated_same_user_interrupt_joins_cleanup_and_preserves_other_user(
    tmp_path, replacements
):
    async def scenario():
        bot = _bot(tmp_path)
        journal = bot._request_journal
        started, cleaning, release, cleaned = (
            asyncio.Event(),
            asyncio.Event(),
            asyncio.Event(),
            asyncio.Event(),
        )
        answered = []

        async def handler(message, content):
            journal.begin(message.id)
            current = asyncio.current_task()
            bot._active_requests["22"] = current
            bot._active_request_user["22"] = str(message.author.id)
            bot._active_request_messages["22"] = message
            try:
                if message.id == 101:
                    started.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        cleaning.set()
                        await release.wait()
                        cleaned.set()
                answered.append(message.id)
                journal.update(message.id, "delivered", effects_started=True)
            finally:
                if bot._active_requests.get("22") is current:
                    bot._active_requests.pop("22", None)
                    bot._active_request_user.pop("22", None)
                    bot._active_request_messages.pop("22", None)

        bot._reply_queue.bind(handler)
        _submit(bot, _message(101, 11))
        await started.wait()
        _submit(bot, _message(201, 12))
        _submit(bot, _message(102, 11))
        replacement_messages = []
        for index in range(replacements):
            message = _message(103 + index, 11)
            replacement_messages.append(message)
            MaxwellBot._interrupt_same_user(bot, message)
            assert _submit(bot, message) == "queued"
            if index == 0:
                await cleaning.wait()
        # The older generation remains alive only to release its resources.
        # Further messages must not inject another cancellation into cleanup.
        predecessor = bot._active_requests["22"]
        assert not predecessor.done()
        release.set()
        await bot._reply_queue._channels["22"].pump
        assert cleaned.is_set()
        assert answered == [201, replacement_messages[-1].id]
        superseded = [101, 102, *(m.id for m in replacement_messages[:-1])]
        assert all(journal.get(mid)["status"] == "superseded" for mid in superseded)
        assert journal.get(201)["status"] == "delivered"
        assert journal.get(replacement_messages[-1].id)["status"] == "delivered"
        assert bot._reply_queue.outstanding == 0
        assert not bot._active_requests and not bot._active_request_user
        await bot._reply_queue.close()

    _run(scenario)


def test_replacement_from_other_user_never_stops_running_users_request(tmp_path):
    async def scenario():
        bot = _bot(tmp_path)
        journal = bot._request_journal
        started, release = asyncio.Event(), asyncio.Event()
        answered = []

        async def handler(message, content):
            journal.begin(message.id)
            current = asyncio.current_task()
            bot._active_requests["22"] = current
            bot._active_request_user["22"] = str(message.author.id)
            bot._active_request_messages["22"] = message
            try:
                if message.id == 101:
                    started.set()
                    await release.wait()
                answered.append(message.id)
                journal.update(message.id, "delivered", effects_started=True)
            finally:
                bot._active_requests.pop("22", None)
                bot._active_request_user.pop("22", None)
                bot._active_request_messages.pop("22", None)

        bot._reply_queue.bind(handler)
        _submit(bot, _message(101, 11))
        await started.wait()
        _submit(bot, _message(201, 12))
        _submit(bot, _message(301, 13))
        replacement = _message(202, 12)
        predecessor = bot._active_requests["22"]
        MaxwellBot._interrupt_same_user(bot, replacement)
        assert not predecessor.cancelling()
        assert journal.get(101)["status"] == "running"
        assert journal.get(201)["status"] == "superseded"
        _submit(bot, replacement)
        release.set()
        await bot._reply_queue._channels["22"].pump
        assert answered == [101, 301, 202]
        assert all(journal.get(mid)["status"] == "delivered" for mid in answered)
        await bot._reply_queue.close()

    _run(scenario)


@pytest.mark.parametrize("outcome", ["succeeded", "failed", "cancelled"])
def test_waiting_for_predecessor_accepts_all_completed_outcomes(outcome):
    async def scenario():
        async def predecessor():
            await asyncio.sleep(0)
            if outcome == "failed":
                raise RuntimeError("predecessor failed")
            if outcome == "cancelled":
                raise asyncio.CancelledError
            return 42

        task = asyncio.create_task(predecessor())
        assert await _await_task_done(task) is None
        assert task.done()
        if outcome == "cancelled":
            assert task.cancelled()

    _run(scenario)


@pytest.mark.parametrize("cancel_mode", ["caller", "deadline"])
@pytest.mark.parametrize("predecessor_cancelled", [False, True])
def test_cancelled_waiter_propagates_without_cancelling_predecessor_or_its_cleanup(
    cancel_mode, predecessor_cancelled
):
    async def scenario():
        started, cleaning, release, cleaned = (
            asyncio.Event(),
            asyncio.Event(),
            asyncio.Event(),
            asyncio.Event(),
        )

        async def predecessor():
            started.set()
            try:
                if predecessor_cancelled:
                    await asyncio.Event().wait()
                else:
                    await release.wait()
            finally:
                cleaning.set()
                await release.wait()
                cleaned.set()

        previous = asyncio.create_task(predecessor())
        await started.wait()
        if predecessor_cancelled:
            previous.cancel()
            await cleaning.wait()
        waiter = asyncio.create_task(_await_task_done(previous))
        await asyncio.sleep(0)
        if cancel_mode == "caller":
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
        else:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(waiter, timeout=0.01)
        assert not previous.done()
        assert not cleaned.is_set()
        release.set()
        await _await_task_done(previous)
        assert previous.done() and cleaned.is_set()
        assert previous.cancelled() is predecessor_cancelled

    _run(scenario)
