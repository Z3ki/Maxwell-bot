"""Durable Discord admission, recovery and full-turn completion contracts."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from bot import MaxwellBot, _current_inbound
from concurrency_safety import KeyedLocks
from message_pipeline import InboundDedup, ReplyQueue, RequestJournal, Watermarks


USER = SimpleNamespace(id=999, display_name="Maxwell", name="maxwell")


def _bot(path, *, capacity=2):
    bot = object.__new__(MaxwellBot)
    bot._connection = SimpleNamespace(user=USER)
    bot._control = {
        "store_memory": False,
        "process_images": False,
        "process_audio": False,
        "error_replies": False,
        "inbound_retry_delay_seconds": 0,
    }
    bot.command_prefix = ","
    bot._request_journal = RequestJournal(str(path / "requests.sqlite3"))
    bot._watermarks = Watermarks(str(path / "watermarks.json"))
    bot._recovery_cursors = {}
    bot._recovery_lock = asyncio.Lock()
    bot._inbound_processing = set()
    bot._inbound_dedup = InboundDedup()
    bot._active_requests = {}
    bot._active_request_user = {}
    bot._replying_channels = set()
    bot._inflight_context = {}
    bot._watch_debounce = {}
    bot._blacklist = set()
    bot._stop_until = {}
    bot._cooldowns = {}
    bot._channel_locks = KeyedLocks()
    bot._load_control = lambda: None
    bot._is_admin = lambda _uid: False
    bot._solo_blocks = lambda _message: False
    bot.clear_message_taint = lambda _message: None
    bot._update_recent_users = lambda *_args: None
    bot._maybe_schedule_context_extraction = lambda _message: None
    bot._reset_partner_reply_budget_for_human = lambda _message: None
    bot._ensure_reply_chain_resolved = AsyncMock()
    bot._respect_slowmode = AsyncMock()
    bot._mark_bot_sent = lambda _channel: None
    bot._reply_queue = ReplyQueue(
        max_directed=capacity, on_drop=bot._on_reply_queue_drop
    )
    bot._reply_queue.bind(bot._run_reliable_turn)
    bot._channels_for_test = {}
    bot.get_channel = lambda cid: bot._channels_for_test.get(int(cid))
    bot.fetch_channel = AsyncMock(side_effect=bot.get_channel)

    async def answer(message, _content):
        await bot._send_with_slowmode(message.channel, "answer", reply_to=message)

    bot._handle_message = answer
    setting = bot._inbound_setting
    bot._inbound_setting = lambda key, default, low, high: (
        0 if key == "inbound_retry_delay_seconds" else setting(key, default, low, high)
    )
    return bot


def _message(bot, mid=101, cid=22, *, directed=True, content="question"):
    channel = bot._channels_for_test.get(cid)
    if channel is None:
        channel = SimpleNamespace(id=cid, name="chat", sent=[], messages={})

        async def send(content=None, **_kwargs):
            sent = SimpleNamespace(id=10000 + len(channel.sent), content=content)
            channel.sent.append(sent)
            return sent

        channel.send = send
        channel.fetch_message = AsyncMock(side_effect=lambda mid: channel.messages[mid])
        bot._channels_for_test[cid] = channel
    message = SimpleNamespace(
        id=mid,
        content=content,
        channel=channel,
        author=SimpleNamespace(id=11, display_name="Alice", bot=False),
        mentions=[USER] if directed else [],
        guild=SimpleNamespace(id=33),
        reference=None,
        attachments=[],
        embeds=[],
        stickers=[],
        components=[],
        poll=None,
        mention_everyone=False,
        role_mentions=[],
        type=SimpleNamespace(name="default"),
    )
    message.reply = channel.send
    channel.messages[mid] = message
    return message


async def _drain(bot):
    tasks = [
        state.pump
        for state in list(bot._reply_queue._channels.values())
        if state.pump is not None
    ]
    if tasks:
        await asyncio.gather(*tasks)


def test_receipt_is_durable_before_watermark_and_work(tmp_path, monkeypatch):
    bot = _bot(tmp_path)
    message = _message(bot)
    seen = []
    original = bot._watermarks.note

    def note(cid, mid):
        assert bot._request_journal.get(mid)["status"] == "received"
        seen.append(mid)
        original(cid, mid)

    monkeypatch.setattr(bot._watermarks, "note", note)

    async def run():
        await bot.on_message(message)
        await _drain(bot)
        await bot.on_message(message)

    asyncio.run(run())
    assert seen == ["101"]
    row = bot._request_state(message)
    assert row["status"] == "delivered"
    assert row["response_id"] == "10000"
    assert row["attempts"] == 1
    assert len(message.channel.sent) == 1


def test_failed_journal_insert_never_advances_watermark(tmp_path, monkeypatch):
    bot = _bot(tmp_path)
    message = _message(bot)

    def unavailable(*_args, **_kwargs):
        raise OSError("journal unavailable")

    monkeypatch.setattr(bot._request_journal, "accept", unavailable)
    asyncio.run(bot.on_message(message))
    assert bot._watermarks.get(22) is None
    assert message.channel.sent == []


@pytest.mark.parametrize(
    ("control", "reason"),
    [
        ({"bot_enabled": False}, "bot_disabled"),
        ({"blocked_channels": ["22"]}, "blocked_channel"),
        ({"allowed_channels": ["44"]}, "channel_not_allowed"),
        ({"ignore_users": ["11"]}, "ignored_author"),
        ({"reply_mentions": False}, "mention_replies_disabled"),
        ({"reply_to_bots": False}, "bot_author"),
    ],
)
def test_gate_returns_have_terminal_reason(tmp_path, control, reason):
    bot = _bot(tmp_path)
    bot._control.update(control)
    message = _message(bot)
    if reason == "bot_author":
        message.author.bot = True
    asyncio.run(bot.on_message(message))
    row = bot._request_state(message)
    assert (row["status"], row["reason"]) == ("suppressed", reason)


def test_new_same_user_pings_overflow_durably_without_cancelling(tmp_path):
    bot = _bot(tmp_path)
    messages = [_message(bot, mid) for mid in range(101, 111)]
    handled = []

    async def run():
        started, release = asyncio.Event(), asyncio.Event()

        async def answer(message, _content):
            handled.append(message.id)
            if message.id == 101:
                started.set()
                await release.wait()
            await bot._send_with_slowmode(message.channel, "answer")

        bot._handle_message = answer
        await bot.on_message(messages[0])
        await started.wait()
        for message in messages[1:]:
            await bot.on_message(message)
        assert bot._request_state(messages[0])["status"] == "running"
        assert bot._request_state(messages[-1])["status"] == "deferred"
        assert bot._reply_queue.depth(22) <= 2
        release.set()
        await _drain(bot)
        for _ in range(10):
            await bot._retry_pending_inbound()
            await _drain(bot)

    asyncio.run(run())
    assert handled == list(range(101, 111))
    assert all(bot._request_state(m)["status"] == "delivered" for m in messages)


def test_explicit_stop_supersedes_active_queued_and_persisted(tmp_path):
    bot = _bot(tmp_path, capacity=1)
    messages = [_message(bot, mid) for mid in range(101, 105)]

    async def run():
        started = asyncio.Event()

        async def work(message, _content):
            bot._mark_request_effect(message)
            started.set()
            await asyncio.Event().wait()

        bot._handle_message = work
        await bot.on_message(messages[0])
        await started.wait()
        for message in messages[1:]:
            await bot.on_message(message)
        stop = _message(bot, 999, content=",stop")
        await bot.on_message(stop)
        await _drain(bot)
        await bot._retry_pending_inbound()

    asyncio.run(run())
    assert all(bot._request_state(m)["status"] == "superseded" for m in messages)


def test_retry_before_effect_is_bounded_and_bypasses_receipt_dedup(tmp_path):
    bot = _bot(tmp_path)
    message = _message(bot)
    calls = []

    async def failing(msg, _content):
        calls.append(msg.id)
        raise TimeoutError

    bot._handle_message = failing

    async def run():
        await bot.on_message(message)
        await _drain(bot)
        assert bot._request_state(message)["status"] == "deferred"
        await bot._retry_pending_inbound()
        await _drain(bot)
        await bot._retry_pending_inbound()

    asyncio.run(run())
    assert calls == [101, 101]
    assert bot._request_state(message)["status"] == "failed"


def test_failure_after_effect_never_replays(tmp_path):
    bot = _bot(tmp_path)
    message = _message(bot)
    calls = []

    async def uncertain(msg, _content):
        bot._mark_request_effect(msg)
        calls.append(msg.id)
        raise TimeoutError

    bot._handle_message = uncertain

    async def run():
        await bot.on_message(message)
        await _drain(bot)
        bot._request_journal.recover()
        await bot._retry_pending_inbound()
        await bot.on_message(message)

    asyncio.run(run())
    assert calls == [101]
    assert bot._request_state(message)["status"] == "failed"
    assert (
        bot._request_state(message)["reason"] == "uncertain_after_effect:TimeoutError"
    )


def test_empty_output_retries_then_sends_confirmed_fallback(tmp_path):
    bot = _bot(tmp_path)
    bot._control["error_replies"] = True
    message = _message(bot)
    bot._handle_message = AsyncMock()

    async def run():
        await bot.on_message(message)
        await _drain(bot)
        await bot._retry_pending_inbound()
        await _drain(bot)

    asyncio.run(run())
    assert bot._handle_message.await_count == 2
    assert bot._request_state(message)["status"] == "delivered"
    assert len(message.channel.sent) == 1
    assert "couldn't finish" in message.channel.sent[0].content


def test_forbidden_send_is_not_delivery_or_replayed(tmp_path):
    bot = _bot(tmp_path)
    message = _message(bot)
    message.channel.send = AsyncMock(
        side_effect=discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden"), "Missing permissions"
        )
    )
    message.reply = message.channel.send

    async def run():
        await bot.on_message(message)
        await _drain(bot)
        await bot._retry_pending_inbound()

    asyncio.run(run())
    assert bot._request_state(message)["status"] == "failed"
    assert bot._request_state(message)["response_id"] is None
    assert message.channel.send.await_count == 1


def test_deadline_covers_preparation_and_cleans_typing_context(tmp_path):
    bot = _bot(tmp_path)
    message = _message(bot)
    bot._inbound_setting = lambda key, default, low, high: (
        0.02 if key == "live_turn_timeout_seconds" else default
    )
    typing = SimpleNamespace(__aexit__=AsyncMock())

    async def preparation(msg, content):
        state = bot._begin_inflight_context(msg, content)
        state["live_typing"] = typing
        bot._replying_channels.add("22")
        await asyncio.Event().wait()

    bot._handle_message = preparation

    async def run():
        await bot.on_message(message)
        await _drain(bot)
        assert _current_inbound.get() is None

    asyncio.run(run())
    assert bot._request_state(message)["status"] == "deferred"
    assert bot._inflight_context == {}
    assert bot._active_requests == {}
    assert bot._replying_channels == set()
    typing.__aexit__.assert_awaited_once()


def test_preparation_exception_cleans_up_and_records_failure(tmp_path):
    bot = _bot(tmp_path)
    message = _message(bot)

    async def preparation(msg, content):
        bot._begin_inflight_context(msg, content)
        bot._replying_channels.add("22")
        raise ValueError("invalid preparation")

    bot._handle_message = preparation

    async def run():
        await bot.on_message(message)
        await _drain(bot)

    asyncio.run(run())
    assert bot._request_state(message)["status"] == "failed"
    assert bot._active_requests == {}
    assert bot._inflight_context == {}


def test_new_edited_mention_dispatches_once_and_never_reanswers(tmp_path):
    bot = _bot(tmp_path)
    before = _message(bot, directed=False, content="ambient")
    bot._request_journal.accept(before.id, 22, 11)
    bot._request_journal.update(before.id, "suppressed", reason="not_addressed")
    after = _message(bot, content="<@999> answer me")

    async def run():
        await bot._maybe_reply_to_edited_mention(before, after)
        await bot._maybe_reply_to_edited_mention(before, after)
        await _drain(bot)
        await bot._maybe_reply_to_edited_mention(before, after)

    asyncio.run(run())
    assert len(after.channel.sent) == 1
    assert bot._request_state(after)["status"] == "delivered"


def test_edited_mentions_can_be_disabled(tmp_path):
    bot = _bot(tmp_path)
    bot._control["respond_to_edited_mentions"] = False
    before = _message(bot, directed=False)
    after = _message(bot, content="<@999> question")
    asyncio.run(bot._maybe_reply_to_edited_mention(before, after))
    assert bot._request_state(after) == {}


def test_unresolved_reply_parent_retries_instead_of_becoming_chatter(tmp_path):
    bot = _bot(tmp_path)
    message = _message(bot, directed=False)
    message.reference = SimpleNamespace(message_id=55, resolved=None)
    parent = SimpleNamespace(author=USER, id=55, reference=None)
    calls = 0

    async def fetch(mid):
        nonlocal calls
        if mid == 55:
            calls += 1
            if calls == 1:
                raise TimeoutError
            return parent
        return message

    message.channel.fetch_message = fetch

    async def run():
        await bot.on_message(message)
        assert bot._request_state(message)["status"] == "deferred"
        await bot._retry_pending_inbound()
        await _drain(bot)

    asyncio.run(run())
    assert bot._request_state(message)["status"] == "delivered"
    assert calls == 2


def test_sleep_is_explicit_suppression_not_promised_deferral(tmp_path):
    bot = _bot(tmp_path)
    message = _message(bot)
    bot._sleep_notified_at = {}
    bot._is_sleeping = lambda: (True, 120)
    bot._request_journal.accept(message.id, 22, 11, directed=True)
    assert asyncio.run(bot._check_sleep_gate(message)) is False
    assert bot._request_state(message)["reason"] == "sleep_policy"
    assert bot._request_state(message)["status"] == "suppressed"
    assert "ping me again" in message.channel.sent[0].content


def test_recovery_pages_oldest_first_all_rooms_with_fetch_fallback(tmp_path):
    bot = _bot(tmp_path)
    bot._control["gap_recovery_max_messages"] = 2
    history_calls = []
    for cid in range(22, 64):
        messages = [_message(bot, cid * 100 + i, cid) for i in range(1, 6)]
        channel = messages[0].channel
        bot._watermarks.note(cid, cid * 100)

        async def history(*, limit, after, oldest_first, rows=messages):
            history_calls.append((rows[0].channel.id, after.id, oldest_first))
            for old in [m for m in rows if m.id > after.id][:limit]:
                yield old

        channel.history = history
    channels = dict(bot._channels_for_test)
    bot.fetch_channel = AsyncMock(side_effect=lambda cid: channels[cid])
    bot.get_channel = lambda _cid: None
    bot._capture_recovery_snapshot()

    async def run():
        for _ in range(16):
            await bot._recover_missed_messages(settle=False)
            await _drain(bot)

    asyncio.run(run())
    assert len(history_calls) == 126
    assert all(call[2] for call in history_calls)
    assert bot._recovery_cursors == {}
    assert all(bot._watermarks.get(cid) == cid * 100 + 5 for cid in channels)
    assert bot.fetch_channel.await_count == 126


def test_disconnect_snapshot_survives_new_live_high_watermark(tmp_path):
    bot = _bot(tmp_path)
    old, missed, live = [_message(bot, mid) for mid in (100, 101, 200)]
    bot._watermarks.note(22, old.id)

    async def history(*, limit, after, oldest_first):
        assert after.id == 100
        assert oldest_first
        yield missed
        yield live

    live.channel.history = history

    async def run():
        await bot.on_disconnect()
        await bot.on_message(live)
        assert bot._watermarks.get(22) == 100
        await bot._recover_missed_messages(settle=False)
        await _drain(bot)

    asyncio.run(run())
    assert bot._request_state(missed)["status"] == "delivered"
    assert bot._watermarks.get(22) == 200
    assert len(live.channel.sent) == 2


def test_recovery_does_not_advance_past_failed_durable_accept(tmp_path, monkeypatch):
    bot = _bot(tmp_path)
    message = _message(bot)
    bot._watermarks.note(22, 100)
    bot._capture_recovery_snapshot()

    async def history(**_kwargs):
        yield message

    message.channel.history = history
    monkeypatch.setattr(
        bot._request_journal,
        "accept",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk unavailable")),
    )
    asyncio.run(bot._recover_missed_messages(settle=False))
    assert bot._watermarks.get(22) == 100
    assert bot._recovery_cursors["22"] == 100


def test_restart_drains_accepted_request_but_not_uncertain_tool(tmp_path):
    original = _bot(tmp_path)
    original._request_journal.accept(101, 22, 11, directed=True)
    original._request_journal.accept(102, 22, 11, directed=True)
    original._request_journal.update(102, "running", effects_started=True)
    restarted = _bot(tmp_path)
    message = _message(restarted, 101)
    uncertain = _message(restarted, 102)
    restarted._request_journal.recover()

    async def run():
        await restarted._retry_pending_inbound()
        await _drain(restarted)

    asyncio.run(run())
    assert restarted._request_state(message)["status"] == "delivered"
    assert restarted._request_state(uncertain)["status"] == "failed"
    assert len(message.channel.sent) == 1


def test_restart_does_not_reset_exhausted_retry_budget(tmp_path):
    bot = _bot(tmp_path)
    message = _message(bot)
    journal = bot._request_journal
    journal.accept(message.id, 22, 11, directed=True)
    journal.begin(message.id)
    journal.begin(message.id)
    journal.recover()
    bot._handle_message = AsyncMock()

    async def run():
        await bot._retry_pending_inbound()
        await _drain(bot)

    asyncio.run(run())
    bot._handle_message.assert_not_awaited()
    assert bot._request_state(message)["status"] == "failed"
    assert bot._request_state(message)["attempts"] == 2


def test_concurrent_rooms_record_only_their_own_delivery(tmp_path):
    bot = _bot(tmp_path)
    first, second = _message(bot, 101, 22), _message(bot, 102, 23)

    async def run():
        await asyncio.gather(bot.on_message(first), bot.on_message(second))
        await _drain(bot)
        assert _current_inbound.get() is None

    asyncio.run(run())
    assert bot._request_state(first)["status"] == "delivered"
    assert bot._request_state(second)["status"] == "delivered"
    assert len(first.channel.sent) == len(second.channel.sent) == 1


def test_queued_request_rechecks_disabled_policy(tmp_path):
    bot = _bot(tmp_path)
    message = _message(bot)
    bot._request_journal.accept(message.id, 22, 11, directed=True)
    bot._request_journal.update(message.id, "queued")
    bot._control["bot_enabled"] = False
    bot._handle_message = AsyncMock()
    asyncio.run(bot._run_reliable_turn(message, message.content))
    bot._handle_message.assert_not_awaited()
    assert bot._request_state(message)["reason"] == "bot_disabled"


def test_overlapping_recovery_scans_are_serialized(tmp_path):
    bot = _bot(tmp_path)
    message = _message(bot)
    bot._watermarks.note(22, 100)
    bot._capture_recovery_snapshot()
    calls = []

    async def run():
        entered, release = asyncio.Event(), asyncio.Event()

        async def history(**kwargs):
            calls.append(kwargs["after"].id)
            entered.set()
            await release.wait()
            yield message

        message.channel.history = history
        scan = asyncio.create_task(bot._recover_missed_messages(settle=False))
        await entered.wait()
        await bot._recover_missed_messages(settle=False)
        release.set()
        await scan
        await _drain(bot)

    asyncio.run(run())
    assert calls == [100]
    assert bot._request_state(message)["status"] == "delivered"


def test_llm_trace_has_inbound_message_id(tmp_path):
    bot = _bot(tmp_path)
    bot.config = SimpleNamespace(DATA_DIR=str(tmp_path))
    bot._trace_lock = asyncio.Lock()
    message = _message(bot)
    asyncio.run(bot._record_llm_trace(message, {"stage": "test"}))
    traces = json.loads((tmp_path / "llm_traces.json").read_text())
    assert traces[-1]["message_id"] == "101"
    assert traces[-1]["channel_id"] == "22"


def test_reliability_logs_have_ids_not_message_contents(tmp_path, caplog):
    bot = _bot(tmp_path)
    message = _message(bot, content="private-inbound-content-sentinel")

    async def run():
        await bot.on_message(message)
        await _drain(bot)

    with caplog.at_level("INFO"):
        asyncio.run(run())
    assert "mid=101" in caplog.text
    assert "stage=delivered" in caplog.text
    assert "private-inbound-content-sentinel" not in caplog.text


def test_delivery_preserves_status_and_records_partial_diagnostic(tmp_path):
    bot = _bot(tmp_path)
    message = _message(bot)
    bot._request_journal.accept(message.id, 22, 11, directed=True)
    bot._mark_request_effect(message)
    bot._record_delivery(message, SimpleNamespace(id=12345))
    bot._record_request_outcome(
        message, "suppressed", reason="model_no_response:already_answered"
    )
    assert bot._request_state(message)["status"] == "delivered"
    bot._record_request_outcome(message, "delivered", reason="partial_delivery")
    row = bot._request_state(message)
    assert row["status"] == "delivered"
    assert row["response_id"] == "12345"
    assert row["reason"] == "partial_delivery"


@pytest.mark.parametrize(
    ("reason", "status"),
    [
        ("deferred", "deferred"),
        ("stale", "suppressed"),
        ("queue full", "suppressed"),
        ("superseded", "suppressed"),
        ("channel cleared", "superseded"),
    ],
)
def test_queue_drop_reasons_preserve_durable_overflow(tmp_path, reason, status):
    bot = _bot(tmp_path)
    message = _message(bot)
    bot._request_journal.accept(message.id, 22, 11, directed=True)
    bot._on_reply_queue_drop("22", SimpleNamespace(message=message), reason)
    assert bot._request_state(message)["status"] == status


def test_explicit_command_is_checkpointed_before_execution(tmp_path):
    bot = _bot(tmp_path)
    message = _message(bot, content=",stop")
    sends = []

    async def send(content, **_kwargs):
        assert bot._request_state(message)["effects_started"] is True
        sends.append(content)
        return SimpleNamespace(id=12345)

    message.channel.send = send
    asyncio.run(bot.on_message(message))
    assert sends == ["nothing to stop"]
    row = bot._request_state(message)
    assert row["status"] == "suppressed"
    assert row["reason"] == "command"
    assert row["effects_started"] is True


def test_restart_mid_gap_keeps_conservative_persisted_cursor(tmp_path):
    first = _bot(tmp_path)
    first._control["gap_recovery_max_messages"] = 2
    first._watermarks.note(22, 100)
    first._watermarks.save()
    first._capture_recovery_snapshot()
    backlog = [_message(first, mid) for mid in (101, 102, 103, 200)]
    history_starts = []

    async def history(*, limit, after, oldest_first):
        assert oldest_first
        history_starts.append(after.id)
        for message in [m for m in backlog if m.id > after.id][:limit]:
            yield message

    backlog[0].channel.history = history

    async def before_restart():
        await first.on_message(backlog[-1])
        await _drain(first)
        await first._recover_missed_messages(settle=False)
        await _drain(first)

    asyncio.run(before_restart())
    assert first._watermarks.get(22) == 102

    restarted = _bot(tmp_path)
    restarted._control["gap_recovery_max_messages"] = 2
    restarted._channels_for_test[22] = backlog[0].channel
    restarted._watermarks.load()
    restarted._capture_recovery_snapshot()
    restarted._request_journal.recover()

    async def after_restart():
        await restarted._recover_missed_messages(settle=False)
        await _drain(restarted)

    asyncio.run(after_restart())
    assert history_starts == [100, 102]
    assert restarted._request_state(backlog[2])["status"] == "delivered"
    # The newer live message was already delivered before the crash.
    assert len(backlog[0].channel.sent) == 4


def test_pending_sweep_reaches_new_rooms_beyond_busy_first_page(tmp_path):
    bot = _bot(tmp_path)
    journal = bot._request_journal
    for mid in range(1, 1002):
        journal.accept(mid, 22, 11, directed=True)
        bot._inbound_processing.add(str(mid))
    waiting = _message(bot, 1002, 23)
    journal.accept(waiting.id, 23, 11, directed=True)

    async def run():
        await bot._retry_pending_inbound()
        assert bot._request_state(waiting)["status"] == "received"
        newer = _message(bot, 1003, 24)
        journal.accept(newer.id, 24, 11, directed=True)
        await bot._retry_pending_inbound()
        await _drain(bot)
        assert bot._request_state(waiting)["status"] == "delivered"
        assert bot._request_state(newer)["status"] == "received"
        # A fresh, bounded sweep includes the newly arrived room.
        await bot._retry_pending_inbound()
        await bot._retry_pending_inbound()
        await _drain(bot)
        assert bot._request_state(newer)["status"] == "delivered"

    asyncio.run(run())


def test_command_exception_after_effect_is_terminal_not_retried(tmp_path):
    bot = _bot(tmp_path)
    message = _message(bot, content=",stop")
    message.channel.send = AsyncMock(side_effect=TimeoutError)

    async def run():
        await bot.on_message(message)
        count = message.channel.send.await_count
        await bot._retry_pending_inbound()
        assert message.channel.send.await_count == count

    asyncio.run(run())
    row = bot._request_state(message)
    assert row["status"] == "failed"
    assert row["effects_started"] is True


def test_provider_call_receives_request_id_for_local_trace(tmp_path, caplog):
    bot = _bot(tmp_path)
    message = _message(bot)
    bot._night_fallback_kwargs = lambda: {}
    bot.ai_provider = SimpleNamespace(
        generate_response=AsyncMock(return_value="answer"), model="configured-model"
    )

    async def run():
        token = _current_inbound.set(message)
        try:
            assert await bot._generate_response([]) == "answer"
        finally:
            _current_inbound.reset(token)

    with caplog.at_level("INFO"):
        asyncio.run(run())
    bot.ai_provider.generate_response.assert_awaited_once_with([], request_id="101")
    assert "requested_model=configured-model" in caplog.text
