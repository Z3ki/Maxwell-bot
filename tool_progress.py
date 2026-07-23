"""Live progress messages for tool calls.

What this is
------------
Every time the bot dispatches a non-terminal tool (or a batch of them), we
post a single "working on it…" status message in the channel. While the
tools run, we EDIT that same message with a rolling tail of the model's
own streamed text. When the tool batch finishes success or fail, we DELETE
the status message so the channel is left with only the tool's real output.

Why bother
----------
Before this, the channel was silent during a 30s shell call or a 10s
web_search, and the only feedback was the post-hoc reply. Users thought
the bot was stuck. The typing indicator isn't enough for tool calls that
take longer than ~10s or for tools that have meaningful internal phases.

Discord vs Telegram
-------------------
Discord supports message.edit() natively. The Telegram adapter in this
codebase does NOT (thin shim around sendMessage). On Telegram we
degrade to: post a single "working…" message at start, delete at end,
no live edits.

Rate limits
-----------
Discord's per-channel edit limit is 5 edits / 5s. We coalesce edits
behind a 2-second min interval per progress object, plus a content
change detector so a no-op update doesn't burn a slot.

Don't confuse this with the LLM trace
------------------------------------
This is user-facing channel feedback. The trace file
(data/llm_traces.json via tool_registry.record_reasoning) tracks the
model's internal reasoning for audit. This file is about what shows up
in the channel.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

# Minimum seconds between two edits of the same progress message. Discord
# allows 5 edits / 5s; with 2s between edits we can do ~2-3 during a fast
# tool call (plenty) and never hit the rate limit on slow ones.
_EDIT_INTERVAL_SECONDS = 2.0

# If a tool's own streaming output started, give it this many seconds
# so the tool's first chunk lands first.
_GRACE_BEFORE_FIRST_POST = 0.15

# Per-token ticks fire on EVERY streamed delta. Most providers chunk
# into ~50-200 char deltas. We MUST coalesce or we'd 429 us into silence.
# 2026-07-21: lowered to 1.0s per user request "every 1 sec".
_TOKEN_TICK_INTERVAL = 1.0

# Total visible budget for the buffer tail (the model's words).
# The "thinking: " prefix (~9 chars) and the " → <tool>" tag are
# added on top, so the FINAL message is longer than this. We budget
# for the content only — the head + tag are negligible vs the
# Discord 2000-char limit.
# 2026-07-21: was 120, raised to 160 so a long phrase fits.
_VISIBLE_BUDGET = 160

# Hard-cap the internal buffer so a 50k-char reasoning run doesn't
# accumulate forever.
_HARD_FALLBACK_CHARS = 4000

# Characters that look ugly in a short status preview (raw JSON
# artefacts, escape sequences). We strip these from the visible text
# so the user sees readable prose, not {"arguments": {"body": "…}.
# 2026-07-21: the previous design showed raw mid-JSON previews like
# "create_site: :center;background :linear -gradient( 90deg ,red"
# which the user correctly flagged as "weird and bad".
_VISIBLE_STRIP_CHARS = set("{}[\\]\"`:")

# Code-snippet preview budget. The model often spends its time
# generating a large artifact (full HTML document for create_site, a
# multi-line shell command, etc.) that the user can't see at all while
# the tool runs. We surface a SHORT head of the artifact on the
# progress line so the user can watch the code scroll by in real time.
# 2026-07-21: per the user request "show a snippet of the code its
# generating" — the model is producing content, the user wants to
# SEE the content, not just a label.
_SNIPPET_BUDGET = 80

# Newline collapsing char. The model emits multi-line HTML; we squash
# runs of whitespace into a single space so the preview fits one line
# in Discord.
_SNIPPET_JOIN_CHAR = " "

# Fast-tool fix. start() is called before generation / tool dispatch and
# posts a 'working on it…' message. For fast paths the progress message
# posts, sits for a few ms, then gets deleted by stop() — and a fresh
# message.reply(response) lands right after. The user sees flicker.
#
# Fix: defer the FIRST post. If the tool batch finishes (stop() called)
# before the window elapses, no message is ever posted.
_DEFERRED_POST_WINDOW = 0.8

# Placeholder string for "tool name announced but no reasoning yet".
_GENERATING_PLACEHOLDER = "generating…"


def _latest_complete_sentence(text: str) -> str:
    """Return the LATEST complete sentence in ``text``.

    2026-07-21: user wants the progress line to update per sentence
    — when the model finishes a sentence, show THAT sentence; when
    it finishes the next one, show the new one. Not a rolling tail
    (which scrolls mid-sentence fragments), not pinned to the first
    sentence (which freezes). The user watches the model think in
    coherent sentence units.

    A "complete sentence" is one that ends with a '.', '!', or '?'
    FOLLOWED BY a space or end-of-string. The latest one is the
    substring from the start of the most recent sentence boundary
    (one space after a previous '.', '!', or '?') to the current
    terminator. If no terminator exists yet, return "" (caller
    decides fallback).
    """
    last_end = -1
    for i, ch in enumerate(text):
        if ch in ".!?":
            # Only count as a sentence end if it's followed by a
            # space or end of string (not "1.5" or "Dr." mid-sentence).
            if i + 1 >= len(text) or text[i + 1] == " ":
                last_end = i
    if last_end < 0:
        return ""
    # The latest sentence starts after the previous terminator.
    # Walk backwards from last_end to find the start of this sentence.
    # Look for a '.', '!', or '?' followed by a space BEFORE last_end.
    start = 0
    for j in range(last_end - 1, -1, -1):
        ch = text[j]
        if ch in ".!?":
            # Check if this terminator is followed by a space
            # (i.e. it ends a complete sentence, not mid-thought).
            if j + 1 < len(text) and text[j + 1] == " ":
                start = j + 2  # skip the terminator and the space
                break
            elif j + 1 >= len(text):
                # Trailing terminator: this is the previous sentence's end.
                start = j + 1
                break
    return text[start : last_end + 1].strip()


class ToolProgress:
    """One-per-channel ephemeral "working on it…" status message.

    Lifecycle:
        prog = ToolProgress(message)
        await prog.start()                              # posts first message
        await prog.update("shell", "checking disk")     # edits (rate-limited)
        await prog.tick(reasoning_delta="...")          # appends reasoning
        await prog.stop()                               # deletes the message

    The visible text is just the LAST ``_VISIBLE_BUDGET`` chars of the
    model's streaming output, prefixed once with the current tool name
    if one has been announced. No sentence detection, no regex, no
    term-paragraph reconstruction — just a sliding window of what the
    model has typed so far. The user watches the words scroll by.
    """

    def __init__(self, message: Any):
        self._msg = message
        self._platform = str(getattr(message, "tool_platform", "discord") or "discord")
        self._posted: Any = None
        self._post_task: asyncio.Task | None = None
        self._last_edit: float = 0.0
        self._last_content: str = ""
        self._lock = asyncio.Lock()
        self._stopped = False
        self._current_tool: str = ""
        self._tool_streaming = False
        self._edits_made: int = 0
        self._deferred_task: asyncio.Task | None = None
        # Rolling buffer of streaming text (the last thing the model said).
        # We just append + cap. render() slices the tail.
        self._reasoning_buffer: str = ""
        # Code-snippet preview buffer. While the model is generating a
        # large artifact (HTML body, shell script, etc.) the caller
        # feeds head-slices into here via update(snippet=...) and the
        # progress line shows a rolling tail — so the user can WATCH
        # the code being written, not just hear "thinking…".
        self._snippet_buffer: str = ""
        # First tool-name arrival is always allowed through the rate
        # limit immediately, so the user instantly sees the model
        # committed to a tool.
        self._last_tool_name_announced: bool = False
        # The very first tick() after start() is also exempt from the
        # rate limit — the user just posted 'working on it…' and is
        # staring at it; showing them SOMETHING (the model's first
        # reasoning tokens) is worth a Discord edit slot.
        self._first_tick_done: bool = False

    @property
    def posted(self) -> Any:
        return self._posted

    async def start(self) -> None:
        if self._stopped or self._posted is not None or self._post_task is not None:
            return
        if self._platform != "discord":
            try:
                self._posted = True
                await self._post_reply("working on it…")
            except Exception as e:  # noqa: BLE001
                logger.debug("Telegram progress post failed: %s", e)
                self._posted = None
            return
        await self._do_deferred_post()

    async def start_defer(self) -> None:
        if self._stopped or self._posted is not None or self._post_task is not None:
            return
        if self._platform != "discord":
            await self.start()
            return
        try:
            self._post_task = asyncio.create_task(self._do_deferred_post())
        except RuntimeError:
            await self._do_deferred_post()

    async def _do_deferred_post(self) -> None:
        try:
            await asyncio.sleep(_DEFERRED_POST_WINDOW)
            if self._stopped or self._tool_streaming or self._posted is not None:
                return
            await self._do_first_post()
        except asyncio.CancelledError:  # noqa: PERF203
            pass
        except Exception as e:  # noqa: BLE001
            logger.debug("Deferred progress post failed: %s", e)
        finally:
            self._post_task = None

    async def _do_first_post(self) -> None:
        try:
            await asyncio.sleep(_GRACE_BEFORE_FIRST_POST)
            if self._stopped or self._tool_streaming or self._posted is not None:
                return
            content = self._render()
            self._posted = await self._post_reply(content)
            self._last_content = content
            self._last_edit = time.monotonic()
            self._edits_made = 1
        except Exception as e:  # noqa: BLE001
            logger.debug("Discord progress post failed: %s", e)
            self._posted = None

    async def _post_reply(self, content: str) -> Any:
        """Post the progress message to the channel (NOT as a reply).

        Falls back to reply() if the message object doesn't expose a
        channel send (Telegram adapter, mocked tests).
        """
        msg = self._msg
        channel = getattr(msg, "channel", None)
        send_fn = getattr(channel, "send", None)
        if send_fn is not None and callable(send_fn):
            try:
                return await send_fn(content)
            except Exception as e:  # noqa: BLE001
                logger.debug("channel.send() failed, falling back to reply: %s", e)
        reply_fn = getattr(msg, "reply", None)
        if reply_fn is not None and callable(reply_fn):
            return await reply_fn(content)
        raise RuntimeError("No channel.send() or reply() available for progress post")

    def _append(self, text: str) -> None:
        """Append text to the rolling buffer, capped to _HARD_FALLBACK_CHARS.

        Providers stream tokens glued with no whitespace ('hello world'
        then 'The user wants me to look at' arrive as two chunks with
        no separator). Concatenating them gives 'hello worldThe user
        wants me to look at' — a single 40-char run that reads as
        gibberish. We insert a space at the join point when neither
        side has a boundary, then collapse runs of whitespace so
        consecutive deltas stay readable.
        """
        if not text:
            return
        prev = self._reasoning_buffer
        if prev and text:
            last_ch = prev[-1]
            first_ch = text[0]
            if not last_ch.isspace() and not first_ch.isspace():
                merged = prev + " " + text
            else:
                merged = prev + text
        else:
            merged = (prev + text) if prev else text
        merged = " ".join(merged.split())
        if len(merged) > _HARD_FALLBACK_CHARS:
            merged = merged[-_HARD_FALLBACK_CHARS:]
        self._reasoning_buffer = merged

    async def update(self, tool_name: str, reasoning: str = "", snippet: str = "") -> None:
        """Record a tool's progress. Coalesces edits; never raises.

        update() is called by the tool-dispatch callback with a NEW
        reasoning string each time the model thinks out loud. We
        REPLACE the buffer (not append) because the new string is
        the model's latest thought, not an accumulation. tick() is
        the path that APPENDS per-token streaming deltas.

        ``snippet`` is an optional code/artifact head (e.g. the first
        ~80 chars of an HTML body or a shell command). When set, it's
        shown on a second line of the progress message so the user
        can watch the model generate the artifact in real time.
        Pass ``""`` to clear it; omit to leave the current value.
        """
        if self._stopped or self._tool_streaming:
            return
        if self._platform != "discord":
            return

        prev_tool = self._current_tool
        self._current_tool = tool_name
        if prev_tool and prev_tool != tool_name:
            # Tool switched: also reset the announcement flag so the
            # new tool name bypasses the rate limit on first arrival,
            # and clear the snippet — a different tool = different
            # artifact.
            self._last_tool_name_announced = False
            self._snippet_buffer = ""
        if reasoning and reasoning != _GENERATING_PLACEHOLDER:
            # Normalize and set (not append — update() carries full
            # reasoning strings from the model's tool-call announcement,
            # not per-token deltas. tick() owns the per-token append.)
            self._reasoning_buffer = " ".join(reasoning.split())
        if snippet:
            # Replace the snippet buffer with a normalized head of
            # the new artifact. The user wants to see the code
            # scroll by, so we keep the LAST _SNIPPET_BUDGET chars
            # (the most recently generated portion is what reads as
            # "still being written").
            normalized = _SNIPPET_JOIN_CHAR.join(snippet.split())
            if len(normalized) > _SNIPPET_BUDGET:
                normalized = normalized[-_SNIPPET_BUDGET:]
            self._snippet_buffer = normalized

        if not self._posted:
            return

        content = self._render()
        if content == self._last_content:
            return
        now = time.monotonic()
        if self._edits_made > 0 and now - self._last_edit < _EDIT_INTERVAL_SECONDS:
            self._schedule_deferred_flush()
            return
        await self._flush(content)

    async def tick(
        self,
        reasoning_delta: str = "",
        tool_name: str | None = None,
    ) -> None:
        """Update the progress message with the model's streaming text.

        Fires on every SSE delta. Most ticks are no-ops; we coalesce
        behind ``_TOKEN_TICK_INTERVAL`` so Discord's 5/5s edit limit
        can breathe. ``tool_name`` is one-shot news: the first arrival
        bypasses the rate limit so the user sees the model commit.
        """
        if self._stopped or self._tool_streaming:
            return
        if self._platform != "discord":
            return

        if tool_name:
            self._current_tool = tool_name
        tool_name_switch = bool(tool_name and not self._last_tool_name_announced)
        if tool_name_switch:
            self._last_tool_name_announced = True

        if reasoning_delta:
            self._append(reasoning_delta)

        if not self._posted:
            return

        now = time.monotonic()
        first_tick = not self._first_tick_done
        if (
            self._edits_made > 0
            and not tool_name_switch
            and not first_tick
            and now - self._last_edit < _TOKEN_TICK_INTERVAL
        ):
            return  # coalesce (~2s between edits)

        content = self._render()
        if content == self._last_content:
            return
        await self._flush(content)
        self._first_tick_done = True

    async def _flush(self, content: str) -> None:
        async with self._lock:
            if self._stopped or not self._posted:
                return
            now = time.monotonic()
            first_flush = self._edits_made == 0 or not self._first_tick_done
            if not first_flush and now - self._last_edit < _TOKEN_TICK_INTERVAL:
                return
            try:
                await self._posted.edit(content=content)
                self._last_edit = time.monotonic()
                self._last_content = content
                self._edits_made += 1
                self._first_tick_done = True
            except Exception as e:
                # 2026-07-21: a Discord 429 (TOO MANY REQUESTS) used
                # to kill the entire progress UI for the rest of the
                # turn — the broad `except Exception: self._posted =
                # None` blanket was the same response for a real
                # network error and a rate-limit, so a single 429
                # would silently freeze the spinner. Detect 429s
                # explicitly, back off, and keep the post alive so the
                # next interval can try again.
                msg = str(e).lower()
                is_429 = "429" in msg or "too many requests" in msg or "rate" in msg
                if is_429:
                    # Push _last_edit forward to force a full
                    # _TOKEN_TICK_INTERVAL wait before the next
                    # attempt. Without this, a tight edit loop would
                    # keep hammering and getting 429'd.
                    self._last_edit = time.monotonic()
                    logger.debug(
                        "[PROGRESS] edit 429, backing off %ss",
                        _TOKEN_TICK_INTERVAL,
                    )
                    return
                logger.debug("Progress edit failed (%s) — disabling further edits", e)
                self._posted = None

    def _render(self) -> str:
        """Render the progress line. Up to two lines:

        1. Reasoning (one sentence) + tool tag — always present when
           we have any state to show.
        2. Code-snippet preview — present only when a snippet has been
           fed via update(snippet=...).

        2026-07-21: the model's reasoning is the useful content for
        the user — it explains what the model is about to do in its
        own words. We extract ONE complete sentence from the
        streaming buffer (everything from the start to the first
        '.', '!', or '?') and show that. Format: 'thinking: I'll
        send a friendly reply. → send_message'. Reasoning is the
        primary content; the tool name is a small trailing tag
        with an arrow.

        The buffer is stripped of JSON artefacts ({, }, \\, ", `, :)
        so mid-tool-call previews don't leak raw JSON. When the
        buffer has no terminator yet, we fall back to showing the
        first _VISIBLE_BUDGET chars of what's there.

        2026-07-21 (cont.): the user asked to also see a snippet of
        the code the model is generating. The artifact head (HTML
        body, shell cmd, message text, etc.) is appended on a
        second line, prefixed with '⤷ ' so it's visually distinct
        from the reasoning line. When the artifact is multi-line
        (HTML, code) we collapse whitespace so the preview fits
        one Discord line and is readable while scrolling.
        """
        raw = self._reasoning_buffer.strip()
        if not raw:
            # No reasoning yet. If a tool name was announced, say so
            # plainly so the user knows what the model committed to.
            if self._current_tool:
                head = f"using {self._current_tool}…"
            else:
                head = "working on it…"
        else:
            # Strip JSON / code artefacts that show up when the model is
            # mid-tool-call (custom protocol emits a bare-JSON object in
            # the text stream; the custom buffer keeps it in pending tail
            # but the raw deltas still flow into on_token). Without this
            # the user sees things like "create_site: :center;background
            # :linear -gradient( 90deg ,red" which is unreadable.
            cleaned = "".join(c for c in raw if c not in _VISIBLE_STRIP_CHARS)
            cleaned = " ".join(cleaned.split())
            # 2026-07-21: show the LATEST COMPLETE SENTENCE, not a
            # rolling tail. The user wants the progress line to update
            # each time the model finishes a new sentence — coherent
            # sentence units, not mid-thought fragments. The visible
            # line therefore advances by sentence, not by char.
            sentence = _latest_complete_sentence(cleaned)
            if not sentence:
                # No terminator yet — fall back to a partial preview
                # so the user sees liveness while the model is still
                # composing the first sentence. Trim to fit budget.
                partial = cleaned[:_VISIBLE_BUDGET]
                if len(cleaned) > _VISIBLE_BUDGET:
                    # Land on a word boundary.
                    last_ws = partial.rfind(" ")
                    if last_ws > _VISIBLE_BUDGET // 2:
                        partial = partial[:last_ws]
                sentence = partial
            elif len(sentence) > _VISIBLE_BUDGET:
                # Long sentence — keep the tail (most recent words)
                # trimmed to a word boundary.
                head2 = sentence[-_VISIBLE_BUDGET:]
                first_ws = head2.find(" ")
                if first_ws != -1 and first_ws < _VISIBLE_BUDGET // 2:
                    head2 = head2[first_ws + 1 :]
                sentence = head2.strip()
            tag = f" → {self._current_tool}" if self._current_tool else ""
            head = f"thinking: {sentence}{tag}"
        if self._snippet_buffer:
            return f"{head}\n⤷ {self._snippet_buffer}"
        return head

    def _schedule_deferred_flush(self) -> None:
        if self._stopped:
            return
        if self._deferred_task and not self._deferred_task.done():
            self._deferred_task.cancel()
        elapsed = time.monotonic() - self._last_edit
        delay = max(0.05, _EDIT_INTERVAL_SECONDS - elapsed) + 0.05
        try:
            self._deferred_task = asyncio.create_task(self._deferred_flush(delay))
        except RuntimeError:
            self._deferred_task = None

    async def _deferred_flush(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            if self._stopped or self._tool_streaming or not self._posted:
                return
            content = self._render()
            if content == self._last_content:
                return
            await self._flush(content)
        except asyncio.CancelledError:  # noqa: PERF203
            pass
        except Exception as e:  # noqa: BLE001
            logger.debug("Deferred progress flush failed: %s", e)

    def notify_streaming(self) -> None:
        self._tool_streaming = True
        if self._deferred_task and not self._deferred_task.done():
            self._deferred_task.cancel()

    async def stop(self) -> None:
        """Delete the progress message immediately — awaited, no edit.

        2026-07-22: the previous design scheduled the delete as a
        background task (fire-and-forget) so the caller wouldn't wait on
        a Discord round-trip. But the caller then posted the reply as an
        AWAITED send, which ran first — so the user saw the stale
        "working on it…" sit next to the reply for a beat before the
        background delete caught up ("too slow to delete"). Earlier
        versions also did a final drain edit() before the delete, which
        made the message visibly change content then vanish ("reappears
        then disappears").

        New contract: stop() AWAITS the delete. Yes, the caller waits
        ~100-300ms on one Discord round-trip, but that's the price of
        guaranteeing the message is gone BEFORE the reply posts — no
        race, no flicker, no stale placeholder. No final edit; the reply
        is the real content.
        """
        if self._stopped:
            return
        self._stopped = True
        if self._post_task and not self._post_task.done():
            self._post_task.cancel()
            self._post_task = None
        if self._deferred_task and not self._deferred_task.done():
            self._deferred_task.cancel()
            self._deferred_task = None
        if not self._posted:
            return
        if self._platform != "discord":
            self._posted = None
            return
        posted = self._posted
        self._posted = None
        try:
            await posted.delete()
        except Exception as e:  # noqa: BLE001
            logger.debug("Progress delete failed: %s", e)

    async def transition_to_final(self, content: str) -> bool:
        """Replace the live progress message with the final reply.

        When a tool batch completes with a final reply in hand, edit
        the existing progress message in place instead of deleting
        + reposting. Avoids the delete-then-fresh-post flicker.

        2026-07-21: was ``await self._posted.edit()`` which made the
        caller wait on a Discord round-trip before posting the reply.
        Now fire-and-forget so the reply is not blocked on Discord
        latency. Returns synchronously based on whether we have a
        posted message to edit; the actual edit happens in the
        background. If the bot is also racing a stop() (e.g. the
        tool's finally block already scheduled a delete), we still
        return True here and the background task will either land
        the edit or fall through to delete — either way the user
        sees one message.
        """
        if self._stopped or self._platform != "discord" or not self._posted:
            return False
        if not content:
            return False
        posted = self._posted
        self._stopped = True
        self._posted = None
        self._last_content = content
        if self._deferred_task and not self._deferred_task.done():
            self._deferred_task.cancel()
            self._deferred_task = None
        try:
            asyncio.create_task(self._background_transition(posted, content))
        except RuntimeError:
            with contextlib.suppress(Exception):
                await posted.edit(content=content)
        return True

    async def _background_transition(self, posted: Any, content: str) -> None:
        """Edit the message in place to the final reply. Fire-and-forget."""
        try:
            await posted.edit(content=content)
        except Exception as e:  # noqa: BLE001
            logger.debug("Progress transition-to-final edit failed: %s", e)


def make_progress(message: Any) -> ToolProgress:
    """Factory used by the bot's tool dispatch. Cheap; one per tool batch."""
    return ToolProgress(message)


__all__ = ["ToolProgress", "make_progress"]
