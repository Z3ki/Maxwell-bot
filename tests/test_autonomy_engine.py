import asyncio
import json
from types import SimpleNamespace

from autonomy import (
    AUTONOMY_DISABLED_TOOLS,
    AutonomyEngine,
    AutonomyStore,
    _truncate,
)


class DummyTool:
    def get_description(self):
        return "dummy"

    async def execute(self, *args, **kwargs):  # pragma: no cover - should not be hit
        raise AssertionError("disabled tool executed")


class FakeMemory:
    def __init__(self):
        self.added = []

    async def add_to_channel_memory(self, channel_id, message):
        self.added.append((channel_id, message))


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

    assert "post_channel: missing explicit numeric target_channel_id" in failures
    assert actions == [
        {"kind": "do_nothing", "reason": "all actions failed validation"}
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


def test_exec_run_tool_refuses_disabled_tools(tmp_path):
    disabled = next(iter(AUTONOMY_DISABLED_TOOLS))
    engine = _engine(tmp_path, auto_channels={"100"}, tools={disabled: DummyTool()})
    result = {"kind": "run_tool", "result": "success", "error": None}

    asyncio.run(engine._exec_run_tool({"tool_name": disabled, "tool_args": {}}, result))

    assert result["result"] == "error"
    assert "disabled" in result["error"]


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
    assert channel.ref.replies == [("threaded correctly", {"mention_author": False})]
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
    assert memory.added == [
        (
            "100",
            {
                "author": "Maxwell",
                "author_id": "42",
                "author_is_bot": True,
                "content": "that was me",
                "message_id": "777",
                "timestamp": memory.added[0][1]["timestamp"],
            },
        )
    ]


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
