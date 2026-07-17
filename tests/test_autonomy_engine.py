import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from autonomy import (
    AutonomyContextIndex,
    AutonomyEngine,
    AutonomyStore,
    DRIVE_NAMES,
    IDLE_INITIATIVE_THRESHOLD,
    _truncate,
)


class DummyTool:
    def get_description(self):
        return "dummy"

    async def execute(self, *args, **kwargs):  # pragma: no cover - should not be hit
        raise AssertionError("disabled tool executed")


class FakeMemory:
    def __init__(self, channel_rows=None):
        self.added = []
        self.channel_rows = channel_rows or {}

    async def add_to_channel_memory(self, channel_id, message):
        self.added.append((channel_id, message))

    async def get_channel_memory(self, channel_id):
        return list(self.channel_rows.get(str(channel_id), []))


def _engine(tmp_path, *, auto_channels=None, tools=None, control=None):
    bot = SimpleNamespace(
        config=SimpleNamespace(DATA_DIR=str(tmp_path)),
        _auto_channels=set(auto_channels or []),
        _control=control or {},
        tools=tools or {},
    )
    return AutonomyEngine(bot)


def test_parse_plan_requires_explicit_channel_id(tmp_path):
    engine = _engine(tmp_path, auto_channels={"100", "200"})

    raw = json.dumps(
        {
            "thought": "say something",
            "actions": [{"kind": "post_channel", "content": "hello"}],
        }
    )
    actions, failures = engine._parse_plan(raw)

    assert any("post_channel: missing target_channel" in f for f in failures)
    assert actions == [
        {"kind": "do_nothing", "reason": "all actions failed validation"}
    ]


def test_parse_plan_resolves_channel_and_message_indices(tmp_path):
    engine = _engine(tmp_path, auto_channels={"100"}, tools={"react": DummyTool()})
    idx = AutonomyContextIndex()
    idx.add_channel("100")
    idx.add_message("555", "100")
    engine._context_index = idx

    raw = json.dumps(
        {
            "thought": "reply using numbered refs",
            "actions": [
                {
                    "kind": "post_channel",
                    "target_channel_id": "1",
                    "reply_to_message_id": "1",
                    "content": "yeah exactly",
                }
            ],
        }
    )
    actions, failures = engine._parse_plan(raw)

    assert failures == []
    assert actions == [
        {
            "kind": "post_channel",
            "target_channel_id": "100",
            "reply_to_message_id": "555",
            "content": "yeah exactly",
            "reason": "",
        }
    ]


def test_parse_plan_preserves_reply_to_message_id(tmp_path):
    engine = _engine(tmp_path, auto_channels={"100"})

    raw = json.dumps(
        {
            "thought": "reply to the right line",
            "actions": [
                {
                    "kind": "post_channel",
                    "target_channel_id": "100",
                    "reply_to_message_id": "555",
                    "content": "yeah exactly",
                }
            ],
        }
    )
    actions, failures = engine._parse_plan(raw)

    assert failures == []
    assert actions == [
        {
            "kind": "post_channel",
            "target_channel_id": "100",
            "reply_to_message_id": "555",
            "content": "yeah exactly",
            "reason": "",
        }
    ]


def test_parse_plan_preserves_run_tool_target_channel_id(tmp_path):
    engine = _engine(tmp_path, auto_channels={"100"}, tools={"react": DummyTool()})

    raw = json.dumps(
        {
            "thought": "react in the right room",
            "actions": [
                {
                    "kind": "run_tool",
                    "tool_name": "react",
                    "target_channel_id": "100",
                    "tool_args": {"emoji": "😂", "target_message_id": "555"},
                }
            ],
        }
    )
    actions, failures = engine._parse_plan(raw)

    assert failures == []
    assert actions == [
        {
            "kind": "run_tool",
            "tool_name": "react",
            "target_channel_id": "100",
            "tool_args": {"emoji": "😂", "target_message_id": "555"},
            "reason": "",
        }
    ]


