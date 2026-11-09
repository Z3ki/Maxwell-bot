"""Discord thread briefs: store, tools, and prompt injection."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

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
        self.owner_id = "111"
        self.guild = SimpleNamespace(
            id="333",
            name="test",
            me=SimpleNamespace(
                id="1",
                guild_permissions=SimpleNamespace(
                    administrator=True, manage_threads=True
                ),
            ),
        )
        self.jump_url = "http://discord.test/t/9001"
        self.archived = False
        self.sent = []
        self.edits = []

    async def send(self, text):
        self.sent.append(text)

    def permissions_for(self, _member):
        return SimpleNamespace(
            administrator=True,
            manage_threads=True,
            view_channel=True,
            read_message_history=True,
        )

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
        self.guild = self.thread.guild

    def permissions_for(self, _member):
        return SimpleNamespace(
            view_channel=True,
            read_message_history=True,
            create_public_threads=True,
            send_messages=True,
        )

    async def create_thread(self, **kwargs):
        self.thread.name = kwargs.get("name") or self.thread.name
        return self.thread


class FakeMessage:
    def __init__(self, content="build a maze", *, thread=False):
        self.content = content
        self.id = "444"
        if thread:
            self.channel = FakeThread()
        else:
            self.channel = FakeChannel()
        self.guild = self.channel.guild
        self.author = SimpleNamespace(
            id="111", display_name="alice", guild=self.guild
        )

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

    async def get_channel_memory(self, channel_id, *, requester=None):
        assert requester is not None
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


@pytest.mark.parametrize("actor", ["requester", "bot"])
@pytest.mark.parametrize(
    ("channel_type", "cap"),
    [
        (discord.ChannelType.text, "create_public_threads"),
        (discord.ChannelType.forum, "send_messages"),
    ],
)
def test_create_thread_requires_both_actors_channel_permissions(
    tmp_path, actor, channel_type, cap
):
    bot = _bot(tmp_path)
    msg = FakeMessage()
    msg.channel.type = channel_type
    msg.create_thread = AsyncMock()
    msg.channel.create_thread = AsyncMock()
    bot.memory.get_channel_memory = AsyncMock()

    def permissions_for(member):
        denied_id = "111" if actor == "requester" else "1"
        return SimpleNamespace(
            view_channel=True,
            create_public_threads=not (
                str(member.id) == denied_id and cap == "create_public_threads"
            ),
            send_messages=not (
                str(member.id) == denied_id and cap == "send_messages"
            ),
        )

    msg.channel.permissions_for = permissions_for
    result = run(CreateThreadTool(bot).execute(msg, context="start the project"))
    assert result.startswith("Error:")
    assert cap in result
    msg.create_thread.assert_not_awaited()
    msg.channel.create_thread.assert_not_awaited()
    bot.memory.get_channel_memory.assert_not_awaited()
    assert bot.thread_store.get("9001") is None


def test_user_install_thread_does_not_use_interaction_id_as_message(tmp_path):
    bot = _bot(tmp_path)
    msg = FakeMessage()
    msg.user_install = True
    msg.id = 987654321
    # UserInstallMessageAdapter has no create_thread method.
    msg = SimpleNamespace(**msg.__dict__)
    thread = msg.channel.thread
    msg.channel.create_thread = AsyncMock(return_value=thread)
    result = run(CreateThreadTool(bot).execute(msg, context="start the project"))
    assert result.startswith("Thread created:")
    kwargs = msg.channel.create_thread.await_args.kwargs
    assert kwargs["type"] == discord.ChannelType.public_thread
    assert "message" not in kwargs
    assert bot.thread_store.get("9001")["context"] == "start the project"


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


@pytest.mark.parametrize("action", ["context", "rename", "archive"])
def test_thread_control_registers_metadata_for_preexisting_threads(tmp_path, action):
    bot = _bot(tmp_path)
    msg = FakeMessage(thread=True)
    result = run(
        ThreadControlTool(bot).execute(
            msg, action=action, context="new brief", name="new title"
        )
    )
    assert not result.startswith("Error:")
    rec = bot.thread_store.get("9001")
    assert rec["guild_id"] == "333"
    assert rec["parent_channel_id"] == "222"
    assert rec["parent_channel_name"] == "general"
    assert rec["created_by"] == "111"
    assert rec["name"] == msg.channel.name
    assert bot.thread_store.list_for(guild_id="333", include_archived=True) == [rec]
    bot.get_channel = lambda _tid: msg.channel
    if action != "archive":
        assert rec["name"] in run(ThreadControlTool(bot).execute(msg, action="list"))


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


def test_thread_rename_requires_manage_threads_unless_owner(tmp_path):
    bot = _bot(tmp_path)
    thread = FakeThread()
    thread.owner_id = "999"

    def permissions_for(member):
        is_bot = getattr(member, "id", None) == "1"
        return SimpleNamespace(
            administrator=is_bot,
            manage_threads=is_bot,
            view_channel=True,
            read_message_history=True,
        )

    thread.permissions_for = permissions_for
    run(bot.thread_store.remember(thread, context="brief"))
    bot.get_channel = lambda tid: thread if int(tid) == 9001 else None
    msg = FakeMessage()
    msg.author = SimpleNamespace(id="111", display_name="alice", guild=msg.guild)
    out = run(
        ThreadControlTool(bot).execute(
            msg, action="rename", thread_id="9001", name="nope"
        )
    )
    assert "manage_threads" in out
    assert thread.name == "maze spec"


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


@pytest.mark.parametrize("permission", ["view_channel", "read_message_history", "uncached"])
def test_thread_briefs_require_readable_history_in_tools_and_prompt(tmp_path, permission):
    bot = _bot(tmp_path)
    msg = FakeMessage(thread=True)
    thread = msg.channel
    run(bot.thread_store.remember(
        thread, context="private brief", parent_snapshot="private parent transcript"
    ))
    if permission == "uncached":
        thread.permissions_for = None
    else:
        permissions = SimpleNamespace(
            view_channel=True, read_message_history=True, manage_threads=True
        )
        setattr(permissions, permission, False)
        thread.permissions_for = lambda _member: permissions
    bot.get_channel = lambda _tid: thread

    status = run(ThreadControlTool(bot).execute(msg, action="status"))
    listed = run(ThreadControlTool(bot).execute(msg, action="list"))
    prompt = bot.thread_store.prompt_block(msg)

    assert status.startswith("Error:")
    assert listed == "No Discord threads with a stored brief in this server."
    assert prompt == ""
    assert "private brief" not in status + listed + prompt
    assert "private parent transcript" not in status + listed + prompt


def test_thread_history_uses_interaction_requester_permissions(tmp_path):
    bot = _bot(tmp_path)
    msg = FakeMessage(thread=True)
    run(bot.thread_store.remember(msg.channel, context="private brief"))
    msg.interaction = SimpleNamespace(
        channel=msg.channel,
        permissions=SimpleNamespace(view_channel=True, read_message_history=False),
    )
    assert run(ThreadControlTool(bot).execute(msg, action="status")).startswith("Error:")
    assert bot.thread_store.prompt_block(msg) == ""


def test_create_thread_without_history_cannot_copy_parent_transcript(tmp_path):
    bot = _bot(tmp_path)
    msg = FakeMessage("my new request")
    msg.channel.permissions_for = lambda _member: SimpleNamespace(
        view_channel=True, create_public_threads=True, read_message_history=False
    )
    bot.memory.get_channel_memory = AsyncMock(
        return_value=[{"author": "other", "content": "older private transcript"}]
    )
    result = run(CreateThreadTool(bot).execute(msg, context="handle my request"))
    assert result.startswith("Thread created:")
    bot.memory.get_channel_memory.assert_not_awaited()
    assert bot.thread_store.get("9001")["parent_snapshot"] == "alice: my new request"


@pytest.mark.parametrize("operation", ["remember", "update"])
@pytest.mark.parametrize(
    "corrupt",
    ['{"threads":', '[]', '{"threads": []}', '{"threads": {"9001": "broken"}}'],
)
def test_thread_mutations_preserve_corrupt_store_even_after_cached_read(
    tmp_path, operation, corrupt
):
    store = ThreadStore(str(tmp_path))
    store.load()
    store.path.write_text(corrupt, encoding="utf-8")
    with pytest.raises(ValueError):
        if operation == "remember":
            run(store.remember(FakeThread(), context="new brief"))
        else:
            run(store.update("9001", context="new note"))
    assert store.path.read_text(encoding="utf-8") == corrupt


def test_thread_mutations_merge_disk_state_instead_of_stale_cache(tmp_path):
    first = ThreadStore(str(tmp_path))
    second = ThreadStore(str(tmp_path))
    first.load()
    second.load()
    run(first.remember(FakeThread("9001"), context="first brief"))
    run(second.remember(FakeThread("9002"), context="second brief"))
    run(first.update("9001", context="first note"))
    run(second.update("9001", context="second note"))

    restored = ThreadStore(str(tmp_path))
    assert restored.get("9001")["context"] == "first brief"
    assert restored.get("9001")["notes"] == ["first note", "second note"]
    assert restored.get("9002")["context"] == "second brief"


@pytest.mark.parametrize("requester", ["missing", "wrong_guild", "private_nonmember"])
def test_thread_status_never_lends_bot_access_to_unrecognized_requesters(
    tmp_path, requester
):
    bot = _bot(tmp_path)
    msg = FakeMessage(thread=True)
    run(bot.thread_store.remember(msg.channel, context="private brief"))
    if requester == "missing":
        msg.author = None
    elif requester == "wrong_guild":
        msg.guild = SimpleNamespace(id="other")
    else:
        msg.channel.is_private = lambda: True
        msg.channel.get_member = lambda _uid: None
        msg.channel.permissions_for = lambda _member: SimpleNamespace(
            view_channel=True, read_message_history=True, manage_threads=False
        )
    result = run(ThreadControlTool(bot).execute(msg, action="status"))
    assert result.startswith("Error:")
    assert "private brief" not in result
    assert bot.thread_store.prompt_block(msg) == ""


@pytest.mark.parametrize("manager", [False, True])
def test_private_thread_brief_remains_available_to_members_and_thread_managers(
    tmp_path, manager
):
    bot = _bot(tmp_path)
    msg = FakeMessage(thread=True)
    msg.channel.is_private = lambda: True
    msg.channel.get_member = lambda uid: None if manager else SimpleNamespace(id=uid)
    msg.channel.permissions_for = lambda _member: SimpleNamespace(
        view_channel=True, read_message_history=True, manage_threads=manager
    )
    run(bot.thread_store.remember(msg.channel, context="authorized brief"))
    result = run(ThreadControlTool(bot).execute(msg, action="status"))
    assert "authorized brief" in result
    assert "authorized brief" in bot.thread_store.prompt_block(msg)


def test_private_thread_member_cannot_publish_brief_in_parent_channel(tmp_path):
    bot = _bot(tmp_path)
    msg = FakeMessage()
    thread = msg.channel.thread
    thread.is_private = lambda: True
    thread.get_member = lambda uid: SimpleNamespace(id=uid)
    thread.permissions_for = lambda _member: SimpleNamespace(
        view_channel=True, read_message_history=True, manage_threads=False
    )
    run(bot.thread_store.remember(
        thread, context="private brief", parent_snapshot="private transcript"
    ))
    bot.get_channel = lambda _tid: thread
    tool = ThreadControlTool(bot)

    status = run(tool.execute(msg, action="status", thread_id="9001"))
    listed = run(tool.execute(msg, action="list"))
    assert status.startswith("Error:")
    assert "private response" in status
    assert listed == "No Discord threads with a stored brief in this server."
    assert "private brief" not in status + listed
    assert "private transcript" not in status + listed

    # Updating a remote thread is still authorized; only disclosure is scoped.
    updated = run(tool.execute(
        msg, action="context", thread_id="9001", context="new authorized note"
    ))
    assert updated.startswith("Brief appended")
    assert bot.thread_store.get("9001")["notes"] == ["new authorized note"]

    msg.user_install = True
    msg.response_visibility = "private"
    private_status = run(tool.execute(msg, action="status", thread_id="9001"))
    assert "private brief" in private_status
    assert "private transcript" in private_status
    assert "private brief" in run(tool.execute(msg, action="list"))
