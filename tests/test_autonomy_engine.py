import asyncio
import json
from datetime import datetime
from types import SimpleNamespace

from autonomy import (
    AutonomyContextIndex,
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
