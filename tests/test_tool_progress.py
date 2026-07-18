"""Tests for tool_progress.ToolProgress.

Covers the four behaviors the user actually cares about:
  1. First post is the generic "working on it…" (no tool name yet)
  2. update() replaces the previous sentence in place (one line, not a log)
  3. Rate limit coalesces rapid updates — the channel doesn't see every tick
  4. notify_streaming() + stop() cleanly deletes the message so the
     channel is left with only the tool's own streamed output
  5. Telegram platform doesn't try to edit (no adapter for it)
  6. stop() is idempotent and safe from finally blocks

We test against a fake Message/Channel so we don't need Discord.
"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

# Make sure the repo root is on the path so the test can import
# `tool_progress` without an installed package.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import tool_progress  # noqa: E402


class FakeChannel:
    def __init__(self):
        self.sent = []  # list of (content,) or (file=,) tuples
        self.edited = []  # list of Message objects we edited (and their last content)
        self.deleted = []  # list of Message objects we deleted
        self._next_id = 1

    async def send(self, content=None, file=None, **kwargs):
        msg = SimpleNamespace(
            id=self._next_id,
            content=content,
            channel=self,
            _deleted=False,
            _edit_count=0,
        )
        self._next_id += 1
        self.sent.append(msg)

        # Mirror discord.Message.edit/delete
        async def edit(content=None, **kw):
            msg.content = content
            msg._edit_count += 1
            self.edited.append(msg)

        async def delete():
            msg._deleted = True
            self.deleted.append(msg)

        msg.edit = edit
        msg.delete = delete
        return msg


class FakeMessage:
    def __init__(self, platform="discord"):
        self.channel = FakeChannel()
        self.tool_platform = platform


def test_initial_post_is_generic():
    msg = FakeMessage()
    prog = tool_progress.ToolProgress(msg)
    asyncio.run(prog.start())
    assert len(msg.channel.sent) == 1
    assert msg.channel.sent[0].content == "working on it…"
    asyncio.run(prog.stop())


def test_update_replaces_in_place_one_sentence():
    msg = FakeMessage()
    prog = tool_progress.ToolProgress(msg)
    asyncio.run(prog.start())
    msg_obj = msg.channel.sent[0]

    # First update
    asyncio.run(prog.update("shell", "checking disk usage"))
    # Rate limit blocks the immediate edit; force a flush
    prog._last_edit = 0  # pretend it's been ages
    asyncio.run(prog.update("shell", "checking disk usage"))
    # The same sentence again — should be a no-op (content unchanged)
    edits_before = len(msg.channel.edited)
    asyncio.run(prog.update("shell", "checking disk usage"))
    assert len(msg.channel.edited) == edits_before  # coalesced
    # New sentence — replaces
    prog._last_edit = 0
    asyncio.run(prog.update("web_search", "searching the docs"))
    # Check last content of the edited message
    assert msg_obj.content == "web_search: searching the docs"
    asyncio.run(prog.stop())


def test_update_uses_models_own_words():
    """The reason field IS the message — no decoration, no backticks, no emoji."""
    msg = FakeMessage()
    prog = tool_progress.ToolProgress(msg)
    asyncio.run(prog.start())
    msg_obj = msg.channel.sent[0]
    prog._last_edit = 0
    asyncio.run(prog.update("shell", "verifying apt sources are sane"))
    assert msg_obj.content == "shell: verifying apt sources are sane"
    # No emoji, no backticks
    assert "⏳" not in msg_obj.content
    assert "`" not in msg_obj.content
    asyncio.run(prog.stop())


def test_update_without_reason_uses_placeholder():
    """If the model didn't write a thought, show a non-empty fallback."""
    msg = FakeMessage()
    prog = tool_progress.ToolProgress(msg)
    asyncio.run(prog.start())
    msg_obj = msg.channel.sent[0]
    prog._last_edit = 0
    asyncio.run(prog.update("fetch_url", ""))
    assert msg_obj.content == "fetch_url: working…"
    asyncio.run(prog.stop())


def test_rate_limit_coalesces_rapid_updates():
    """Two updates within 2s should only post one edit."""
    msg = FakeMessage()
    prog = tool_progress.ToolProgress(msg)
    asyncio.run(prog.start())
    msg_obj = msg.channel.sent[0]
    # First update — goes through
    asyncio.run(prog.update("shell", "doing thing one"))
    assert msg_obj.content == "shell: doing thing one"
    # Second update immediately — should be cached but NOT edited
    edits_before = len(msg.channel.edited)
    asyncio.run(prog.update("shell", "doing thing two"))
    # No new edit because we're within the rate limit window
    assert len(msg.channel.edited) == edits_before
    # The pending line is still recorded so a future call picks it up
    assert prog._current_reason == "doing thing two"
    asyncio.run(prog.stop())


