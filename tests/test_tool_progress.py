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


def test_initial_post_uses_channel_send_not_reply():
    """2026-07-21: 'working on it…' must NOT thread as a reply to the
    user's message. A reply pings the user with a desktop push on every
    tool call, which is noise. The progress is informational, so it
    goes to the channel as a plain message.send() instead."""
    msg = FakeMessage()
    prog = tool_progress.ToolProgress(msg)
    asyncio.run(prog.start())
    assert len(msg.replies) == 0
    assert len(msg.channel.sent) == 1
    assert msg.channel.sent[0].content == "working on it…"
    asyncio.run(prog.stop())


def test_half_sentence_reasoning_shows_partial_tail():
    """2026-07-21 rewrite: the visible line is the last N chars of
    the streaming buffer, NOT a complete-sentence extraction. A
    partial buffer (no terminator) is shown as-is. The user watches
    the words scroll by in real time, no waiting for a period."""
    msg = FakeMessage()
    prog = tool_progress.ToolProgress(msg)
    asyncio.run(prog.start())
    msg_obj = msg.channel.sent[0]
    # No tool set yet; buffer is empty -> 'working on it…'
    assert msg_obj.content == "working on it…"
    # Set a tool but the reasoning buffer has no terminator yet.
    prog._last_edit = 0
    asyncio.run(prog.update("shell", "the user wants me to look at"))
    # Partial buffer -> the partial text is shown, with tool prefix.
    assert msg_obj.content == "shell: the user wants me to look at"
    # More reasoning — visible line scrolls forward with it.
    prog._last_edit = 0
    asyncio.run(
        prog.update(
            "shell",
            "the user wants me to look at the disk and report back. They asked about space.",
        )
    )
    # The whole reasoning is in the buffer (under _VISIBLE_BUDGET=200
    # chars), so the visible line is the full reasoning. The rolling
    # tail design shows up only when the buffer overflows 200 chars.
    assert msg_obj.content == (
        "shell: the user wants me to look at the disk and report back. "
        "They asked about space."
    )


def test_update_replaces_in_place():
    msg = FakeMessage()
    prog = tool_progress.ToolProgress(msg)
    asyncio.run(prog.start())
    msg_obj = msg.channel.sent[0]

    # First update — tool name + reasoning appended to buffer.
    prog._last_edit = 0
    asyncio.run(prog.update("shell", "checking disk usage."))
    assert msg_obj.content == "shell: checking disk usage."
    # New tool — buffer resets to the new reasoning (new tool = new line).
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
    # treats the post-start window as already-elapsed).
    prog._last_edit = 0
    asyncio.run(prog.update("shell", "doing thing one."))
    assert msg_obj.content == "shell: doing thing one."
    # Second update immediately — should be cached but NOT edited
    edits_before = len(msg.channel.edited)
    asyncio.run(prog.update("shell", "doing thing two."))
    # No new edit because we're within the rate limit window
    assert len(msg.channel.edited) == edits_before
    # The pending line is still recorded so a future call picks it up
    assert "doing thing two." in prog._reasoning_buffer
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


def test_long_reasoning_truncated_to_visible_budget():
    """A 500-char reasoning run is shown as the last _VISIBLE_BUDGET
    chars of the buffer (trimmed to a word boundary) so the line
    fits in a Discord message and doesn't overflow."""
    msg = FakeMessage()
    prog = tool_progress.ToolProgress(msg)
    asyncio.run(prog.start())
    msg_obj = msg.channel.sent[0]
    prog._last_edit = 0
    long = "checking disk usage and memory now.\n" + "x" * 500
    asyncio.run(prog.update("shell", long))
    content = msg_obj.content
    # Newlines collapsed to spaces.
    assert "\n" not in content
    # The tool name is prepended.
    assert content.startswith("shell: ")
    # The visible line is at most _VISIBLE_BUDGET + the tool-prefix length.
    assert len(content) <= tool_progress._VISIBLE_BUDGET + len("shell: ")
    # The leading part of the reasoning isn't in the visible tail
    # (we kept only the last _VISIBLE_BUDGET chars).
    assert "checking disk usage and memory now." not in content
    asyncio.run(prog.stop())


