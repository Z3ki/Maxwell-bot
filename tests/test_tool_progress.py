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
        self.replies = []  # content of every reply() call

    async def reply(self, content=None, **kwargs):
        # Mirror the discord.Message.reply signature: post a new message
        # referencing this one. For test purposes, post via the channel
        # and record that a reply was used.
        self.replies.append(content)
        return await self.channel.send(content, **kwargs)


def test_initial_post_is_generic():
    msg = FakeMessage()
    prog = tool_progress.ToolProgress(msg)
    asyncio.run(prog.start())
    assert len(msg.channel.sent) == 1
    assert msg.channel.sent[0].content == "working on it…"
    asyncio.run(prog.stop())


def test_initial_post_uses_reply():
    """2026-07-19: 'working on it…' must thread under the user's
    message via reply(), not be a freestanding channel.send post."""
    msg = FakeMessage()
    prog = tool_progress.ToolProgress(msg)
    asyncio.run(prog.start())
    assert len(msg.replies) == 1
    assert msg.replies[0] == "working on it…"
    asyncio.run(prog.stop())


def test_half_sentence_reasoning_stays_at_placeholder():
    """A reasoning buffer with no terminator must NOT render as a
    partial sentence. The user explicitly said 'must be full sentence
    to show, not parts'. Buffer of 'the user wants me to look at' with
    no terminator -> '<tool>: generating…' until the model emits a
    period. (Before any tool commits it's 'working on it…'.)"""
    msg = FakeMessage()
    prog = tool_progress.ToolProgress(msg)
    asyncio.run(prog.start())
    msg_obj = msg.channel.sent[0]
    # No tool set yet; buffer is empty -> 'working on it…'
    assert msg_obj.content == "working on it…"
    # Set a tool but the reasoning buffer has no terminator yet.
    prog._last_edit = 0
    asyncio.run(prog.update("shell", "the user wants me to look at"))
    # Tool is set; no full sentence yet -> 'shell: generating…' (the
    # tool-active placeholder; the bot has committed to a tool but the
    # model hasn't finished a thought).
    assert msg_obj.content == "shell: generating…"
    # Add a terminator and a follow-up sentence — now the LAST full
    # sentence is rendered, with the tool-name prefix.
    prog._last_edit = 0
    asyncio.run(
        prog.update(
            "shell",
            "the user wants me to look at the disk and report back. They asked about space.",
        )
    )
    assert msg_obj.content == "shell: They asked about space."
    asyncio.run(prog.stop())


def test_update_replaces_in_place_one_sentence():
    msg = FakeMessage()
    prog = tool_progress.ToolProgress(msg)
    asyncio.run(prog.start())
    msg_obj = msg.channel.sent[0]

    # First update — reasoning must contain a terminator (full sentence)
    # per the 2026-07-19 'show whole sentences only' directive.
    prog._last_edit = 0
    asyncio.run(prog.update("shell", "checking disk usage."))
    assert msg_obj.content == "shell: checking disk usage."
    # New sentence + new tool — replaces with the new tool prefix
    prog._last_edit = 0
    asyncio.run(prog.update("web_search", "searching the docs."))
    assert msg_obj.content == "web_search: searching the docs."
    asyncio.run(prog.stop())


def test_update_uses_models_own_words():
    """The reason field IS the message — no decoration, no backticks, no emoji.
    The tool name now leads the line so the user sees which tool is running."""
    msg = FakeMessage()
    prog = tool_progress.ToolProgress(msg)
    asyncio.run(prog.start())
    msg_obj = msg.channel.sent[0]
    prog._last_edit = 0
    # Must end with a terminator; partial fragments stay as the placeholder.
    asyncio.run(prog.update("shell", "verifying apt sources are sane."))
    assert msg_obj.content == "shell: verifying apt sources are sane."
    # No emoji, no backticks
    assert "⏳" not in msg_obj.content
    assert "`" not in msg_obj.content
    asyncio.run(prog.stop())


def test_update_without_reason_uses_placeholder():
    """If the model didn't write a thought, the placeholder stays
    'working on it…' until reasoning arrives. We deliberately don't
    set the tool name on the visible line either (2026-07-19)."""
    msg = FakeMessage()
    prog = tool_progress.ToolProgress(msg)
    asyncio.run(prog.start())
    msg_obj = msg.channel.sent[0]
    prog._last_edit = 0
    asyncio.run(prog.update("fetch_url", ""))
    # Empty reasoning -> no edit; the original "working on it…" placeholder
    # stays on the line. update() is a no-op until reasoning has substance.
    assert msg_obj.content == "working on it…"
    asyncio.run(prog.stop())