def test_stop_deletes_posted_message():
    msg = FakeMessage()
    prog = tool_progress.ToolProgress(msg)
    asyncio.run(prog.start())
    posted_msg = msg.channel.sent[0]
    assert posted_msg not in msg.channel.deleted
    asyncio.run(prog.stop())
    assert posted_msg in msg.channel.deleted
    # stop() is idempotent
    asyncio.run(prog.stop())


def test_stop_without_post_is_safe():
    """stop() before start() should be a no-op, not raise."""
    msg = FakeMessage()
    prog = tool_progress.ToolProgress(msg)
    asyncio.run(prog.stop())
    assert msg.channel.sent == []
    assert msg.channel.deleted == []


def test_notify_streaming_marks_for_deletion():
    """notify_streaming() makes the next stop() actually delete."""
    msg = FakeMessage()
    prog = tool_progress.ToolProgress(msg)
    asyncio.run(prog.start())
    posted_msg = msg.channel.sent[0]
    # Simulate tool's first chunk going out
    prog.notify_streaming()
    # The next update() should be a no-op (streaming has started)
    edits_before = len(msg.channel.edited)
    prog._last_edit = 0
    asyncio.run(prog.update("shell", "now streaming"))
    assert len(msg.channel.edited) == edits_before
    asyncio.run(prog.stop())
    assert posted_msg in msg.channel.deleted


def test_telegram_does_not_try_to_edit():
    """Telegram adapter in this codebase has no editMessage; we just post
    one ack message and stop() drops the reference (we don't try to delete
    by message_id since the adapter doesn't expose a clean fetch)."""
    msg = FakeMessage(platform="telegram")
    prog = tool_progress.ToolProgress(msg)
    asyncio.run(prog.start())
    # First post went out
    assert len(msg.channel.sent) == 1
    # update() should be a no-op (we don't have editMessage on Telegram)
    edits_before = len(msg.channel.edited)
    prog._last_edit = 0
    asyncio.run(prog.update("shell", "any reasoning"))
    assert len(msg.channel.edited) == edits_before
    # stop() should NOT try to fetch+delete (no id exposed)
    asyncio.run(prog.stop())
    assert msg.channel.deleted == []


def test_start_is_idempotent():
    msg = FakeMessage()
    prog = tool_progress.ToolProgress(msg)
    asyncio.run(prog.start())
    asyncio.run(prog.start())
    # Only one post despite two starts
    assert len(msg.channel.sent) == 1
    asyncio.run(prog.stop())


def test_long_reasoning_truncated_to_one_sentence():
    """Multi-line or long reasoning is collapsed to one line and trimmed."""
    msg = FakeMessage()
    prog = tool_progress.ToolProgress(msg)
    asyncio.run(prog.start())
    msg_obj = msg.channel.sent[0]
    prog._last_edit = 0
    long = "checking disk usage\nand also looking at memory\n" + "x" * 500
    asyncio.run(prog.update("shell", long))
    content = msg_obj.content
    # One line
    assert "\n" not in content
    # Truncated with ellipsis (or short enough to fit)
    assert content.startswith("shell: ")
    assert "x" * 200 not in content  # the long tail was dropped
    asyncio.run(prog.stop())


def test_tool_base_helper_signals_progress():
    """The base Tool._signal_streaming() helper routes through bot._current_progress."""
    # Build a fake "bot" with a _current_progress that records calls
    fake_progress = SimpleNamespace()
    signaled = []
    fake_progress.notify_streaming = lambda: signaled.append(True)
    fake_bot = SimpleNamespace(_current_progress=fake_progress)

    # Need to import tools to test the helper
    import tools

    # Subclass Tool with a no-op execute/get_description to test _signal_streaming
    class _Stub(tools.Tool):
        def get_description(self):
            return ""

        async def execute(self, message, **kwargs):
            return ""

    inst = _Stub(fake_bot)
    inst._signal_streaming()
    assert signaled == [True]
    # No bot attached
    inst2 = _Stub(None)
    inst2._signal_streaming()  # should be a silent no-op, not raise
    # No _current_progress on the bot
    inst3 = _Stub(SimpleNamespace(_current_progress=None))
    inst3._signal_streaming()  # also a no-op


def test_progress_not_posted_when_start_skipped():
    """If the bot didn't create a progress (flag off, no batch), start() is
    a no-op and update() does nothing — exactly the disabled-by-default case."""
    msg = FakeMessage()
    # The control flag check happens in bot.py, not in ToolProgress itself.
    # But we can verify the *tool* handles the case where start was never
    # called gracefully.
    prog = tool_progress.ToolProgress(msg)
    # Never call start
    prog._last_edit = 0
    asyncio.run(prog.update("shell", "should be ignored"))
    # No post, no edit
    assert msg.channel.sent == []
    assert msg.channel.edited == []