def test_exec_run_tool_respects_dashboard_disabled_tools(tmp_path):
    disabled = "react"
    engine = _engine(
        tmp_path,
        auto_channels={"100"},
        tools={disabled: DummyTool()},
        control={"tools_enabled": True, "disabled_tools": [disabled]},
    )
    result = {"kind": "run_tool", "result": "success", "error": None}

    asyncio.run(engine._exec_run_tool({"tool_name": disabled, "tool_args": {}}, result))

    assert result["result"] == "error"
    assert "disabled" in result["error"]


def test_exec_run_tool_refuses_blocked_explicit_channel(tmp_path):
    engine = _engine(
        tmp_path,
        auto_channels={"100"},
        tools={"dummy": DummyTool()},
        control={"tools_enabled": True, "disabled_tools": [], "blocked_channels": ["100"]},
    )
    channel = SimpleNamespace(id=100, guild=SimpleNamespace(id=9))
    engine.bot.get_channel = lambda channel_id: channel if channel_id == 100 else None
    engine.bot.fetch_channel = None
    result = {"kind": "run_tool", "result": "success", "error": None}

    asyncio.run(
        engine._exec_run_tool(
            {"tool_name": "dummy", "target_channel_id": "100", "tool_args": {}},
            result,
        )
    )

    assert result["result"] == "error"
    assert result["error"] == "channel not allowed for autonomy"


def test_exec_run_tool_react_uses_target_message_id(tmp_path):
    class ReactTool:
        def get_description(self):
            return "react"

        async def execute(self, message, emoji=None, **kwargs):
            await message.add_reaction(emoji)
            return "reacted"

    class TargetMessage:
        def __init__(self):
            self.reactions = []

        async def add_reaction(self, emoji):
            self.reactions.append(emoji)

    class Channel:
        id = 100
        guild = SimpleNamespace(id=9)

        def __init__(self):
            self.target = TargetMessage()

        async def fetch_message(self, message_id):
            assert message_id == 555
            return self.target

    channel = Channel()
    bot = SimpleNamespace(
        config=SimpleNamespace(DATA_DIR=str(tmp_path)),
        _auto_channels={"100"},
        _control={"tools_enabled": True, "disabled_tools": []},
        tools={"react": ReactTool()},
        user=SimpleNamespace(id=42, display_name="Maxwell", name="Maxwell"),
        get_channel=lambda channel_id: channel if channel_id == 100 else None,
        fetch_channel=None,
    )
    engine = AutonomyEngine(bot)
    result = {"kind": "run_tool", "result": "success", "error": None}

    asyncio.run(
        engine._exec_run_tool(
            {
                "tool_name": "react",
                "target_channel_id": "100",
                "tool_args": {"emoji": "😂", "target_message_id": "555"},
            },
            result,
        )
    )

    assert result["result"] == "success"
    assert channel.target.reactions == ["😂"]


def test_exec_post_channel_replies_to_specific_message(tmp_path):
    class SentMessage:
        id = 777

    class ReferencedMessage:
        def __init__(self):
            self.replies = []

        async def reply(self, content, **kwargs):
            self.replies.append((content, kwargs))
            return SentMessage()

    class Channel:
        id = 100
        guild = SimpleNamespace(id=9)

        def __init__(self):
            self.ref = ReferencedMessage()
            self.sent = []

        async def fetch_message(self, message_id):
            assert message_id == 555
            return self.ref

        async def send(self, content):  # pragma: no cover - reply path should win
            self.sent.append(content)
            return SentMessage()

    channel = Channel()
    bot = SimpleNamespace(
        config=SimpleNamespace(DATA_DIR=str(tmp_path)),
        _auto_channels={"100"},
        _control={},
        tools={},
        get_channel=lambda channel_id: channel if channel_id == 100 else None,
        fetch_channel=None,
    )
    engine = AutonomyEngine(bot)
    result = {"kind": "post_channel", "result": "success", "error": None}

    asyncio.run(
        engine._exec_post_channel(
            {
                "target_channel_id": "100",
                "reply_to_message_id": "555",
                "content": "threaded correctly",
            },
            result,
        )
    )

    assert result["result"] == "success"
    assert result["sent_as_reply"] is True
    assert channel.ref.replies == [("threaded correctly", {"mention_author": True})]
    assert channel.sent == []