def test_tool_base_helper_signals_progress():
    """The base Tool._signal_streaming() helper routes through
    bot._current_progress_by_channel. Under load many channels can
    have tool batches in flight, and the per-channel dict makes
    sure tool B in channel Y doesn't accidentally notify channel X's
    progress."""
    import tools

    # Subclass Tool with a no-op execute/get_description to test _signal_streaming
    class _Stub(tools.Tool):
        def get_description(self):
            return ""

        async def execute(self, message, **kwargs):
            return ""

    # Build a fake "bot" with a per-channel progress dict
    fake_progress = SimpleNamespace()
    signaled = []
    fake_progress.notify_streaming = lambda: signaled.append(True)
    fake_message = SimpleNamespace(channel=SimpleNamespace(id=42))
    fake_bot = SimpleNamespace(_current_progress_by_channel={"42": fake_progress})

    inst = _Stub(fake_bot)
    inst._signal_streaming(fake_message)
    assert signaled == [True]

    # No bot attached: no-op
    inst2 = _Stub(None)
    inst2._signal_streaming()  # should be a silent no-op, not raise

    # No _current_progress_by_channel on the bot: no-op
    inst3 = _Stub(SimpleNamespace())
    inst3._signal_streaming(fake_message)  # also a no-op

    # The per-channel bug under load: when a tool batches across
    # many channels and the helper doesn't have the message in scope,
    # the OLD code (single ``bot._current_progress``) would signal
    # whatever channel's progress was last written — almost always
    # the WRONG one. The NEW code without ``message`` only acts when
    # the dict has exactly one entry (the single-channel bot case),
    # so a multi-channel bot never accidentally signals the wrong
    # progress just because the helper couldn't resolve the channel.
    wrong_chan = SimpleNamespace(channel=SimpleNamespace(id=99))
    multi_bot = SimpleNamespace(
        _current_progress_by_channel={
            "42": fake_progress,
            "99": SimpleNamespace(notify_streaming=lambda: signaled.append("WRONG")),
        }
    )
    inst_multi = _Stub(multi_bot)
    signaled.clear()
    inst_multi._signal_streaming()  # no message passed, multi-channel bot
    # Both progresses are still alive; the helper didn't pick a random
    # one. The user gets the wrong-channel-delete bug NOT triggered.
    assert signaled == []
    # When the helper DOES have the message, the per-channel lookup
    # works as expected.
    signaled.clear()
    inst_multi._signal_streaming(fake_message)  # id=42
    assert signaled == [True]
    signaled.clear()
    inst_multi._signal_streaming(wrong_chan)  # id=99
    assert signaled == ["WRONG"]  # the right channel's progress WAS signaled


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


# 2026-07-21: sentence-extraction (last full sentence, terminator regex,
# _format_thinking) is GONE. The progress UI now just shows the last
# _VISIBLE_BUDGET chars of the streaming buffer with the tool name
# prefix. No more "is this a complete sentence?" heuristic, no more
# glued-delta bug, no more "user can't read a half-sentence" debate.
# The user watches the words scroll by in real time.


# ---- fast-tool fix: deferred post + transition_to_final ----


def test_fast_tool_no_progress_message_posted():
    """The fast-tool flicker the user reported: bot posts 'working
    on it…', stops the tool batch in 50ms, then sends a fresh reply.
    Old behavior: <placeholder> <deletion> <reply>. New behavior:
    if stop() lands before the deferred post window, no message
    ever goes out — the user just sees the reply.

    With the fire-and-forget start_defer() API this is the path the
    bot's gen_progress takes. We exercise it directly: start_defer
    schedules a background post task, stop() cancels it before it
    wakes up, the post never lands."""
    msg = FakeMessage()
    prog = tool_progress.ToolProgress(msg)
    asyncio.run(prog.start_defer())  # fire-and-forget
    # Cancel before the deferred window elapses
    asyncio.run(prog.stop())
    # The background post task was cancelled; no message ever went out.
    assert msg.channel.sent == []
    assert msg.channel.deleted == []