def test_rate_limit_coalesces_rapid_updates():
    """Two updates within 2s should only post one edit."""
    msg = FakeMessage()
    prog = tool_progress.ToolProgress(msg)
    asyncio.run(prog.start())
    msg_obj = msg.channel.sent[0]
    # First update — go through (force last_edit to 0 so the rate limit
    # treats the post-start window as already-elapsed). Reasoning must
    # be a full sentence to pass the meaningful-reasoning gate.
    prog._last_edit = 0
    asyncio.run(prog.update("shell", "doing thing one."))
    assert msg_obj.content == "shell: doing thing one."
    # Second update immediately — should be cached but NOT edited
    edits_before = len(msg.channel.edited)
    asyncio.run(prog.update("shell", "doing thing two."))
    # No new edit because we're within the rate limit window
    assert len(msg.channel.edited) == edits_before
    # The pending line is still recorded so a future call picks it up
    assert prog._current_reason == "doing thing two."
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
    long = "checking disk usage and memory now.\n" + "x" * 500
    asyncio.run(prog.update("shell", long))
    content = msg_obj.content
    # One line
    assert "\n" not in content
    # Whole-sentence render with the tool-name prefix.
    assert content == "shell: checking disk usage and memory now."
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


# ---- per-token tick() (live streaming progress) ----


def test_tick_shows_thinking_before_tool_name():
    """Long generations where the model thinks for seconds before committing
    to a tool used to be silent (just 'working on it…'). tick() should
    surface the model's reasoning as 'thinking: <full sentence>' so the
    user sees liveness during the thinking phase. The 2026-07-19
    directive: only show full sentences, never partials."""
    msg = FakeMessage()
    prog = tool_progress.ToolProgress(msg)
    asyncio.run(prog.start())
    msg_obj = msg.channel.sent[0]
    edits_before = len(msg.channel.edited)
    # First tick — reasoning must include a sentence terminator to
    # pass the meaningful-reasoning gate.
    asyncio.run(
        prog.tick(reasoning_delta="The user wants me to draft a site about apples.")
    )
    assert len(msg.channel.edited) > edits_before
    assert (
        msg_obj.content == "thinking: The user wants me to draft a site about apples."
    )
    asyncio.run(prog.stop())


def test_tick_accumulates_reasoning_text():
    """Multiple reasoning deltas should accumulate into the buffer; the
    rendered preview is the latest tail, not a stale snapshot."""
    msg = FakeMessage()
    prog = tool_progress.ToolProgress(msg)
    asyncio.run(prog.start())
    msg_obj = msg.channel.sent[0]
    # Stream three deltas within the rate-limit window — only the first
    # actually edits; the rest accumulate in the buffer.
    prog._last_edit = 0
    asyncio.run(prog.tick(reasoning_delta="First bit. "))
    assert "First bit." in msg_obj.content
    # Force the second tick past the rate-limit so it can edit.
    prog._last_edit = 0
    asyncio.run(prog.tick(reasoning_delta="Then more thinking. "))
    # The preview should now contain both pieces (or at least the tail).
    assert "Then more thinking" in msg_obj.content
    prog._last_edit = 0
    asyncio.run(prog.tick(reasoning_delta="Final sentence."))
    assert "Final sentence" in msg_obj.content
    asyncio.run(prog.stop())


def test_tick_tool_name_shows_tool_prefix():
    """When tick() is called with tool_name, the visible line is prefixed
    with the tool name so the user sees which tool the model committed to.
    The reasoning sentence (if complete) follows after a colon. The
    2026-07-19 UX reversal: '<tool>: <reasoning>' IS desired — the user
    wants to see the tool name in flight, not just the assistant's words."""
    msg = FakeMessage()
    prog = tool_progress.ToolProgress(msg)
    asyncio.run(prog.start())
    msg_obj = msg.channel.sent[0]
    # First, a thinking tick.
    prog._last_edit = 0
    asyncio.run(prog.tick(reasoning_delta="Let me check that user."))
    assert msg_obj.content.startswith("thinking:")
    # Now the model commits to a tool — the very first tool_name tick
    # must go through even if we're inside the rate-limit window, and
    # it shows the tool name on the visible line.
    prog._last_edit = 0
    asyncio.run(prog.tick(reasoning_delta="", tool_name="lookup_user"))
    assert "lookup_user" in msg_obj.content
    # The reasoning sentence is preserved after the tool name.
    assert "Let me check that user." in msg_obj.content
    # And the old 'thinking:' prefix is gone.
    assert not msg_obj.content.startswith("thinking:")
    assert "thinking:" not in msg_obj.content
    asyncio.run(prog.stop())