def test_exec_post_channel_records_autonomy_message_as_self_memory(tmp_path):
    class SentMessage:
        id = 777
        created_at = None

    class Channel:
        id = 100
        guild = SimpleNamespace(id=9)

        async def send(self, content):
            self.sent = content
            return SentMessage()

    memory = FakeMemory()
    channel = Channel()
    bot = SimpleNamespace(
        config=SimpleNamespace(DATA_DIR=str(tmp_path)),
        _auto_channels={"100"},
        _control={"store_memory": True},
        tools={},
        user=SimpleNamespace(id=42, display_name="Maxwell", name="Maxwell"),
        bot_name="Maxwell",
        memory=memory,
        get_channel=lambda channel_id: channel if channel_id == 100 else None,
        fetch_channel=None,
    )
    engine = AutonomyEngine(bot)
    result = {"kind": "post_channel", "result": "success", "error": None}

    asyncio.run(
        engine._exec_post_channel(
            {"target_channel_id": "100", "content": "that was me"},
            result,
        )
    )

    assert result["result"] == "success"
    assert len(memory.added) == 1
    channel_id, item = memory.added[0]
    datetime.fromisoformat(item.pop("timestamp"))
    assert channel_id == "100"
    assert item == {
        "author": "Maxwell",
        "author_id": "42",
        "author_is_bot": True,
        "content": "that was me",
        "message_id": "777",
        "autonomy": True,
        "autonomy_reason": "",
    }


def test_exec_post_channel_refuses_blocked_channel(tmp_path):
    class Channel:
        async def send(self, content):  # pragma: no cover - must not send
            raise AssertionError("sent to blocked channel")

    bot = SimpleNamespace(
        config=SimpleNamespace(DATA_DIR=str(tmp_path)),
        _auto_channels={"100"},
        _control={"blocked_channels": ["100"]},
        tools={},
        get_channel=lambda channel_id: Channel(),
        fetch_channel=None,
    )
    engine = AutonomyEngine(bot)
    result = {"kind": "post_channel", "result": "success", "error": None}

    asyncio.run(
        engine._exec_post_channel(
            {"target_channel_id": "100", "content": "nope"},
            result,
        )
    )

    assert result["result"] == "error"
    assert result["error"] == "channel not allowed for autonomy"


def test_create_goal_reports_store_limit_as_error(tmp_path):
    engine = _engine(tmp_path)
    engine.store = AutonomyStore(str(tmp_path))

    async def run():
        for idx in range(engine.store.MAX_GOALS):
            await engine.store.add_goal(f"goal {idx}")
        result = {"kind": "create_goal", "result": "success", "error": None}
        await engine._exec_create_goal({"description": "one too many"}, result)
        return result

    result = asyncio.run(run())
    assert result["result"] == "error"
    assert result["error"] == "goal limit reached"
    assert result["goal_id"] is None


def test_truncate_handles_tiny_budgets():
    assert _truncate("abcdef", 0) == ""
    assert _truncate("abcdef", 3) == "abc"
    assert _truncate("abcdef", 99) == "abcdef"


