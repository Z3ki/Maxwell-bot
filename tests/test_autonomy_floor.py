"""Turn-taking rules: does Maxwell hold the floor in this room right now.

The unit tests below drive `read_floor` directly with synthetic message
windows — no Discord, no bot — because the rules are the part worth pinning
down. The engine tests at the bottom check the other half: that a closed
verdict actually stops a send.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from autonomy import AutonomyEngine
from autonomy_social import (
    FLOOR_ADDRESSED,
    FLOOR_BUSY,
    FLOOR_COOLDOWN,
    FLOOR_HANDLED,
    FLOOR_HOLDING,
    FLOOR_IDLE,
    FLOOR_OPEN,
    FLOOR_REPLYING,
    FloorMessage,
    FloorSettings,
    floor_message_from_discord,
    read_floor,
    render_floor_section,
    summarize_floor,
)

NOW = datetime(2026, 8, 21, 12, 0, 0, tzinfo=timezone.utc)
BOT = SimpleNamespace(id=42)


def _msg(seconds_ago, *, author="user1", is_self=False, addresses=False, is_bot=False):
    return FloorMessage(
        created_at=NOW - timedelta(seconds=seconds_ago),
        is_self=is_self,
        is_bot=is_bot or is_self,
        addresses_self=addresses,
        author_id="42" if is_self else author,
    )


def _read(messages, **kwargs):
    kwargs.setdefault("now", NOW)
    return read_floor("100", messages, **kwargs)


# --- the states -----------------------------------------------------------


def test_replying_beats_everything():
    # Even a room where somebody is plainly waiting stays closed while the
    # main reply path is mid-generation — that reply IS the answer.
    verdict = _read([_msg(5, addresses=True)], is_replying=True)
    assert verdict.state == FLOOR_REPLYING
    assert verdict.may_speak is False


def test_empty_room_is_idle_and_open():
    verdict = _read([])
    assert verdict.state == FLOOR_IDLE
    assert verdict.may_speak is True


def test_maxwell_spoke_last_holds_the_floor():
    """The headline bug: autonomy adding a second line under its own first one."""
    verdict = _read([_msg(300, author="user1"), _msg(120, is_self=True)])
    assert verdict.state == FLOOR_HOLDING
    assert verdict.may_speak is False
    assert "last speaker" in verdict.reason


def test_hold_releases_once_the_room_has_moved_on():
    # Spoke into silence an hour ago. Nobody answered, nobody is talking.
    # Starting something fresh is fair at that point.
    verdict = _read([_msg(3700, is_self=True)])
    assert verdict.state == FLOOR_IDLE
    assert verdict.may_speak is True


def test_hold_survives_right_up_to_the_release_boundary():
    settings = FloorSettings(hold_release_seconds=1800)
    assert _read([_msg(1799, is_self=True)], settings=settings).state == FLOOR_HOLDING
    assert _read([_msg(1801, is_self=True)], settings=settings).state != FLOOR_HOLDING


def test_addressed_after_his_last_line_is_open():
    verdict = _read(
        [
            _msg(400, is_self=True),
            _msg(30, author="user1", addresses=True),
        ]
    )
    assert verdict.state == FLOOR_ADDRESSED
    assert verdict.may_speak is True


def test_ping_answered_by_the_live_path_is_handled():
    # Human pinged 60s ago; the normal on_message reply went out 30s ago but
    # hasn't landed in the history window yet. Autonomy must not answer twice.
    ping_ts = (NOW - timedelta(seconds=60)).timestamp()
    reply_ts = (NOW - timedelta(seconds=30)).timestamp()
    verdict = _read(
        [_msg(60, author="user1", addresses=True)],
        last_bot_reply_ts=reply_ts,
    )
    assert verdict.state == FLOOR_HANDLED
    assert verdict.may_speak is False
    assert reply_ts > ping_ts  # sanity on the fixture itself


def test_ping_newer_than_the_live_reply_is_still_addressed():
    verdict = _read(
        [_msg(10, author="user1", addresses=True)],
        last_bot_reply_ts=(NOW - timedelta(seconds=300)).timestamp(),
    )
    assert verdict.state == FLOOR_ADDRESSED


def test_ping_that_predates_his_own_last_line_does_not_count():
    # He was pinged, then he spoke. The ping is spent — this must not read as
    # someone still waiting once the hold window lapses.
    verdict = _read(
        [
            _msg(4000, author="user1", addresses=True),
            _msg(3900, is_self=True),
        ]
    )
    assert verdict.state != FLOOR_ADDRESSED


def test_cooldown_blocks_an_unprompted_restart():
    # He spoke 30s ago, someone else has since said something not aimed at
    # him. Nobody is waiting, so this is him starting a new thread — too soon.
    verdict = _read([_msg(30, is_self=True), _msg(10, author="user1")])
    assert verdict.state == FLOOR_COOLDOWN
    assert verdict.may_speak is False


def test_cooldown_does_not_apply_when_someone_is_waiting():
    verdict = _read([_msg(30, is_self=True), _msg(10, author="user1", addresses=True)])
    assert verdict.state == FLOOR_ADDRESSED
    assert verdict.may_speak is True


def test_legacy_reply_ts_alone_triggers_cooldown():
    # No self message in the window at all — only the in-memory reply stamp.
    verdict = _read(
        [_msg(10, author="user1")],
        last_bot_reply_ts=(NOW - timedelta(seconds=20)).timestamp(),
    )
    assert verdict.state == FLOOR_COOLDOWN


def test_busy_when_two_people_are_mid_exchange():
    verdict = _read(
        [_msg(30, author="a"), _msg(20, author="b"), _msg(5, author="a")]
    )
    assert verdict.state == FLOOR_BUSY
    assert verdict.may_speak is False


def test_one_person_talking_to_themselves_is_not_busy():
    # Three messages, but one author. That's someone thinking out loud, not a
    # conversation to interrupt.
    verdict = _read(
        [_msg(30, author="a"), _msg(20, author="a"), _msg(5, author="a")]
    )
    assert verdict.state == FLOOR_OPEN
    assert verdict.may_speak is True


def test_stale_burst_is_not_busy():
    # Same three-way burst, but it ended ten minutes ago.
    verdict = _read(
        [_msg(700, author="a"), _msg(690, author="b"), _msg(680, author="a")]
    )
    assert verdict.may_speak is True


def test_open_vs_idle_split_on_silence():
    settings = FloorSettings(idle_after_seconds=600)
    assert _read([_msg(60, author="a")], settings=settings).state == FLOOR_OPEN
    assert _read([_msg(900, author="a")], settings=settings).state == FLOOR_IDLE


def test_undated_messages_are_ignored_not_crashed():
    verdict = _read([FloorMessage(created_at=None, author_id="a"), _msg(60, author="a")])
    assert verdict.state == FLOOR_OPEN


# --- settings -------------------------------------------------------------


def test_settings_read_from_control():
    settings = FloorSettings.from_control(
        {
            "autonomy_floor_cooldown_seconds": 15,
            "autonomy_floor_idle_seconds": 120,
            "autonomy_floor_mid_flow_messages": 5,
        }
    )
    assert settings.cooldown_seconds == 15
    assert settings.idle_after_seconds == 120
    assert settings.mid_flow_min_messages == 5


def test_legacy_block_window_raises_the_cooldown_floor():
    # An operator who set the old knob to 10 minutes gets 10 minutes, not 90s.
    settings = FloorSettings.from_control(
        {"autonomy_recent_reply_block_seconds": 600}
    )
    assert settings.cooldown_seconds == 600


def test_legacy_block_window_never_lowers_the_cooldown():
    settings = FloorSettings.from_control({"autonomy_recent_reply_block_seconds": 0})
    assert settings.cooldown_seconds == FloorSettings.cooldown_seconds


def test_garbage_control_values_fall_back_to_defaults():
    settings = FloorSettings.from_control({"autonomy_floor_cooldown_seconds": "nope"})
    assert settings.cooldown_seconds == FloorSettings.cooldown_seconds
    assert FloorSettings.from_control(None).idle_after_seconds > 0


# --- discord adapter ------------------------------------------------------


def test_adapter_detects_mention_reply_and_self():
    ts = NOW - timedelta(seconds=5)
    mention = SimpleNamespace(
        author=SimpleNamespace(id=7, bot=False),
        mentions=[SimpleNamespace(id=42)],
        created_at=ts,
    )
    assert floor_message_from_discord(mention, bot_user=BOT).addresses_self is True

    plain = SimpleNamespace(
        author=SimpleNamespace(id=7, bot=False), mentions=[], created_at=ts
    )
    assert floor_message_from_discord(plain, bot_user=BOT).addresses_self is False

    replied = floor_message_from_discord(
        plain, bot_user=BOT, reply=SimpleNamespace(author=SimpleNamespace(id=42))
    )
    assert replied.addresses_self is True

    own = SimpleNamespace(
        author=SimpleNamespace(id=42, bot=True), mentions=[], created_at=ts
    )
    assert floor_message_from_discord(own, bot_user=BOT).is_self is True


def test_adapter_implicit_address_marks_inbound_dms_only():
    ts = NOW - timedelta(seconds=5)
    inbound = SimpleNamespace(
        author=SimpleNamespace(id=7, bot=False), mentions=[], created_at=ts
    )
    outbound = SimpleNamespace(
        author=SimpleNamespace(id=42, bot=True), mentions=[], created_at=ts
    )
    assert (
        floor_message_from_discord(
            inbound, bot_user=BOT, implicit_address=True
        ).addresses_self
        is True
    )
    # His own DM line is not a message addressed to him.
    assert (
        floor_message_from_discord(
            outbound, bot_user=BOT, implicit_address=True
        ).addresses_self
        is False
    )


def test_naive_timestamps_are_treated_as_utc():
    naive = SimpleNamespace(
        author=SimpleNamespace(id=7, bot=False),
        mentions=[],
        created_at=datetime(2026, 8, 21, 11, 59, 0),
    )
    parsed = floor_message_from_discord(naive, bot_user=BOT)
    assert parsed.created_at is not None
    assert parsed.created_at.tzinfo is not None


# --- rendering ------------------------------------------------------------


def test_render_groups_open_and_closed_rooms():
    verdicts = [
        _read([_msg(120, is_self=True)]),                      # HOLDING
        read_floor("200", [_msg(30, author="a", addresses=True)], now=NOW, label="channel=2"),
    ]
    text = render_floor_section(verdicts)
    assert "YOUR TURN" in text
    assert "NOT YOUR TURN" in text
    assert FLOOR_ADDRESSED in text
    assert FLOOR_HOLDING in text


def test_render_says_so_when_no_room_is_open():
    text = render_floor_section([_read([_msg(60, is_self=True)])])
    assert "none right now" in text
    assert "NOT YOUR TURN" in text


def test_render_with_no_rooms_read_does_not_imply_permission():
    # Failing open here would defeat the point of the whole module.
    text = render_floor_section([])
    assert "don't post" in text


def test_summarize_floor_is_loggable():
    assert "floor:" in summarize_floor([_read([_msg(60, is_self=True)])])
    assert "no rooms read" in summarize_floor([])


# --- engine gate ----------------------------------------------------------


class _Channel:
    """Minimal channel that records sends and serves a history window."""

    guild = SimpleNamespace(id=9)

    def __init__(self, cid=100, history=()):
        self.id = cid
        self.sent = []
        self._history = list(history)

    async def send(self, content, **kwargs):
        self.sent.append(content)
        return SimpleNamespace(id=777, created_at=datetime.now(timezone.utc))

    def history(self, limit=10):
        items = self._history[:limit]

        async def _gen():
            for item in items:
                yield item

        return _gen()


def _hist(seconds_ago, *, author_id, bot=False, mentions=()):
    return SimpleNamespace(
        id=1000 + int(seconds_ago),
        author=SimpleNamespace(id=author_id, bot=bot),
        mentions=[SimpleNamespace(id=m) for m in mentions],
        created_at=datetime.now(timezone.utc) - timedelta(seconds=seconds_ago),
    )


def _gate_bot(tmp_path, channel, *, replying=(), last_reply=None, control=None):
    return SimpleNamespace(
        config=SimpleNamespace(DATA_DIR=str(tmp_path)),
        _auto_channels={"100"},
        _control=control if control is not None else {},
        tools={},
        user=SimpleNamespace(id=42, display_name="Maxwell", name="Maxwell"),
        get_channel=lambda cid: channel if cid == 100 else None,
        fetch_channel=None,
        is_closed=lambda: False,
        _replying_channels=set(replying),
        _last_bot_reply=dict(last_reply or {}),
        rem_log=None,
        memory=None,
    )


def test_execute_drops_a_post_into_a_room_he_holds(tmp_path):
    """End to end: plan says post, room says he spoke last, nothing sends."""
    channel = _Channel(history=[_hist(60, author_id=42, bot=True)])
    engine = AutonomyEngine(_gate_bot(tmp_path, channel))

    results = asyncio.run(
        engine.execute(
            [{"kind": "post_channel", "target_channel_id": "100", "content": "hi again"}]
        )
    )

    assert results[0]["result"] == "skipped"
    assert FLOOR_HOLDING in results[0]["content_summary"]
    assert channel.sent == []


def test_execute_allows_a_post_when_the_room_is_his(tmp_path):
    channel = _Channel(
        history=[_hist(20, author_id=7, mentions=[42]), _hist(600, author_id=7)]
    )
    engine = AutonomyEngine(_gate_bot(tmp_path, channel))

    results = asyncio.run(
        engine.execute(
            [{"kind": "post_channel", "target_channel_id": "100", "content": "on it"}]
        )
    )

    assert results[0]["result"] == "success"
    assert channel.sent == ["on it"]


def test_execute_drops_a_post_while_the_main_bot_is_replying(tmp_path):
    channel = _Channel(history=[_hist(5, author_id=7, mentions=[42])])
    engine = AutonomyEngine(
        _gate_bot(tmp_path, channel, replying={"100"})
    )

    results = asyncio.run(
        engine.execute(
            [{"kind": "post_channel", "target_channel_id": "100", "content": "me too"}]
        )
    )

    assert results[0]["result"] == "skipped"
    assert FLOOR_REPLYING in results[0]["content_summary"]
    assert channel.sent == []


def test_execute_gate_can_be_switched_off(tmp_path):
    channel = _Channel(history=[_hist(60, author_id=42, bot=True)])
    engine = AutonomyEngine(
        _gate_bot(tmp_path, channel, control={"autonomy_floor_enabled": False})
    )

    results = asyncio.run(
        engine.execute(
            [{"kind": "post_channel", "target_channel_id": "100", "content": "hi again"}]
        )
    )

    assert results[0]["result"] == "success"
    assert channel.sent == ["hi again"]


def test_execute_still_dedups_two_posts_to_one_room_in_one_tick(tmp_path):
    channel = _Channel(
        history=[_hist(20, author_id=7, mentions=[42]), _hist(600, author_id=7)]
    )
    engine = AutonomyEngine(_gate_bot(tmp_path, channel))

    results = asyncio.run(
        engine.execute(
            [
                {"kind": "post_channel", "target_channel_id": "100", "content": "one"},
                {"kind": "post_channel", "target_channel_id": "100", "content": "two"},
            ]
        )
    )

    assert results[0]["result"] == "success"
    assert results[1]["result"] == "skipped"
    assert channel.sent == ["one"]


def test_non_speaking_actions_are_never_gated(tmp_path):
    """Freedom is the point: only speech is timed, everything else runs."""
    channel = _Channel(history=[_hist(5, author_id=42, bot=True)])
    engine = AutonomyEngine(
        _gate_bot(tmp_path, channel, replying={"100"})
    )

    results = asyncio.run(
        engine.execute([{"kind": "create_goal", "description": "learn webgpu"}])
    )

    assert results[0]["result"] == "success"


def test_send_dm_is_gated_through_the_recipients_dm_channel(tmp_path):
    dm = _Channel(cid=500, history=[_hist(30, author_id=42, bot=True)])
    bot = _gate_bot(tmp_path, dm)
    bot.get_channel = lambda cid: dm if cid == 500 else None
    engine = AutonomyEngine(bot)
    # Normally populated by gather_context's DM pass.
    engine._dm_channel_by_user = {"7": "500"}

    results = asyncio.run(
        engine.execute(
            [{"kind": "send_dm", "target_user_id": "7", "content": "you there?"}]
        )
    )

    assert results[0]["result"] == "skipped"
    assert FLOOR_HOLDING in results[0]["content_summary"]
    assert dm.sent == []


def test_send_dm_to_someone_new_is_not_gated(tmp_path):
    # No prior DM channel means no conversation to interrupt.
    channel = _Channel()
    engine = AutonomyEngine(_gate_bot(tmp_path, channel))
    engine._dm_channel_by_user = {}

    results = asyncio.run(
        engine.execute(
            [{"kind": "send_dm", "target_user_id": "7", "content": "hey"}]
        )
    )

    # Fails on user lookup (no get_user on the fake bot), but crucially it was
    # not short-circuited by the floor gate.
    assert "not your turn" not in str(results[0].get("content_summary", ""))


# --- gather_context integration -------------------------------------------


class _CtxStore:
    async def load_goals(self):
        return []

    async def load_state(self):
        return {}

    async def load_log(self):
        return []


class _CtxRemLog:
    async def drain_slice(self, since):
        return []


def _ctx_bot(tmp_path, channel, *, replying=(), private_channels=()):
    bot = SimpleNamespace(
        config=SimpleNamespace(DATA_DIR=str(tmp_path)),
        _auto_channels={str(channel.id)},
        _control={"bot_enabled": True, "autonomy_drives_enabled": False,
                  "autonomy_reflect_enabled": False},
        tools={},
        user=SimpleNamespace(id=42, display_name="Maxwell", name="Maxwell"),
        guilds=[SimpleNamespace(id=1, text_channels=[channel], me=SimpleNamespace())],
        private_channels=list(private_channels),
        rem_log=_CtxRemLog(),
        memory=None,
        get_channel=lambda cid: channel if cid == channel.id else None,
        fetch_channel=None,
        _replying_channels=set(replying),
        _last_bot_reply={},
    )
    channel.permissions_for = lambda _me: SimpleNamespace(send_messages=True)
    return bot


class _CtxChannel:
    """A channel whose history() is an async generator, as gather_context expects."""

    name = "general"
    topic = ""

    def __init__(self, cid=100, messages=()):
        self.id = cid
        self._messages = list(messages)

    async def history(self, limit=12):
        for msg in self._messages[:limit]:
            yield msg


def _ctx_msg(mid, seconds_ago, *, author_id, bot=False, mentions=(), content="hi"):
    return SimpleNamespace(
        id=mid,
        content=content,
        created_at=datetime.now(timezone.utc) - timedelta(seconds=seconds_ago),
        author=SimpleNamespace(
            id=author_id, display_name="Alice", name="alice", bot=bot
        ),
        mentions=[SimpleNamespace(id=m, display_name="Maxwell", name="max") for m in mentions],
        reference=None,
        attachments=[],
        embeds=[],
    )


def test_gather_context_renders_the_floor_read(tmp_path):
    channel = _CtxChannel(messages=[_ctx_msg(555, 30, author_id=7, mentions=[42])])
    engine = AutonomyEngine(_ctx_bot(tmp_path, channel))
    engine.store = _CtxStore()

    context = asyncio.run(engine.gather_context())

    assert "=== CONVERSATION FLOOR" in context
    assert "YOUR TURN" in context
    assert FLOOR_ADDRESSED in context
    # The read is cached for the execute-time gate to fall back on.
    assert engine._floor_verdicts["100"].state == FLOOR_ADDRESSED


def test_gather_context_marks_a_room_he_holds_as_closed(tmp_path):
    channel = _CtxChannel(messages=[_ctx_msg(555, 30, author_id=42, bot=True)])
    engine = AutonomyEngine(_ctx_bot(tmp_path, channel))
    engine.store = _CtxStore()

    context = asyncio.run(engine.gather_context())

    assert "NOT YOUR TURN" in context
    assert FLOOR_HOLDING in context
    assert engine._floor_verdicts["100"].may_speak is False


def test_floor_read_sits_directly_under_the_clock(tmp_path):
    # Section order is load-bearing: per-section budgets mean later sections
    # get truncated first, and this one must never be the thing that's cut.
    channel = _CtxChannel(messages=[_ctx_msg(555, 30, author_id=7)])
    engine = AutonomyEngine(_ctx_bot(tmp_path, channel))
    engine.store = _CtxStore()

    context = asyncio.run(engine.gather_context())
    sections = [s.split("\n")[0] for s in context.split("\n\n=== ")]

    assert "CURRENT TIME" in sections[0]
    assert "CONVERSATION FLOOR" in sections[1]


def test_gather_context_maps_dm_recipients_for_the_dm_gate(tmp_path):
    dm = _CtxChannel(cid=500, messages=[_ctx_msg(900, 30, author_id=7)])
    dm.recipient = SimpleNamespace(id=7, display_name="Alice", name="alice", bot=False)
    channel = _CtxChannel(messages=[_ctx_msg(555, 30, author_id=7)])
    engine = AutonomyEngine(_ctx_bot(tmp_path, channel, private_channels=[dm]))
    engine.store = _CtxStore()

    asyncio.run(engine.gather_context())

    assert engine._dm_channel_by_user["7"] == "500"
    # Inbound DMs count as addressed even with no mention in them.
    assert engine._floor_verdicts["500"].state == FLOOR_ADDRESSED


def test_gate_falls_back_to_the_mid_reply_check_with_no_visibility(tmp_path):
    """A channel this tick never read still can't be talked over."""
    engine = AutonomyEngine(
        _gate_bot(tmp_path, _Channel(cid=999), replying={"999"})
    )
    verdict = asyncio.run(engine._floor_gate("999"))
    assert verdict.state == FLOOR_REPLYING
    assert verdict.may_speak is False


