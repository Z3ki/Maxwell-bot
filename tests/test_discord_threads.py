"""Discord thread briefs: store, tools, and prompt injection."""

import asyncio
from types import SimpleNamespace

from bot import MaxwellBot, _prepare_tool_params
from discord_threads import (
    CreateThreadTool,
    ThreadControlTool,
    ThreadStore,
    clamp_auto_archive,
    is_discord_thread,
    sanitize_thread_name,
)
from tool_schemas import TOOL_PARAMETERS


class FakeThread:
    def __init__(self, tid="9001", name="maze spec"):
        self.id = tid
        self.name = name
        self.parent_id = "222"
        self.parent = SimpleNamespace(id="222", name="general")
        self.guild = SimpleNamespace(id="333")
        self.jump_url = "http://discord.test/t/9001"
        self.archived = False
        self.sent = []
        self.edits = []

    async def send(self, text):
        self.sent.append(text)

    async def edit(self, **kwargs):
        self.edits.append(kwargs)
        if "name" in kwargs:
            self.name = kwargs["name"]
        if "archived" in kwargs:
            self.archived = kwargs["archived"]


class FakeChannel:
    def __init__(self, cid="222", name="general"):
        self.id = cid
        self.name = name
        self.thread = FakeThread()

    async def create_thread(self, **kwargs):
        self.thread.name = kwargs.get("name") or self.thread.name
        return self.thread


class FakeMessage:
    def __init__(self, content="build a maze", *, thread=False):
        self.content = content
        self.author = SimpleNamespace(id="111", display_name="alice")
        self.guild = SimpleNamespace(id="333")
        self.id = "444"
        if thread:
            self.channel = FakeThread()
        else:
            self.channel = FakeChannel()

    async def create_thread(self, name=None, auto_archive_duration=None):
        thread = self.channel.thread
        thread.name = name or thread.name
        return thread


class MemoryStub:
    def __init__(self, rows=None):
        self.rows = rows or [
            {"author": "alice", "content": "make a coop maze"},
            {"author": "maxwell", "content": "ok what kind"},
        ]

    async def get_channel_memory(self, channel_id):
        return list(self.rows)


def run(coro):
    return asyncio.run(coro)


def _bot(tmp_path):
    store = ThreadStore(str(tmp_path))
    return SimpleNamespace(
        thread_store=store,
        memory=MemoryStub(),
        get_channel=lambda _id: None,
        fetch_channel=None,
    )


def test_sanitize_and_archive_helpers():
    assert sanitize_thread_name("  maze\n spec  ") == "maze spec"
    assert sanitize_thread_name("") == "thread"
    assert clamp_auto_archive("90") == 60
    assert clamp_auto_archive(2000) == 1440
    assert clamp_auto_archive("nope") == 1440


def test_is_discord_thread_detects_fakes_and_skips_channels():
    assert is_discord_thread(FakeThread()) is True
    assert is_discord_thread(FakeChannel()) is False
    typed = SimpleNamespace(type=SimpleNamespace(value=11, name="public_thread"))
    assert is_discord_thread(typed) is True
    assert is_discord_thread(SimpleNamespace(id=1)) is False


def test_create_thread_requires_context(tmp_path):
    tool = CreateThreadTool(_bot(tmp_path))
    out = run(tool.execute(FakeMessage(), name="maze"))
    assert out.startswith("Error: context is required")


def test_create_thread_refuses_nested_and_dms(tmp_path):
    tool = CreateThreadTool(_bot(tmp_path))
    nested = FakeMessage(thread=True)
    out = run(tool.execute(nested, context="keep going on the maze"))
    assert "already inside a thread" in out
    dm = FakeMessage()
    dm.guild = None
    out = run(tool.execute(dm, context="hello"))
    assert "not DMs" in out