def test_gather_context_uses_numbered_channels_and_messages(tmp_path):
    class Store:
        async def load_goals(self):
            return []

        async def load_state(self):
            return {}

        async def load_log(self):
            return []

    class RemLog:
        async def drain_slice(self, since):
            return []

    class HistoryMessage:
        id = 555
        content = "hello there"
        created_at = datetime.now()
        author = SimpleNamespace(
            id=7,
            display_name="Alice",
            name="alice",
            bot=False,
        )
        mentions = []
        reference = None
        attachments = []
        embeds = []

    class Channel:
        id = 100
        name = "general"
        topic = ""

        async def history(self, limit=12):
            for msg in [HistoryMessage()]:
                yield msg

    channel = Channel()
    bot = SimpleNamespace(
        config=SimpleNamespace(DATA_DIR=str(tmp_path)),
        _auto_channels={"100"},
        _control={"bot_enabled": True},
        tools={},
        user=SimpleNamespace(id=42, display_name="Maxwell", name="Maxwell"),
        guilds=[
            SimpleNamespace(
                id=1,
                text_channels=[channel],
                me=SimpleNamespace(),
            )
        ],
        private_channels=[],
        rem_log=RemLog(),
        memory=None,
        get_channel=lambda channel_id: channel if channel_id == 100 else None,
        fetch_channel=None,
    )
    channel.permissions_for = lambda _me: SimpleNamespace(send_messages=True)
    engine = AutonomyEngine(bot)
    engine.store = Store()

    context = asyncio.run(engine.gather_context())

    assert "channel=1(#general)" in context
    assert "msg=1" in context
    assert "  1: #general" in context
    assert "100" not in context.split("=== AVAILABLE CHANNELS")[1].split("===")[0]


def test_gather_context_includes_normal_channel_memory(tmp_path):
    class Store:
        async def load_goals(self):
            return []

        async def load_state(self):
            return {}

        async def load_log(self):
            return []

    class RemLog:
        async def drain_slice(self, since):
            return []

    class Channel:
        id = 100
        name = "general"
        topic = ""

        async def history(self, limit=1):
            if False:
                yield None

    memory = FakeMemory(
        {
            "100": [
                {
                    "author": "Maxwell",
                    "author_id": "42",
                    "author_is_bot": True,
                    "content": "i already said this like maxwell",
                },
                {
                    "author": "Maxwell",
                    "author_is_bot": True,
                    "content": "old self row with missing id",
                }
            ]
        }
    )
    channel = Channel()
    bot = SimpleNamespace(
        config=SimpleNamespace(DATA_DIR=str(tmp_path)),
        _auto_channels={"100"},
        _control={"bot_enabled": True},
        tools={},
        user=SimpleNamespace(id=42, display_name="Maxwell", name="Maxwell"),
        guilds=[SimpleNamespace(text_channels=[], me=SimpleNamespace())],
        private_channels=[],
        rem_log=RemLog(),
        memory=memory,
        get_channel=lambda channel_id: channel if channel_id == 100 else None,
        fetch_channel=None,
    )
    engine = AutonomyEngine(bot)
    engine.store = Store()

    context = asyncio.run(engine.gather_context())

    assert "RECENT CONTEXT MEMORY" in context
    assert "You/Maxwell(42): i already said this like maxwell" in context
    assert "You/Maxwell: old self row with missing id" in context


# ---------------------------------------------------------------------------
# self-directed agency: drives, idle initiative, complete_goal, reflection
# ---------------------------------------------------------------------------


def _bot_with_user(tmp_path, *, control=None, tools=None):
    return SimpleNamespace(
        config=SimpleNamespace(DATA_DIR=str(tmp_path)),
        _auto_channels=set(),
        _control=control or {},
        tools=tools or {},
        user=SimpleNamespace(id=42, display_name="Maxwell", name="Maxwell"),
        bot_name="Maxwell",
    )


def test_parse_plan_complete_goal_valid(tmp_path):
    engine = _engine(tmp_path)
    raw = json.dumps(
        {
            "thought": "that goal is done",
            "actions": [
                {"kind": "complete_goal", "goal_id": "goal_abc12345", "reason": "done"}
            ],
        }
    )
    actions, failures = engine._parse_plan(raw)
    assert failures == []
    assert actions == [
        {"kind": "complete_goal", "goal_id": "goal_abc12345", "reason": "done"}
    ]


