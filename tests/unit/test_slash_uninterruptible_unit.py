"""/maxwell runs are not interruptible by the same user's later messages.

A /maxwell turn answers a deferred interaction. Before this fix a later
message from the same user went through the same-user interrupt path
(``_should_interrupt_inflight`` -> ``_interrupt_same_user``), which cancelled
the slash turn or dropped it from the queue as "superseded", so the
interaction stayed on "Maxwell is thinking..." forever.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from bot import MaxwellBot
from message_pipeline import ReplyQueue, RequestJournal
from user_install import (
    INTERACTION_FALLBACK_TEXT,
    INTERACTION_STOPPED_TEXT,
    UserInstallSession,
    handle_user_install_interaction,
    resolve_unanswered_interaction,
)

GUILD_CHANNEL = SimpleNamespace(id=22, guild=SimpleNamespace(id=33))
DM_CHANNEL = SimpleNamespace(id=22, guild=None, recipient=SimpleNamespace(id=11))


def _run(scenario):
    return asyncio.run(asyncio.wait_for(scenario(), timeout=5))


def _message(mid, author=11, *, channel=GUILD_CHANNEL, slash=False):
    message = SimpleNamespace(
        id=mid,
        channel=channel,
        guild=getattr(channel, "guild", None),
        author=SimpleNamespace(id=author, bot=False),
    )
    if slash:
        message.user_install = True
    return message


def _bot(tmp_path):
    bot = SimpleNamespace(
        _request_journal=RequestJournal(tmp_path / "requests.sqlite3"),
        _active_requests={},
        _active_request_user={},
        _active_request_messages={},
        _cancel_watch_debounce=Mock(),
        _message_addresses_self=lambda _message: True,
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


def _bind(bot, blocking, started, release, answered):
    journal = bot._request_journal

    async def handler(message, content):
        journal.begin(message.id)
        current = asyncio.current_task()
        bot._active_requests["22"] = current
        bot._active_request_user["22"] = str(message.author.id)
        bot._active_request_messages["22"] = message
        try:
            if message.id == blocking:
                started.set()
                await release.wait()
            answered.append(message.id)
            journal.update(message.id, "delivered", effects_started=True)
        finally:
            if bot._active_requests.get("22") is current:
                bot._active_requests.pop("22", None)
                bot._active_request_user.pop("22", None)
                bot._active_request_messages.pop("22", None)

    bot._reply_queue.bind(handler)


def _interrupt_like_on_message(bot, message):
    # Mirrors bot_parts/messages.py::_on_message_impl's interrupt gate.
    if MaxwellBot._should_interrupt_inflight(bot, message):
        MaxwellBot._interrupt_same_user(bot, message)
        return True
    return False


@pytest.mark.parametrize("channel", [GUILD_CHANNEL, DM_CHANNEL], ids=["guild", "dm"])
def test_running_slash_turn_is_not_cancelled_by_same_user_message(tmp_path, channel):
    async def scenario():
        bot = _bot(tmp_path)
        started, release, answered = asyncio.Event(), asyncio.Event(), []
        _bind(bot, 101, started, release, answered)
        _submit(bot, _message(101, channel=channel, slash=True))
        await started.wait()
        running = bot._active_requests["22"]

        follow_up = _message(102, channel=channel)
        assert MaxwellBot._should_interrupt_inflight(bot, follow_up) is False
        # Even a direct interrupt call must leave the slash turn alone.
        MaxwellBot._interrupt_same_user(bot, follow_up)
        await bot._request_journal.drain()
        assert not running.cancelling()
        assert bot._request_journal.get(101)["status"] == "running"

        assert _submit(bot, follow_up) == "queued"
        release.set()
        await bot._reply_queue._channels["22"].pump
        assert answered == [101, 102]
        assert bot._request_journal.get(101)["status"] == "delivered"
        assert bot._request_journal.get(102)["status"] == "delivered"
        await bot._reply_queue.close()

    _run(scenario)


def test_queued_slash_turn_is_not_dropped_by_same_user_interrupt(tmp_path):
    async def scenario():
        bot = _bot(tmp_path)
        started, release, answered = asyncio.Event(), asyncio.Event(), []
        _bind(bot, 101, started, release, answered)
        _submit(bot, _message(101))
        await started.wait()
        running = bot._active_requests["22"]
        _submit(bot, _message(102, slash=True))

        follow_up = _message(103)
        assert _interrupt_like_on_message(bot, follow_up) is True
        await bot._request_journal.drain()
        # The normal run is still interrupted; the queued slash turn stays.
        assert running.cancelling()
        assert bot._request_journal.get(101)["status"] == "superseded"
        assert bot._request_journal.get(102)["status"] != "superseded"
        _submit(bot, follow_up)
        await bot._reply_queue._channels["22"].pump
        assert answered == [102, 103]
        assert bot._request_journal.get(102)["status"] == "delivered"
        await bot._reply_queue.close()

    _run(scenario)


def test_slash_turn_running_still_lets_newer_message_replace_older_queued_one(
    tmp_path,
):
    async def scenario():
        bot = _bot(tmp_path)
        started, release, answered = asyncio.Event(), asyncio.Event(), []
        _bind(bot, 101, started, release, answered)
        _submit(bot, _message(101, slash=True))
        await started.wait()
        running = bot._active_requests["22"]
        _submit(bot, _message(102))

        newest = _message(103)
        assert _interrupt_like_on_message(bot, newest) is True
        await bot._request_journal.drain()
        assert not running.cancelling()
        assert bot._request_journal.get(101)["status"] == "running"
        assert bot._request_journal.get(102)["status"] == "superseded"
        _submit(bot, newest)
        release.set()
        await bot._reply_queue._channels["22"].pump
        assert answered == [101, 103]
        await bot._reply_queue.close()

    _run(scenario)


def test_normal_run_is_still_interrupted_by_same_user_message(tmp_path):
    async def scenario():
        bot = _bot(tmp_path)
        started, release, answered = asyncio.Event(), asyncio.Event(), []
        _bind(bot, 101, started, release, answered)
        _submit(bot, _message(101))
        await started.wait()
        running = bot._active_requests["22"]

        follow_up = _message(102)
        assert _interrupt_like_on_message(bot, follow_up) is True
        await bot._request_journal.drain()
        assert running.cancelling()
        assert bot._request_journal.get(101)["status"] == "superseded"
        _submit(bot, follow_up)
        await bot._reply_queue._channels["22"].pump
        assert answered == [102]
        await bot._reply_queue.close()

    _run(scenario)


def test_other_users_message_never_touches_a_slash_turn(tmp_path):
    async def scenario():
        bot = _bot(tmp_path)
        started, release, answered = asyncio.Event(), asyncio.Event(), []
        _bind(bot, 101, started, release, answered)
        _submit(bot, _message(101, slash=True))
        await started.wait()
        assert MaxwellBot._should_interrupt_inflight(bot, _message(102, 12)) is False
        release.set()
        await bot._reply_queue._channels["22"].pump
        assert answered == [101]
        await bot._reply_queue.close()

    _run(scenario)


class _Response:
    def __init__(self):
        self.deferred = False

    def is_done(self):
        return self.deferred

    async def defer(self, *, ephemeral=False):
        self.deferred = True


class _Followup:
    def __init__(self):
        self.payloads = []

    async def send(self, content=None, **kwargs):
        self.payloads.append({"content": content, **kwargs})
        return SimpleNamespace(id=len(self.payloads), content=content)


def _interaction():
    return SimpleNamespace(
        id=99,
        type=2,
        channel_id=555,
        channel=SimpleNamespace(name="general", id=555),
        guild=None,
        user=SimpleNamespace(id=7, name="zek", display_name="zek", bot=False),
        data={
            "name": "maxwell",
            "options": [
                {"name": "prompt", "type": 3, "value": "hello"},
                {"name": "visibility", "type": 3, "value": "private"},
            ],
            "resolved": {},
        },
        response=_Response(),
        followup=_Followup(),
    )


def _slash_bot(on_message):
    spawned = []
    bot = SimpleNamespace(on_message=on_message, _spawn_detached=spawned.append)
    return bot, spawned


def test_slash_interaction_gets_final_reply_when_turn_raises():
    async def on_message(_message):
        raise RuntimeError("boom")

    async def scenario():
        bot, spawned = _slash_bot(on_message)
        interaction = _interaction()
        assert await handle_user_install_interaction(bot, interaction) is True
        await spawned[0]
        return interaction.followup.payloads

    payloads = asyncio.run(scenario())
    assert [row["content"] for row in payloads] == [INTERACTION_FALLBACK_TEXT]
    assert payloads[0]["ephemeral"] is True


def test_slash_interaction_gets_final_reply_when_turn_is_cancelled():
    entered = asyncio.Event

    async def scenario():
        started = entered()

        async def on_message(_message):
            started.set()
            await asyncio.Event().wait()

        bot, spawned = _slash_bot(on_message)
        interaction = _interaction()
        assert await handle_user_install_interaction(bot, interaction) is True
        task = asyncio.ensure_future(spawned[0])
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return interaction.followup.payloads

    payloads = asyncio.run(scenario())
    assert [row["content"] for row in payloads] == [INTERACTION_STOPPED_TEXT]


def test_answered_slash_interaction_gets_no_extra_fallback():
    async def on_message(message):
        await message.channel.send("the answer")
        raise RuntimeError("late failure")

    async def scenario():
        bot, spawned = _slash_bot(on_message)
        interaction = _interaction()
        await handle_user_install_interaction(bot, interaction)
        await spawned[0]
        return interaction.followup.payloads

    assert [row["content"] for row in asyncio.run(scenario())] == ["the answer"]


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("queued", []),
        ("running", []),
        ("deferred", []),
        ("delivered", []),
        ("failed", [INTERACTION_FALLBACK_TEXT]),
        ("suppressed", [INTERACTION_FALLBACK_TEXT]),
        ("superseded", [INTERACTION_FALLBACK_TEXT]),
    ],
)
def test_resolver_only_answers_requests_nothing_else_will_answer(status, expected):
    interaction = _interaction()
    message = SimpleNamespace(
        user_install=True,
        _session=UserInstallSession(interaction, visibility="private"),
    )

    async def state(_message):
        return {"status": status}

    bot = SimpleNamespace(_request_state=state)
    asyncio.run(resolve_unanswered_interaction(bot, message))
    assert [row["content"] for row in interaction.followup.payloads] == expected


def test_resolver_ignores_normal_messages():
    message = SimpleNamespace(id=1, _session=None)
    assert (
        asyncio.run(resolve_unanswered_interaction(SimpleNamespace(), message)) is False
    )