def test_tick_rate_limits_to_avoid_429():
    """Rapid ticks within _TOKEN_TICK_INTERVAL coalesce; only the latest
    content survives. The Discord edit limit is 5/5s; a 10s long reasoning
    burst can produce 30+ deltas."""
    msg = FakeMessage()
    prog = tool_progress.ToolProgress(msg)
    asyncio.run(prog.start())
    msg_obj = msg.channel.sent[0]
    # First tick — goes through (first-tick exemption + buffer has
    # enough substance to clear the meaningful-reasoning gate).
    prog._last_edit = 0
    asyncio.run(prog.tick(reasoning_delta="Reasoning that is long enough to render."))
    edits_after_first = len(msg.channel.edited)
    # Two rapid ticks inside the window — must coalesce.
    asyncio.run(prog.tick(reasoning_delta=" More after."))
    asyncio.run(prog.tick(reasoning_delta=" And more."))
    # Still only the first edit landed (rate-limited), but the buffer
    # accumulated so a future tick picks it up.
    assert len(msg.channel.edited) == edits_after_first
    assert "And more." in prog._reasoning_buffer
    # Next tick past the window — flushes the accumulated buffer.
    prog._last_edit = 0
    asyncio.run(prog.tick(reasoning_delta=" Even more reasoning here."))
    assert len(msg.channel.edited) > edits_after_first
    assert "Even more" in msg_obj.content
    asyncio.run(prog.stop())


def test_tick_is_noop_after_stop():
    """Ticks fired after stop() must be silently dropped, not raise."""
    msg = FakeMessage()
    prog = tool_progress.ToolProgress(msg)
    asyncio.run(prog.start())
    asyncio.run(prog.stop())
    # Stopped — tick should not raise, not edit.
    edits_before = len(msg.channel.edited)
    asyncio.run(prog.tick(reasoning_delta="after stop"))
    assert len(msg.channel.edited) == edits_before


def test_stop_drains_final_tick_before_delete():
    """A tick that fired <_TOKEN_TICK_INTERVAL before stop() gets its
    content cached in the buffer but never rendered — without a drain
    in stop(), the user stares at the second-to-last update while the
    tool runs. stop() must do a final best-effort edit so the message
    reflects the latest model state when it disappears."""
    msg = FakeMessage()
    prog = tool_progress.ToolProgress(msg)
    asyncio.run(prog.start())
    msg_obj = msg.channel.sent[0]
    # Pretend an earlier tick already used the first-tick exemption so
    # subsequent ticks are rate-limited normally.
    prog._first_tick_done = True
    prog._edits_made = 1
    prog._last_edit = time_monotonic()  # pretend we just edited
    asyncio.run(prog.tick(reasoning_delta="Final thought before tool runs."))
    # The buffer accumulated, but the rate-limit blocked the edit.
    assert "Final thought" in prog._reasoning_buffer
    # Now stop() — must do one last edit so the user sees the final
    # reasoning before the message disappears.
    edits_before_stop = len(msg.channel.edited)
    asyncio.run(prog.stop())
    assert len(msg.channel.edited) > edits_before_stop
    assert "Final thought" in msg_obj.content
    # And then the message was deleted.
    assert msg_obj in msg.channel.deleted


def time_monotonic():
    import time

    return time.monotonic()


def test_last_full_sentence_returns_most_recent():
    """The visible line is the LAST complete sentence in the buffer,
    not the first one or the whole buffer. The 2026-07-19 user
    directive: full sentence to show, not parts."""
    s = tool_progress._last_full_sentence
    # No terminator -> empty.
    assert s("the user wants me to look") == ""
    # One sentence with terminator -> the whole string.
    assert (
        s("the user wants me to look at the disk.")
        == "the user wants me to look at the disk."
    )
    # Two sentences -> only the second (most recent) is returned.
    assert (
        s("the user wants me to look at the disk. They asked about space.")
        == "They asked about space."
    )
    # Three sentences -> only the last one.
    assert s("first thought. second thought. third thought.") == "third thought."
    # Mixed terminators.
    assert (
        s("the user asked a question! then I answered. finally, done?")
        == "finally, done?"
    )