def test_parse_plan_complete_goal_alias(tmp_path):
    engine = _engine(tmp_path)
    raw = json.dumps(
        {
            "thought": "retire it",
            "actions": [{"kind": "retire_goal", "goal_id": "goal_xyz"}],
        }
    )
    actions, failures = engine._parse_plan(raw)
    assert failures == []
    assert actions[0]["kind"] == "complete_goal"
    assert actions[0]["goal_id"] == "goal_xyz"


def test_parse_plan_complete_goal_missing_id(tmp_path):
    engine = _engine(tmp_path)
    raw = json.dumps(
        {"thought": "x", "actions": [{"kind": "complete_goal"}]}
    )
    actions, failures = engine._parse_plan(raw)
    assert any("complete_goal: missing goal_id" in f for f in failures)
    assert actions == [
        {"kind": "do_nothing", "reason": "all actions failed validation"}
    ]


def test_exec_complete_goal_marks_inactive(tmp_path):
    store = AutonomyStore(str(tmp_path))

    async def run():
        created = await store.add_goal("ship the thing")
        gid = created["id"]
        engine = AutonomyEngine(_bot_with_user(tmp_path))
        engine.store = store
        result = {"kind": "complete_goal", "result": "success", "error": None}
        await engine._exec_complete_goal({"goal_id": gid}, result)
        goals = await store.load_goals()
        return result, goals, gid

    result, goals, gid = asyncio.run(run())
    assert result["result"] == "success"
    assert result["goal_id"] == gid
    target = [g for g in goals if g["id"] == gid][0]
    assert target["active"] is False
    assert target.get("completed_at")
    assert target.get("last_progress_at")  # retiring also stamps progress


def test_add_goal_seeds_last_progress_at(tmp_path):
    store = AutonomyStore(str(tmp_path))

    async def run():
        created = await store.add_goal("a brand new goal")
        engine = AutonomyEngine(
            _bot_with_user(tmp_path, control={"autonomy_goal_stale_days": 14})
        )
        return created, engine._goal_age_days(created)

    created, age = asyncio.run(run())
    assert created.get("last_progress_at")
    assert created.get("created_at")
    # A just-created goal must not be considered stale.
    assert age is not None and age < 1


def test_exec_complete_goal_unknown_id_errors(tmp_path):
    engine = AutonomyEngine(_bot_with_user(tmp_path))
    engine.store = AutonomyStore(str(tmp_path))
    result = {"kind": "complete_goal", "result": "success", "error": None}
    asyncio.run(engine._exec_complete_goal({"goal_id": "nope"}, result))
    assert result["result"] == "error"
    assert "not found" in result["error"]


def test_update_drives_decays_toward_baseline_and_clamps(monkeypatch, tmp_path):
    engine = AutonomyEngine(_bot_with_user(tmp_path))
    # No jitter -> deterministic.
    monkeypatch.setattr("autonomy.random.uniform", lambda a, b: 0.0)
    # Start every drive pinned to the ceiling; with empty stimuli they should
    # decay 10% toward baseline and stay within [0,1].
    drives_in = {name: 1.0 for name in DRIVE_NAMES}
    out = engine._update_drives(drives_in, {})
    for name in DRIVE_NAMES:
        assert 0.0 <= out[name] <= 1.0
    # curiosity: 1.0 + (0.45 - 1.0) * 0.10 == 0.945 (no bumps, social high so no
    # bored-maker nudge).
    assert out["curiosity"] == pytest.approx(0.945)
    assert out["social"] == pytest.approx(0.935)
    assert out["restless"] == pytest.approx(0.915)