def test_gate_allows_an_unseen_quiet_channel(tmp_path):
    engine = AutonomyEngine(_gate_bot(tmp_path, _Channel(cid=999)))
    assert asyncio.run(engine._floor_gate("999")).may_speak is True


def test_recent_live_reply_blocks_even_with_an_empty_history_window(tmp_path):
    engine = AutonomyEngine(
        _gate_bot(
            tmp_path,
            _Channel(cid=999),
            last_reply={"999": (datetime.now(timezone.utc) - timedelta(seconds=10)).timestamp()},
        )
    )
    verdict = asyncio.run(engine._floor_gate("999"))
    assert verdict.state == FLOOR_COOLDOWN
    assert verdict.may_speak is False


def test_a_blocked_post_does_not_consume_the_room_slot(tmp_path):
    """The second attempt must report the real reason, not a false 'already sent'."""
    channel = _Channel(history=[_hist(60, author_id=42, bot=True)])
    engine = AutonomyEngine(_gate_bot(tmp_path, channel))

    results = asyncio.run(
        engine.execute(
            [
                {"kind": "post_channel", "target_channel_id": "100", "content": "one"},
                {"kind": "post_channel", "target_channel_id": "100", "content": "two"},
            ]
        )
    )

    assert [r["result"] for r in results] == ["skipped", "skipped"]
    for r in results:
        assert FLOOR_HOLDING in r["content_summary"]
        assert "already sent" not in r["content_summary"]
    assert channel.sent == []
