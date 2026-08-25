"""The four-stage tick: observe -> plan -> policy gate -> execute.

The stages always existed; only two of them had names. The gate was a block
of `continue` statements in the middle of `execute`, which meant a denied
action and a failed action produced the same shape of result and nothing
could report "the plan was fine, policy stopped it". These tests pin the gate
as its own stage — decisions without side effects, denials that carry a
reason — and pin that pulling it out did not change what actually runs.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from autonomy import AutonomyEngine, GateVerdict, Observation


class _Channel:
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


def _engine(tmp_path, channel, control=None, tools=None):
    bot = SimpleNamespace(
        config=SimpleNamespace(DATA_DIR=str(tmp_path)),
        _auto_channels={"100"},
        _control=control if control is not None else {},
        tools=tools or {},
        user=SimpleNamespace(id=42, display_name="Maxwell", name="Maxwell"),
        get_channel=lambda cid: channel if cid == 100 else None,
        fetch_channel=None,
        is_closed=lambda: False,
        _replying_channels=set(),
        _last_bot_reply={},
        rem_log=None,
        memory=None,
    )
    return AutonomyEngine(bot)


def _open_room():
    """A room where it is genuinely his turn."""
    return _Channel(history=[_hist(20, author_id=7, mentions=[42]), _hist(600, author_id=7)])


# ─── the gate decides without doing ──────────────────────────────────────


def test_the_gate_allows_a_post_into_an_open_room_without_sending_it(tmp_path):
    channel = _open_room()
    engine = _engine(tmp_path, channel)
    action = {"kind": "post_channel", "target_channel_id": "100", "content": "hi"}

    verdicts = asyncio.run(engine.policy_gate([action]))

    assert [v.allowed for v in verdicts] == [True]
    assert verdicts[0].target_channel_id == "100"
    # A gate that sends is not a gate.
    assert channel.sent == []


def test_the_gate_denies_a_post_into_a_room_he_holds(tmp_path):
    channel = _Channel(history=[_hist(60, author_id=42, bot=True)])
    engine = _engine(tmp_path, channel)

    verdicts = asyncio.run(
        engine.policy_gate(
            [{"kind": "post_channel", "target_channel_id": "100", "content": "again"}]
        )
    )

    assert verdicts[0].allowed is False
    assert verdicts[0].code == "floor"
    # The reason is fed back to the planner as feedback, so it has to read as
    # an explanation rather than a code.
    assert "not your turn" in verdicts[0].reason


def test_the_gate_denies_the_second_post_to_one_room(tmp_path):
    engine = _engine(tmp_path, _open_room())
    verdicts = asyncio.run(
        engine.policy_gate(
            [
                {"kind": "post_channel", "target_channel_id": "100", "content": "one"},
                {"kind": "post_channel", "target_channel_id": "100", "content": "two"},
            ]
        )
    )
    assert [v.code for v in verdicts] == ["ok", "duplicate_post"]


def test_the_gate_denies_a_tool_autonomy_is_not_allowed(tmp_path):
    engine = _engine(tmp_path, _open_room(), control={"disabled_tools": ["shell"]})
    verdicts = asyncio.run(
        engine.policy_gate([{"kind": "run_tool", "tool_name": "shell", "tool_args": {}}])
    )
    assert verdicts[0].allowed is False
    assert verdicts[0].code == "tool_blocked"
    assert "shell" in verdicts[0].reason


def test_non_speaking_actions_pass_the_gate_untouched(tmp_path):
    # Only speech is timed. Research, memory and goal work are never gated.
    engine = _engine(tmp_path, _Channel(history=[_hist(1, author_id=42, bot=True)]))
    actions = [
        {"kind": "update_memory", "content": "a fact"},
        {"kind": "create_goal", "description": "learn webgpu"},
        {"kind": "do_nothing", "reason": "nothing to do"},
    ]
    verdicts = asyncio.run(engine.policy_gate(actions))
    assert all(v.allowed for v in verdicts)
    assert all(v.target_channel_id is None for v in verdicts)


def test_the_gate_can_be_switched_off(tmp_path):
    channel = _Channel(history=[_hist(60, author_id=42, bot=True)])
    engine = _engine(tmp_path, channel, control={"autonomy_floor_enabled": False})
    verdicts = asyncio.run(
        engine.policy_gate(
            [{"kind": "post_channel", "target_channel_id": "100", "content": "hi"}]
        )
    )
    assert verdicts[0].allowed is True


def test_dedup_survives_the_floor_being_off(tmp_path):
    # Structural, not a matter of taste: one plan never posts twice into one
    # room even with turn-taking disabled.
    engine = _engine(tmp_path, _open_room(), control={"autonomy_floor_enabled": False})
    verdicts = asyncio.run(
        engine.policy_gate(
            [
                {"kind": "post_channel", "target_channel_id": "100", "content": "one"},
                {"kind": "post_channel", "target_channel_id": "100", "content": "two"},
            ]
        )
    )
    assert [v.code for v in verdicts] == ["ok", "duplicate_post"]


def test_a_denied_room_is_not_claimed(tmp_path):
    # Claiming before the gate would make a blocked action consume the slot,
    # and the next action aimed at the room would come back "already sent" —
    # which is false, and that string reaches the planner as feedback.
    channel = _Channel(history=[_hist(60, author_id=42, bot=True)])
    engine = _engine(tmp_path, channel)
    verdicts = asyncio.run(
        engine.policy_gate(
            [
                {"kind": "post_channel", "target_channel_id": "100", "content": "one"},
                {"kind": "post_channel", "target_channel_id": "100", "content": "two"},
            ]
        )
    )
    assert [v.code for v in verdicts] == ["floor", "floor"]


# ─── execute honours the gate ────────────────────────────────────────────


def test_run_allowed_executes_only_what_the_gate_passed(tmp_path):
    channel = _open_room()
    engine = _engine(tmp_path, channel)
    allowed = GateVerdict(
        {"kind": "post_channel", "target_channel_id": "100", "content": "yes"},
        True,
    )
    denied = GateVerdict(
        {"kind": "post_channel", "target_channel_id": "100", "content": "no"},
        False,
        "floor",
        "not your turn in this conversation [holding] — spoke last",
    )

    results = asyncio.run(engine.run_allowed([allowed, denied]))

    assert channel.sent == ["yes"]
    assert results[1]["result"] == "skipped"
    # A denial is distinguishable from a failure, which is the point of
    # giving the gate a name.
    assert results[1]["denied_by"] == "floor"
    assert results[1]["error"] is None


def test_execute_still_gates_for_every_caller_that_wants_one_call(tmp_path):
    channel = _Channel(history=[_hist(60, author_id=42, bot=True)])
    engine = _engine(tmp_path, channel)
    results = asyncio.run(
        engine.execute(
            [{"kind": "post_channel", "target_channel_id": "100", "content": "hi"}]
        )
    )
    assert results[0]["result"] == "skipped"
    assert channel.sent == []


# ─── observation ─────────────────────────────────────────────────────────


def test_observe_reports_what_it_read(tmp_path):
    engine = _engine(tmp_path, _open_room())

    async def _fake_gather():
        return "the world, as it is"

    engine.gather_context = _fake_gather
    observation = asyncio.run(engine.observe())
    assert isinstance(observation, Observation)
    assert observation.context == "the world, as it is"
    assert observation.chars == len("the world, as it is")
    assert observation.duration >= 0


def test_a_hung_observation_fails_the_tick_rather_than_freezing_it(tmp_path):
    # Single-flight means a gather that never returns disables autonomy
    # forever, so the timeout has to surface as an error, not a silent skip.
    engine = _engine(tmp_path, _open_room())

    async def _hang():
        await asyncio.sleep(3600)

    engine.gather_context = _hang

    async def run():
        # Drive it with a short timeout rather than waiting out the real one.
        try:
            await asyncio.wait_for(engine.observe(), timeout=0.1)
        except asyncio.TimeoutError:
            return "timed out"
        return "returned"

    assert asyncio.run(run()) == "timed out"


# ─── sleep ───────────────────────────────────────────────────────────────


def test_a_sleeping_bot_does_not_post_unprompted(tmp_path):
    """He tells people he is asleep, then posts anyway — he did, until now.

    The live reply path refuses to answer while a sleep window is open and
    says "max is sleeping, back in Xm". Nothing checked that here, so the tick
    would post into a channel or DM someone while that notice was still
    standing.
    """
    channel = _open_room()
    engine = _engine(tmp_path, channel)
    engine.bot._is_sleeping = lambda: (True, 600)

    verdicts = asyncio.run(
        engine.policy_gate(
            [{"kind": "post_channel", "target_channel_id": "100", "content": "hi"}]
        )
    )

    assert verdicts[0].allowed is False
    assert verdicts[0].code == "asleep"
    assert channel.sent == []


def test_sleep_stops_speech_only(tmp_path):
    # Freedom is the point: he can still think, remember and plan asleep.
    engine = _engine(tmp_path, _open_room())
    engine.bot._is_sleeping = lambda: (True, 600)

    verdicts = asyncio.run(
        engine.policy_gate(
            [
                {"kind": "update_memory", "content": "a fact"},
                {"kind": "create_goal", "description": "learn webgpu"},
            ]
        )
    )
    assert all(v.allowed for v in verdicts)


def test_an_awake_bot_is_unaffected(tmp_path):
    engine = _engine(tmp_path, _open_room())
    engine.bot._is_sleeping = lambda: (False, 0)
    verdicts = asyncio.run(
        engine.policy_gate(
            [{"kind": "post_channel", "target_channel_id": "100", "content": "hi"}]
        )
    )
    assert verdicts[0].allowed is True


def test_sleep_disabled_means_no_autonomy_pause(tmp_path):
    # An operator who turned the feature off should not get a silent pause
    # from a stale window.
    engine = _engine(tmp_path, _open_room(), control={"enable_sleep": False})
    engine.bot._is_sleeping = lambda: (True, 600)
    verdicts = asyncio.run(
        engine.policy_gate(
            [{"kind": "post_channel", "target_channel_id": "100", "content": "hi"}]
        )
    )
    assert verdicts[0].allowed is True


def test_a_bot_that_cannot_report_sleep_is_treated_as_awake(tmp_path):
    engine = _engine(tmp_path, _open_room())

    def _boom():
        raise RuntimeError("no loop")

    engine.bot._is_sleeping = _boom
    verdicts = asyncio.run(
        engine.policy_gate(
            [{"kind": "post_channel", "target_channel_id": "100", "content": "hi"}]
        )
    )
    assert verdicts[0].allowed is True