def test_update_drives_bored_maker_nudge_when_social_low(monkeypatch, tmp_path):
    engine = AutonomyEngine(_bot_with_user(tmp_path))
    monkeypatch.setattr("autonomy.random.uniform", lambda a, b: 0.0)
    out = engine._update_drives({name: 0.0 for name in DRIVE_NAMES}, {})
    # creative gets the +0.03 bored-maker nudge because social<0.3.
    # creative = 0 + (0.25-0)*0.10 + 0.03 = 0.055
    assert out["creative"] == pytest.approx(0.055)
    # restless = 0 + (0.15-0)*0.10 + 0.02 = 0.035
    assert out["restless"] == pytest.approx(0.035)


def test_update_drives_jitter_can_push_past_zero_then_clamps(monkeypatch, tmp_path):
    engine = AutonomyEngine(_bot_with_user(tmp_path))
    # Strong negative jitter on a near-zero drive must clamp at 0, not go negative.
    monkeypatch.setattr("autonomy.random.uniform", lambda a, b: -1.0)
    out = engine._update_drives({name: 0.0 for name in DRIVE_NAMES}, {})
    for name in DRIVE_NAMES:
        assert out[name] >= 0.0


def test_compute_drive_stimuli_counts_mentions_replies_links(tmp_path):
    engine = AutonomyEngine(_bot_with_user(tmp_path))
    events = [
        {"role": "user", "user_id": "7", "mentions": [{"id": "42"}]},  # mentions you
        {"role": "user", "user_id": "8", "reply_to_self": True},       # reply to you
        {"role": "user", "user_id": "9"},                              # plain human msg
        {"role": "assistant", "user_id": "42"},                        # self -> not human
    ]
    ch_lines = ['content="see https://example.com and http://foo.bar/x"']
    stimuli = engine._compute_drive_stimuli(
        events=events, ch_lines=ch_lines, goals=[], engagement_present=False, state={}
    )
    assert stimuli["mentions_you"] == 1
    assert stimuli["replies_to_you"] == 1
    assert stimuli["human_msgs"] == 3  # 7, 8, 9 (assistant excluded)
    assert stimuli["links"] == 2
    assert stimuli["idle_bump"] == 0.0  # events present -> not idle


def test_compute_drive_stimuli_idle_grows_with_time_since_action(tmp_path):
    engine = AutonomyEngine(_bot_with_user(tmp_path))
    old = (datetime.now(timezone.utc) - timedelta(hours=4)).isoformat()
    stimuli = engine._compute_drive_stimuli(
        events=[], ch_lines=[], goals=[], engagement_present=False,
        state={"last_action_at": old},
    )
    # 4h idle -> factor (1 + min(4,8)) = 5x the per-tick bump.
    assert stimuli["idle_bump"] > 0.0
    assert stimuli["idle_bump"] == pytest.approx(0.04 * 5.0)


def test_compute_drive_stimuli_counts_stale_goals(tmp_path):
    engine = AutonomyEngine(
        _bot_with_user(tmp_path, control={"autonomy_goal_stale_days": 14})
    )
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    fresh = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    goals = [
        {"id": "g1", "active": True, "last_progress_at": old},
        {"id": "g2", "active": True, "last_progress_at": fresh},
        {"id": "g3", "active": False, "last_progress_at": old},  # inactive -> ignored
    ]
    stimuli = engine._compute_drive_stimuli(
        events=[], ch_lines=[], goals=goals, engagement_present=False, state={}
    )
    assert stimuli["stale_goals"] == 1


def test_goal_age_uses_last_progress_not_last_acted(tmp_path):
    # Regression for the staleness/all-bump conflict: a goal whose
    # last_acted_on is fresh (bumped by the all-goals "alive" path) but whose
    # last_progress_at is old MUST still count as stale. last_acted_on is the
    # "Maxwell is alive" signal and must not mask staleness.
    engine = AutonomyEngine(
        _bot_with_user(tmp_path, control={"autonomy_goal_stale_days": 14})
    )
    old_progress = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    fresh_acted = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    goal = {
        "id": "g1",
        "active": True,
        "last_acted_on": fresh_acted,   # fresh — would have masked staleness before
        "last_progress_at": old_progress,  # old — real progress watermark
    }
    age = engine._goal_age_days(goal)
    assert age is not None and age >= 14