def test_slow_tool_still_posts():
    """If the tool batch is still running after the deferred window,
    the progress message MUST go out so the user sees liveness."""
    msg = FakeMessage()
    prog = tool_progress.ToolProgress(msg)
    asyncio.run(prog.start())  # waits the window
    # Window elapsed, post is now live
    assert len(msg.channel.sent) == 1
    assert msg.channel.sent[0].content == "working on it…"
    # Tick to give it a real sentence
    prog._last_edit = 0
    asyncio.run(prog.tick(reasoning_delta="Doing the slow thing."))
    assert "Doing the slow thing." in msg.channel.sent[0].content
    asyncio.run(prog.stop())
    # Posted AND deleted
    assert msg.channel.sent[0] in msg.channel.deleted


def test_transition_to_final_edits_in_place():
    """The fast-tool happy path: the bot has a reply, transitions
    the progress message to BECOME the reply. No delete, no
    second post, no flicker."""
    msg = FakeMessage()
    prog = tool_progress.ToolProgress(msg)
    asyncio.run(prog.start())  # posts
    assert len(msg.channel.sent) == 1
    # Simulate a tick that put real content
    prog._last_edit = 0
    asyncio.run(prog.tick(reasoning_delta="I will check the disk. Looking now."))
    posted = msg.channel.sent[0]
    # transition_to_final: edit in place
    ok = asyncio.run(prog.transition_to_final("Disk has 50GB free."))
    assert ok is True
    # The message was EDITED, not deleted, not re-posted
    assert posted.content == "Disk has 50GB free."
    assert posted not in msg.channel.deleted
    # And it's still the same message (only one in .sent)
    assert len(msg.channel.sent) == 1


def test_transition_to_final_no_post_returns_false():
    """If the progress never posted (deferred window won the race),
    transition_to_final must return False so the caller falls
    through to the normal message.reply() path. Returning True
    would skip the reply and the user would see nothing."""
    msg = FakeMessage()
    prog = tool_progress.ToolProgress(msg)
    # Fire-and-forget the deferred post; stop() cancels it
    asyncio.run(prog.start_defer())
    asyncio.run(prog.stop())
    # No message was ever posted.
    assert len(msg.channel.sent) == 0
    # transition_to_final returns False; caller must post reply itself.
    ok = asyncio.run(prog.transition_to_final("Hello."))
    assert ok is False


def test_transition_to_final_after_stop_returns_false():
    """If the progress was already stopped (tool batch ran its
    stop() in the finally block before the final reply path),
    transition_to_final must not resurrect the message."""
    msg = FakeMessage()
    prog = tool_progress.ToolProgress(msg)
    asyncio.run(prog.start())
    # Simulate the tool batch running its stop() (deletes the message)
    asyncio.run(prog.stop())
    assert msg.channel.sent[0] in msg.channel.deleted
    # transition_to_final can't edit a deleted message
    ok = asyncio.run(prog.transition_to_final("Final reply"))
    assert ok is False
    # No edits beyond what the original stop() did
    # (the test only had the start-post + stop-delete; no edits)
    assert len(msg.channel.edited) == 0


def test_update_does_not_clobber_buffer_with_placeholder():
    """The bot's _on_tool_call_name callback calls
    ``update(tool_name, "generating…")`` when a tool name arrives
    before any reasoning. The literal 'generating…' must NOT
    overwrite the per-token reasoning buffer the tick() path
    accumulated. Otherwise the user loses the model's thought
    progression the moment a tool commits."""
    msg = FakeMessage()
    prog = tool_progress.ToolProgress(msg)
    asyncio.run(prog.start())
    # Build up a real reasoning buffer via ticks
    prog._last_edit = 0
    asyncio.run(prog.tick(reasoning_delta="I will check the disk. "))
    asyncio.run(prog.tick(reasoning_delta="Now looking at usage. "))
    # Buffer has the accumulated reasoning
    assert "I will check the disk." in prog._reasoning_buffer
    assert "Now looking at usage." in prog._reasoning_buffer
    # Now the bot's _on_tool_call_name fires with the placeholder
    prog._last_edit = 0
    asyncio.run(prog.update("shell", "generating…"))
    # The buffer is INTACT — the placeholder is treated as a
    # tool-name announcement, not a thought.
    assert "I will check the disk." in prog._reasoning_buffer
    assert "Now looking at usage." in prog._reasoning_buffer
    # The tool name IS set
    assert prog._current_tool == "shell"
    asyncio.run(prog.stop())