def test_create_thread_stores_brief_and_parent_snapshot(tmp_path):
    bot = _bot(tmp_path)
    tool = CreateThreadTool(bot)
    msg = FakeMessage("let's build the maze in a thread")
    out = run(
        tool.execute(
            msg,
            name="maze spec",
            context="Coop neural worm maze. Static HTML, no backend. Stay in this thread.",
            opening="thread is open, dump ideas here",
        )
    )
    assert "Thread created:" in out
    assert "9001" in out
    rec = bot.thread_store.get("9001")
    assert rec is not None
    assert "Coop neural worm maze" in rec["context"]
    assert "make a coop maze" in rec["parent_snapshot"]
    assert msg.channel.thread.sent == ["thread is open, dump ideas here"]


def test_prompt_block_injects_brief(tmp_path):
    store = ThreadStore(str(tmp_path))
    thread = FakeThread()
    run(
        store.remember(
            thread,
            parent_message=FakeMessage(),
            context="Build the maze. No backend.",
            parent_snapshot="alice: make a maze",
        )
    )
    msg = FakeMessage(thread=True)
    msg.channel = thread
    block = store.prompt_block(msg)
    assert "Discord thread #maze spec" in block
    assert "child of #general" in block
    assert "Build the maze. No backend." in block
    assert "alice: make a maze" in block
    assert "Stay in this thread" in block


def test_prompt_block_empty_outside_threads(tmp_path):
    store = ThreadStore(str(tmp_path))
    msg = FakeMessage()
    assert store.prompt_block(msg) == ""


def test_thread_control_appends_context(tmp_path):
    bot = _bot(tmp_path)
    thread = FakeThread()
    run(bot.thread_store.remember(thread, context="first brief"))
    bot.get_channel = lambda tid: thread if int(tid) == 9001 else None
    tool = ThreadControlTool(bot)
    msg = FakeMessage()
    out = run(
        tool.execute(
            msg,
            action="context",
            thread_id="9001",
            context="also add a scoreboard",
        )
    )
    assert "appended" in out
    rec = bot.thread_store.get("9001")
    assert rec["context"] == "first brief"
    assert rec["notes"] == ["also add a scoreboard"]


def test_thread_control_rename_and_archive(tmp_path):
    bot = _bot(tmp_path)
    thread = FakeThread()
    run(bot.thread_store.remember(thread, context="brief"))
    bot.get_channel = lambda tid: thread if int(tid) == 9001 else None
    tool = ThreadControlTool(bot)
    msg = FakeMessage()
    out = run(tool.execute(msg, action="rename", thread_id="9001", name="maze v2"))
    assert "maze v2" in out
    assert thread.name == "maze v2"
    out = run(tool.execute(msg, action="archive", thread_id="9001"))
    assert "Archived" in out
    assert thread.archived is True
    assert bot.thread_store.get("9001")["status"] == "archived"


def test_turn_hides_create_thread_inside_a_thread():
    bot = SimpleNamespace(
        tools={"create_thread": object(), "thread_control": object(), "send_message": object()},
        _control={"disabled_tools": []},
        plugin_manager=None,
        _is_admin=lambda _uid: True,
    )
    in_thread = FakeMessage(thread=True)
    names = MaxwellBot._turn_tool_names(bot, "discord", in_thread, "hi")
    assert "create_thread" not in names
    assert "thread_control" in names
    parent = FakeMessage()
    names = MaxwellBot._turn_tool_names(bot, "discord", parent, "hi")
    assert "create_thread" in names


def test_prepare_tool_params_keeps_opening_from_message_alias():
    out = _prepare_tool_params(
        "create_thread",
        {"context": "do the maze", "message": "thread is open"},
    )
    assert out["opening"] == "thread is open"
    assert "message" not in out


def test_create_thread_schema_requires_context():
    schema = TOOL_PARAMETERS["create_thread"]
    assert "context" in schema["required"]
    assert "name" in schema["properties"]
    assert "opening" in schema["properties"]