def test_render_drives_section_idle_initiative_line(tmp_path):
    engine = AutonomyEngine(_bot_with_user(tmp_path))
    drives = {name: 0.2 for name in DRIVE_NAMES}
    drives["curiosity"] = 0.9  # high -> top drive
    text = engine._render_drives_section(drives, idle_initiative=True)
    assert "CURRENT DRIVES" in text
    assert "curiosity 0.90 (high)" in text
    assert "IDLE INITIATIVE" in text
    assert "curiosity is high" in text


def test_render_drives_section_no_idle_initiative(tmp_path):
    engine = AutonomyEngine(_bot_with_user(tmp_path))
    drives = {name: 0.2 for name in DRIVE_NAMES}
    text = engine._render_drives_section(drives, idle_initiative=False)
    assert "IDLE INITIATIVE" not in text


def test_idle_initiative_gate_threshold(tmp_path):
    # Top drive below threshold + no events -> no idle initiative line.
    engine = AutonomyEngine(_bot_with_user(tmp_path))
    drives = {name: 0.2 for name in DRIVE_NAMES}
    drives["curiosity"] = IDLE_INITIATIVE_THRESHOLD - 0.01
    text = engine._render_drives_section(drives, idle_initiative=False)
    assert "IDLE INITIATIVE" not in text


def test_should_reflect_true_when_never_or_old(tmp_path):
    engine = AutonomyEngine(_bot_with_user(tmp_path))
    now = datetime.now(timezone.utc)
    assert engine._should_reflect({}, now=now) is True  # never reflected
    old = (now - timedelta(seconds=4000)).isoformat()
    assert engine._should_reflect({"last_reflect_at": old}, now=now) is True


def test_should_reflect_false_when_recent(tmp_path):
    engine = AutonomyEngine(_bot_with_user(tmp_path))
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(seconds=60)).isoformat()
    assert engine._should_reflect({"last_reflect_at": recent}, now=now) is False


def test_reflection_section_text(tmp_path):
    engine = AutonomyEngine(_bot_with_user(tmp_path))
    text = engine._render_reflection_section()
    assert "REFLECTION" in text
    assert "complete_goal" in text
    assert "create_goal" in text


def test_gather_context_includes_drives_section(tmp_path):
    class Store:
        async def load_goals(self):
            return []

        async def load_state(self):
            return {}

        async def load_log(self):
            return []

    class RemLog:
        async def drain_slice(self, since):
            return []

    bot = SimpleNamespace(
        config=SimpleNamespace(DATA_DIR=str(tmp_path)),
        _auto_channels=set(),
        _control={"bot_enabled": True},
        tools={},
        user=SimpleNamespace(id=42, display_name="Maxwell", name="Maxwell"),
        guilds=[SimpleNamespace(text_channels=[], me=SimpleNamespace())],
        private_channels=[],
        rem_log=RemLog(),
        memory=None,
        get_channel=lambda channel_id: None,
        fetch_channel=None,
    )
    engine = AutonomyEngine(bot)
    engine.store = Store()

    context = asyncio.run(engine.gather_context())

    assert "CURRENT DRIVES" in context
    # Every drive name should be surfaced so the planner knows its wants.
    for name in DRIVE_NAMES:
        assert name in context