def test_concurrent_stop_during_deferred_post():
    """stop() called during the deferred post window must cancel
    the post task. Otherwise a 'working on it…' message lands
    AFTER the bot's reply has gone out — the exact orphan-message
    flicker the user complained about."""
    msg = FakeMessage()
    prog = tool_progress.ToolProgress(msg)
    # Fire-and-forget the deferred post
    asyncio.run(prog.start_defer())
    # stop() immediately — the deferred post is mid-sleep
    asyncio.run(prog.stop())
    # The post task was cancelled; the post never landed
    assert msg.channel.sent == []
    assert msg.channel.deleted == []


def test_concurrent_stop_during_tick():
    """A tick that races with stop() must be dropped silently.
    A slow tick that wins the lock AFTER stop() has run would
    otherwise try to edit a deleted message and 404."""
    msg = FakeMessage()
    prog = tool_progress.ToolProgress(msg)
    asyncio.run(prog.start())  # post lands
    # Manually flip the stopped flag to simulate a stop() that
    # raced in. The tick must bail early.
    prog._stopped = True
    edits_before = len(msg.channel.edited)
    asyncio.run(prog.tick(reasoning_delta="Late reasoning."))
    assert len(msg.channel.edited) == edits_before


def test_concurrent_progresses_isolated_per_channel():
    """The cross-channel bug under load: two channels each have
    their own progress. The bot has both in its per-channel dict.
    A tool batch in channel A finishing must NOT touch channel B's
    progress. Old single-attribute design had them stepping on each
    other."""
    import tools

    fake_progress_a = SimpleNamespace()
    fake_progress_b = SimpleNamespace()
    sig_a, sig_b = [], []
    fake_progress_a.notify_streaming = lambda: sig_a.append(True)
    fake_progress_b.notify_streaming = lambda: sig_b.append(True)
    bot = SimpleNamespace(
        _current_progress_by_channel={
            "100": fake_progress_a,
            "200": fake_progress_b,
        }
    )

    class _Stub(tools.Tool):
        def get_description(self):
            return ""

        async def execute(self, message, **kwargs):
            return ""

    inst = _Stub(bot)
    msg_a = SimpleNamespace(channel=SimpleNamespace(id=100))
    msg_b = SimpleNamespace(channel=SimpleNamespace(id=200))
    # Tool runs in channel A — should signal ONLY A's progress.
    inst._signal_streaming(msg_a)
    assert sig_a == [True]
    assert sig_b == []
    # Tool runs in channel B — should signal ONLY B's progress.
    sig_a.clear()
    inst._signal_streaming(msg_b)
    assert sig_a == []
    assert sig_b == [True]