def test_gather_context_flags_stale_goal_and_nudges_complete_goal(tmp_path):
    class Store:
        def __init__(self):
            old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
            self.goals = [
                {"id": "goal_stale1", "active": True, "description": "old thing",
                 "last_progress_at": old}
            ]

        async def load_goals(self):
            return list(self.goals)

        async def load_state(self):
            return {}

        async def load_log(self):
            return []

    class RemLog:
        async def drain_slice(self, since):
            return []

    bot = SimpleNamespace(
        config=SimpleNamespace(DATA_DIR=str(tmp_path)),
        _auto_channels=set(),
        _control={"bot_enabled": True, "autonomy_goal_stale_days": 14},
        tools={},
        user=SimpleNamespace(id=42, display_name="Maxwell", name="Maxwell"),
        guilds=[SimpleNamespace(text_channels=[], me=SimpleNamespace())],
        private_channels=[],
        rem_log=RemLog(),
        memory=None,
        get_channel=lambda channel_id: None,
        fetch_channel=None,
    )
    engine = AutonomyEngine(bot)
    engine.store = Store()

    context = asyncio.run(engine.gather_context())

    assert "STALE" in context
    assert "complete_goal" in context  # the stale nudge names the retire action


def test_log_tick_bumps_last_acted_for_all_but_progress_only_for_referenced(tmp_path):
    # Real store so the goal-bump block in _log_tick persists. Confirms:
    #  - last_acted_on advances for EVERY active goal on a successful tick
    #    (the "Maxwell is alive" signal), AND
    #  - last_progress_at advances ONLY for the goal explicitly referenced
    #    (by a goal_id in a successful action's reason), so a goal merely
    #    "alive" but never formally touched keeps going stale.
    store = AutonomyStore(str(tmp_path))

    async def run():
        referenced = await store.add_goal("referenced goal")  # goal_########
        other = await store.add_goal("other goal, never referenced")
        referenced_id = referenced["id"]
        engine = AutonomyEngine(_bot_with_user(tmp_path))
        engine.store = store
        engine._last_thought = "did a thing for one goal"
        # A successful action whose reason names referenced_id by its id.
        actions = [
            {"kind": "update_memory", "content": "fact",
             "reason": f"advancing goal {referenced_id}"}
        ]
        results = [{"kind": "update_memory", "result": "success", "error": None,
                    "content_summary": "fact"}]
        await engine._log_tick("ctx", actions, results, 0.1, "2026-01-01T00:00:00+00:00")
        goals = {g["id"]: g for g in await store.load_goals()}
        return referenced_id, other["id"], goals

    ref_id, other_id, goals = asyncio.run(run())
    # Both active goals get last_acted_on (alive signal).
    assert goals[ref_id].get("last_acted_on")
    assert goals[other_id].get("last_acted_on")
    # Only the referenced goal's last_progress_at advanced beyond its seed.
    assert goals[ref_id]["last_progress_at"] == "2026-01-01T00:00:00+00:00"
    # The never-referenced goal's last_progress_at is still its creation stamp
    # (NOT overwritten with the tick time) — so it can still go stale later.
    assert goals[other_id]["last_progress_at"] != "2026-01-01T00:00:00+00:00"


def test_log_tick_does_not_mark_progress_when_only_do_nothing(tmp_path):
    # A do_nothing-only tick is not "acted", so neither last_acted_on nor
    # last_progress_at should advance (and last_action_at stays unset).
    store = AutonomyStore(str(tmp_path))

    async def run():
        g = await store.add_goal("lonely goal")
        engine = AutonomyEngine(_bot_with_user(tmp_path))
        engine.store = store
        engine._last_thought = "nothing to do"
        await engine._log_tick(
            "ctx",
            [{"kind": "do_nothing", "reason": "quiet"}],
            [{"kind": "do_nothing", "result": "skipped", "error": None,
              "content_summary": "quiet"}],
            0.1, "2026-01-01T00:00:00+00:00",
        )
        goals = await store.load_goals()
        state = await store.load_state()
        return g["id"], goals, state

    gid, goals, state = asyncio.run(run())
    goal = {x["id"]: x for x in goals}[gid]
    assert goal.get("last_acted_on") is None  # no all-bump on a do_nothing tick
    assert "last_action_at" not in state