def test_streaming_tick_inserts_space_between_glued_deltas():
    """Real-API regression: hit kimi-k2.6:cloud and verify the
    progress UI captures streaming deltas and space-joins them.

    The previous bug: SSE deltas that arrived with no whitespace
    boundary between them were concatenated into one run ("hello
    worldThe user wants me to look at the disk.") so _format_thinking
    saw a single sentence and the progress line never rolled. The fix
    inserts a space between deltas when neither side has a boundary.

    This test runs against the live provider configured in .env so
    it verifies the real streaming path end-to-end: HTTP -> SSE
    parsing -> on_token callback -> tick() -> progress message edit.
    Skipped if OLLAMA_BASE_URL is not set (CI / no network).
    """
    import os as _os
    from pathlib import Path as _Path
    # Load .env so OLLAMA_BASE_URL / OLLAMA_MODEL are visible in pytest
    _env_path = _Path(__file__).resolve().parent.parent / ".env"
    if _env_path.exists():
        for _line in _env_path.read_text(encoding="utf-8").splitlines():
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _k, _, _v = _line.partition("=")
            _os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))
    base_url = _os.getenv("OLLAMA_BASE_URL", "").rstrip("/")
    if not base_url:
        import pytest
        pytest.skip("OLLAMA_BASE_URL not set; real-API progress test skipped")
    model = _os.getenv("OLLAMA_MODEL", "kimi-k2.6:cloud")
    api_key = _os.getenv("OLLAMA_API_KEY", "")

    msg = FakeMessage()
    prog = tool_progress.ToolProgress(msg)

    pending: list = []

    async def drive():
        from providers import OllamaProvider as _Prov

        prov = _Prov(
            base_url=base_url,
            model=model,
            max_tokens=256,
            temperature=0.7,
            api_key=api_key,
            retry_attempts=1,
        )
        fb = _os.getenv("OLLAMA_FALLBACK_BASE_URL", "")
        if fb:
            prov._endpoints.append(
                type(prov._endpoints[0])(
                    "fallback",
                    fb,
                    _os.getenv("OLLAMA_FALLBACK_MODEL", ""),
                    _os.getenv("OLLAMA_FALLBACK_API_KEY", ""),
                    True,
                )
            )
        ok = await prov.initialize()
        assert ok, "provider failed to initialize"

        await prog.start()

        def on_token(tok):
            c = tok.get("content", "") or ""
            r = tok.get("reasoning", "") or ""
            if c or r:
                pending.append(
                    asyncio.create_task(
                        prog.tick(
                            reasoning_delta=c + r,
                            tool_name=tok.get("tool_name"),
                        )
                    )
                )

        messages = [
            {
                "role": "system",
                "content": "You are a test bot. Answer in 3-4 short sentences about cats.",
            },
            {
                "role": "user",
                "content": "Tell me about cats in 3-4 short sentences.",
            },
        ]
        result = await prov.generate_chat_completion(
            messages, on_token=on_token, timeout=120, max_tokens=256
        )
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        # Let any coalesced final tick render
        await asyncio.sleep(3.5)

        edits = [e.content for e in msg.channel.edited]
        assert edits, "progress message was never edited"
        # Buffer should be space-joined at word boundaries, not glued.
        # The original bug glued two letter-runs together with no space
        # between them ("hello worldThe user" — "d" then "T" with no
        # boundary). Numbers and hyphens around them (e.g. "12-16" from
        # the model itself) are not deltas that need spacing.
        import re as _re2
        buf = prog._reasoning_buffer
        buf_compact = _re2.sub(r"[\s\-\d_]+", "", buf)
        for glued in ["worldThe", "diskThen", "meowingPurring", "catsThey"]:
            if glued in buf_compact:
                raise AssertionError(
                    f"deltas were not space-joined: {glued!r} in {buf!r}"
                )
        # The progress message should show the model's rolling output
        # (the last _VISIBLE_BUDGET chars of the buffer). The buffer
        # must have grown from at least one tick — i.e. the progress
        # message captured the streaming text.
        assert prog._reasoning_buffer.strip(), (
            f"no streaming text was captured: {prog._reasoning_buffer!r}"
        )
        # At least one edit should reflect the captured text
        assert any(
            "thinking" in e or ":" in e for e in edits
        ), f"no useful progress rendered: {edits!r}"
        # Final response should contain the same text as the buffer.
        # ProviderResult is a str subclass, so str(result) IS the content
        # we care about. (The real bot reads the .choices[0].message
        # path; for this test we just want to confirm the stream and
        # the final agree.)
        final = str(result)
        import re as _re
        buf_words = set(_re.findall(r"\w+", buf.lower()))
        final_words = set(_re.findall(r"\w+", final.lower()))
        assert buf_words, f"empty buffer: {buf!r}"
        assert final_words, f"empty final: {final!r}"
        # Most of the words should overlap — kimi streamed them too
        overlap = len(buf_words & final_words)
        assert overlap >= min(5, len(buf_words) // 2), (
            f"buffer and final don't agree: buf={buf_words!r} final={final_words!r}"
        )
        await prog.stop()
        await prov.close()

    asyncio.run(drive())
