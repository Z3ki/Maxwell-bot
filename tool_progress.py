"""Live progress messages for tool calls.

What this is
------------
Every time the bot dispatches a non-terminal tool (or a batch of them), we
post a single "working on it…" status message in the channel. While the
tools run, we EDIT that same message with a rotating view of what's
happening (the model's own streamed reasoning, lightly trimmed). When the
tool batch finishes success or fail, we DELETE the status message so the
channel is left with only the tool's real output.

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
Discord's per-channel edit limit is 5 edits / 5s. A naive "edit every
100ms" would 429 us into oblivion. We coalesce edits behind a 2-second
min interval per progress object, plus a content change detector so a
no-op update doesn't burn a slot.

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
import re
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
# The 2026-07-19 directive: 1.5s made the progress message feel like it
# jumped straight to the last sentence — the user couldn't watch the model
# think. Slow this so each ~50-100 char chunk of reasoning actually
# shows as its own visible progression. Discord's 5/5s edit budget still
# gives us 3+ edits per turn, plenty.
_TOKEN_TICK_INTERVAL = 3.0

# Total visible budget. Long enough to show real thought, short enough to
# fit a Discord message and not overflow. The 2026-07-19 directive: 220
# chars clipped multi-paragraph reasoning to one sentence and made the
# bot look like it was skipping straight to the answer. Show the model's
# actual thinking, not a tweet-length summary.
_VISIBLE_BUDGET = 600

# Hard-cut fallback. If the model's stream is one giant unbroken token
# (no whitespace, no terminators) longer than this, we hard-cut.
_HARD_FALLBACK_CHARS = 640

# Fast-tool fix. start() is called before generation / tool dispatch and
# posts a 'working on it…' message. For fast paths (create_site finishes
# in ms, send_message dispatches instantly, pure-text replies) the
# progress message posts, sits for 50-200ms, then gets deleted by
# stop() — and a fresh message.reply(response) lands right after. The
# user sees: <progress> <deletion flicker> <reply>. Annoying.
#
# Fix: defer the FIRST post. If the tool batch finishes (stop() called)
# before the window elapses, no message is ever posted. The window has
# to be long enough that a real long tool (shell, web_search) will
# definitely still be running when it fires, and short enough that the
# user doesn't sit through silence before seeing the placeholder.
# 800ms is the empirical sweet spot: create_site / send_message /
# memory lookups all complete in <200ms, so they fall inside the
# window; real tools (shell, fetch_url, autonomy) take seconds.
_DEFERRED_POST_WINDOW = 0.8

# The string the bot uses as a placeholder when a tool name arrives
# before any reasoning. We use this to NOT clobber the per-token
# reasoning buffer the tick() path spent the last several seconds
# accumulating — if update() is called with this string, we treat it
# as a no-reasoning announcement and keep the existing buffer intact.
_GENERATING_PLACEHOLDER = "generating…"


# Matches sentence-ending punctuation: . ! ? followed by whitespace / EOL.
_TRAILING_SENTENCE_RE = re.compile(r"[.!?](?:\s|$)")


def _trim_to_word_boundary(text: str, max_chars: int) -> str:
    """Cut at the last whitespace inside ``[:max_chars]`` so the visible
    preview ends on a real word. Returns the prefix unchanged if it fits
    in max_chars as a clean cut."""
    if len(text) <= max_chars:
        return text
    head = text[:max_chars]
    last_ws = head.rfind(" ")
    if last_ws > 0:
        return head[:last_ws].rstrip()
    return head.rstrip()


def _last_sentence_in(text: str) -> str:
    """Return just the part of ``text`` up to (and including) the most
    recent sentence terminator. Returns "" if no terminator exists.
    """
    if not text:
        return ""
    matches = list(_TRAILING_SENTENCE_RE.finditer(text))
    if not matches:
        return ""
    last = matches[-1]
    return text[: last.end()].rstrip()


def _last_full_sentence(buffer: str) -> str:
    """Return the most recent complete sentence in ``buffer``.

    A 'complete sentence' is text from the previous terminator (or
    buffer start) up to and including the next terminator. If the
    buffer has multiple sentences, only the LAST one is returned.
    If the buffer has no terminator (the stream is mid-thought), the
    empty string is returned — we never show partial sentences on the
    progress line.

    The 2026-07-19 user directive: visible text must be a whole
    sentence, never a half. The user can read a full thought; they
    can't parse "the user wants me to look at the disk" without
    re-reading it when the rest arrives.
    """
    if not buffer:
        return ""
    text = " ".join(buffer.split())
    matches = list(_TRAILING_SENTENCE_RE.finditer(text))
    if not matches:
        return ""
    last = matches[-1]
    # If there's more than one match, the start of the last sentence
    # is right after the previous match's end. Otherwise it starts
    # at the beginning of the buffer.
    if len(matches) >= 2:
        prev = matches[-2]
        start = prev.end()
    else:
        start = 0
    return text[start : last.end()].strip()


def _format_thinking(buffer: str) -> str:
    """Render a mid-stream reasoning buffer as a single coherent line.

    The 2026-07-19 user directive: the visible line is the LAST complete
    sentence the model emitted. A growing multi-sentence block was tested
    and rejected — it ballooned the edit payload (more bytes per PATCH,
    more chance of 429) and forced the user to re-read the whole
    paragraph every time a new sentence arrived. One sentence, always,
    rolling forward as the model emits more terminators.

    The function is the public face of "what sentence to show". Under
    the hood it delegates to ``_last_full_sentence`` for the actual
    extraction so the two stay in lockstep.
    """
    return _last_full_sentence(buffer)


class ToolProgress:
    """One-per-channel ephemeral "working on it…" status message.

    Lifecycle:
        prog = ToolProgress(message)
        await prog.start()                              # posts first message
        await prog.update("shell", "checking disk")     # edits (rate-limited)
        await prog.update("web_search", "…")            # coalesces if too soon
        await prog.stop()                               # deletes the message

    On Telegram, start() posts a single "working…" message and update()
    becomes a no-op (no editMessage). stop() still deletes.

    Concurrent safety: a single instance is meant to be used by a
    single tool batch. Don't share it across batches.
    """

    def __init__(self, message: Any):
        self._msg = message
        self._platform = str(getattr(message, "tool_platform", "discord") or "discord")
        self._posted: Any = None
        # Background task for the deferred 'working on it…' post. None
        # when no post is scheduled, set during the window, cleared in
        # the task's finally block. ``stop()`` cancels it so a fast
        # tool batch never produces a flash-flicker.
        self._post_task: asyncio.Task | None = None
        self._last_edit: float = 0.0
        self._last_content: str = ""
        self._lock = asyncio.Lock()
        self._stopped = False
        self._current_tool: str = ""
        self._current_reason: str = ""
        self._tool_streaming = False
        self._edits_made: int = 0
        self._deferred_task: asyncio.Task | None = None
        # Rolling buffer of reasoning text accumulated from per-token
        # SSE deltas. tick() grows this; render() picks up the latest.
        self._reasoning_buffer: str = ""
        # First tool-name arrival is always allowed through the rate
        # limit immediately, so the user instantly sees the model
        # committed to a tool.
        self._last_tool_name_announced: bool = False
        # The very first tick() after start() is also exempt from the
        # rate limit — the user just posted 'working on it…' and is
        # staring at it; showing them SOMETHING (the model's first
        # reasoning tokens) is worth a Discord edit slot. Subsequent
        # ticks coalesce behind _TOKEN_TICK_INTERVAL.
        self._first_tick_done: bool = False

    @property
    def posted(self) -> Any:
        return self._posted

    async def start(self) -> None:
        """Begin tracking progress for this tool batch.

        Awaits until the first post is complete. Internally, that means
        a ``_DEFERRED_POST_WINDOW`` sleep on Discord so a fast tool
        batch (create_site in 50ms, send_message in 200ms, pure-text
        reply in 300ms) never produces a flash-delete-replace flicker
        in the channel. The user complaint that motivated this: the
        bot would post 'working on it…', delete it 100ms later when
        stop() ran, then send a fresh reply — three API events for
        what should have been one.

        ``start_defer()`` is the fire-and-forget variant for callers
        that can't await the window (the SSE reader callback, for
        example, must not be blocked by the post). Use it from any
        code path where the bot's reply is going to fire through
        ``transition_to_final`` or ``stop()`` and the caller doesn't
        need to know when the post lands.
        """
        if self._stopped or self._posted is not None or self._post_task is not None:
            return
        if self._platform != "discord":
            # Telegram: post the ack synchronously (no deferred window
            # for Telegram — the adapter has its own quirks handled
            # elsewhere).
            try:
                self._posted = True
                await self._post_reply("working on it…")
            except Exception as e:  # noqa: BLE001
                logger.debug("Telegram progress post failed: %s", e)
                self._posted = None
            return

        # Discord: wait the deferred window, then post IF the progress
        # is still alive and no tool has begun streaming. stop() racing
        # us sets _stopped=True; we bail silently.
        await self._do_deferred_post()

    async def start_defer(self) -> None:
        """Same as ``start()`` but returns immediately. The deferred
        post runs in a background task. Use this from the SSE reader
        callback where blocking the reader on the post would
        back-pressure the upstream provider.
        """
        if self._stopped or self._posted is not None or self._post_task is not None:
            return
        if self._platform != "discord":
            await self.start()
            return
        try:
            self._post_task = asyncio.create_task(self._do_deferred_post())
        except RuntimeError:
            # No running loop. Fall back to synchronous start.
            await self._do_deferred_post()

    async def _do_deferred_post(self) -> None:
        """Wait the deferred window, then post IF the progress is still
        alive and no tool has begun streaming its own output. Bails
        silently on every other path. Idempotent — both start() and
        start_defer() call into here.
        """
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
        """Actually post the 'working on it…' message. Called either
        eagerly (no-loop fallback) or after the deferred window."""
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
        """Post the progress message to the channel WITHOUT replying to
        the user's message.

        2026-07-21: was a reply to the triggering message, which made
        the bot visibly '@ mention' the user every time it started a
        tool call (Discord sends a desktop push for replies). User
        feedback: don't ping me for a 'working on it…' status. The
        progress is informational, not directed at anyone, so it goes
        to the channel as a plain message. Falls back to reply() if
        the message object doesn't expose a channel send (Telegram
        adapter, mocked tests).
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

    async def update(self, tool_name: str, reasoning: str = "") -> None:
        """Record a tool's progress. Coalesces edits; never raises."""
        if self._stopped or self._tool_streaming:
            return
        if self._platform != "discord":
            return

        reasoning = (reasoning or "").strip()
        prev_tool = self._current_tool
        self._current_tool = tool_name
        # Only overwrite the streaming buffer when the caller passed real
        # reasoning. The legacy _on_tool_call_name callback often fires
        # with an empty string just to announce the tool name — clobbering
        # the buffer in that case wipes the per-token reasoning the
        # tick() path just spent the last several seconds accumulating.
        #
        # Same trap when the caller passes the literal placeholder
        # ``_GENERATING_PLACEHOLDER`` ("generating…") — the bot's
        # ``_on_tool_call_name`` callback does ``reasoning or "generating…"``
        # which means a tool-name announcement that arrives before any
        # reasoning text is sent as the placeholder. Treating that as
        # "real reasoning" would wipe the per-token buffer just as
        # egregiously as an empty string. Both are announcements, not
        # thoughts; the tick() path owns the buffer.
        if reasoning and reasoning != _GENERATING_PLACEHOLDER:
            # 2026-07-21: same inter-delta spacing fix as tick() above —
            # if the existing buffer doesn't end in whitespace and the
            # new reasoning doesn't start with whitespace, insert a
            # space so _format_thinking can find sentence boundaries.
            prev = self._reasoning_buffer
            if prev and not prev[-1].isspace() and not reasoning[0].isspace():
                self._reasoning_buffer = (prev + " " + reasoning).strip()
            else:
                self._reasoning_buffer = (prev + reasoning).strip()
            self._current_reason = reasoning
        if prev_tool and prev_tool != tool_name:
            self._last_tool_name_announced = False

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
        """Update the progress message with a rolling preview of the
        model's streaming reasoning.

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
            # 2026-07-21: insert a space between deltas when neither
            # side has a boundary. Providers stream tokens glued with
            # no whitespace ("hello world" then "The user wants me to
            # look at" arrive as two chunks with no separator, so
            # concatenating them gives "hello worldThe user wants me
            # to look at" — a single 40-char run that _format_thinking
            # then sees as ONE sentence and renders verbatim). Insert
            # a space if the join point is between two non-space
            # characters so each delta stays readable.
            prev = self._reasoning_buffer
            if prev and reasoning_delta:
                last_ch = prev[-1]
                first_ch = reasoning_delta[0]
                if not last_ch.isspace() and not first_ch.isspace():
                    tail = (prev + " " + reasoning_delta).strip()
                else:
                    tail = (prev + reasoning_delta).strip()
            else:
                tail = (prev + reasoning_delta).strip()
            # Bound growth so 10k-char reasoning doesn't accumulate.
            if len(tail) > _HARD_FALLBACK_CHARS * 4:
                tail = tail[-_HARD_FALLBACK_CHARS * 4 :]
            self._reasoning_buffer = tail

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
            return  # coalesce

        content = self._render()
        if content == self._last_content:
            return
        await self._flush(content)
        self._first_tick_done = True

    async def _flush(self, content: str) -> None:
        async with self._lock:
            if self._stopped or not self._posted:
                return
            # Re-check the throttle UNDER the lock. tick() racing coroutines
            # can all see the same now - self._last_edit value and all pass
            # the gate simultaneously, which is what produced 17 PATCH/429
            # storms during a single kimi-k2.6 generation. Now only the first
            # coroutine that wins the lock actually edits; the rest see an
            # updated _last_edit and bail. Same 1.5s cadence, just serialized.
            #
            # Also exempt the very first tick after start() — the user just
            # saw "working on it…" and is waiting; the model's first coherent
            # sentence is worth an edit slot even though start() set the
            # throttle timestamp a few hundred ms ago.
            now = time.monotonic()
            first_flush = self._edits_made == 0 or not self._first_tick_done
            if not first_flush and now - self._last_edit < _TOKEN_TICK_INTERVAL:
                logger.debug(
                    "[PROGRESS] flush coalesced under lock: elapsed=%.3fs threshold=%.3fs",
                    now - self._last_edit,
                    _TOKEN_TICK_INTERVAL,
                )
                return
            try:
                await self._posted.edit(content=content)
                self._last_edit = time.monotonic()
                self._last_content = content
                self._edits_made += 1
                self._first_tick_done = True
            except Exception as e:  # noqa: BLE001
                logger.debug("Progress edit failed (%s) — disabling further edits", e)
                self._posted = None

    def _has_meaningful_reasoning(self) -> bool:
        """True when the buffered reasoning contains a FULL sentence
        worth showing instead of the static 'working on it…' placeholder.

        The 2026-07-19 user directive: only show whole sentences, never
        half-sentences mid-stream. A 6-char 'Cas' fragment or a
        mid-clause buffer like 'the user wants me to look at' should NOT
        be rendered — wait until the model emits a terminator and we
        have a complete thought to display. If the buffer is exactly one
        complete sentence (ends with terminator) we show it; if it has
        more after a terminator, the user will see the first complete
        sentence until a second one arrives, at which point the rolling
        window advances.
        """
        buf = self._reasoning_buffer.strip()
        if not buf:
            return False
        # Must contain at least one sentence terminator. The visible
        # line is a complete sentence, never a partial.
        return any(c in buf for c in ".!?")

    def _render(self) -> str:
        """Render the current state as a single user-facing line.

        Format:
          - No complete sentence in the buffer yet: the model hasn't
            told us anything worth reading. We deliberately do NOT
            prefix the tool name on this placeholder — the 2026-07-19
            directive is "don't show the tool name until there's
            something to show", because a bare '<tool>: generating…'
            flashes during a fast tool call (create_site) and is gone
            before the user can read it. Better: stay on the neutral
            'working on it…' until we have a sentence, then transition
            to '<tool>: <sentence>' or 'thinking: <sentence>'.

          - One or more complete sentences in the buffer:
              * With a tool committed: '<tool>: <last sentence>'
              * Without a tool: 'thinking: <last sentence>'

        The rolling-window effect comes from ``_format_thinking``
        returning the most-recent terminator-bounded sentence; as the
        model emits more sentences, the visible line advances.
        """
        formatted = _format_thinking(self._reasoning_buffer)
        if not formatted:
            # No complete sentence yet — stay on the neutral placeholder.
            # The 2026-07-19 user directive explicitly rejected a bare
            # '<tool>: generating…' here because it flashes for fast
            # tools and never gives the user time to read it.
            return "working on it…"
        if self._current_tool:
            return f"{self._current_tool}: {formatted}"
        return f"thinking: {formatted}"

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
        except asyncio.CancelledError:  # noqa: PERF203 — task cancellation, not error
            pass
        except Exception as e:  # noqa: BLE001
            logger.debug("Deferred progress flush failed: %s", e)

    def notify_streaming(self) -> None:
        self._tool_streaming = True
        if self._deferred_task and not self._deferred_task.done():
            self._deferred_task.cancel()

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        # Cancel the deferred-post task so a stop() called during the
        # 800ms window doesn't leave a half-sleeping task that wakes up
        # later and posts an orphan 'working on it…' right after the
        # tool's reply has gone out. The user complaint: a stray
        # progress message appearing AFTER the bot's reply because
        # the deferred post woke up after the reply was sent.
        if self._post_task and not self._post_task.done():
            self._post_task.cancel()
        if self._deferred_task and not self._deferred_task.done():
            self._deferred_task.cancel()
        if not self._posted:
            return
        if self._platform != "discord":
            self._posted = None
            return
        # Final drain so the user sees the model's last coherent thought
        # before the message disappears. Best-effort.
        try:
            content = self._render()
            if content and content != self._last_content:
                with contextlib.suppress(Exception):
                    await self._posted.edit(content=content)
                    self._last_content = content
        except Exception as e:  # noqa: BLE001
            logger.debug("Final progress drain edit failed: %s", e)
        try:
            await self._posted.delete()
        except Exception as e:  # noqa: BLE001
            logger.debug("Progress delete failed: %s", e)
        finally:
            self._posted = None

    async def transition_to_final(self, content: str) -> bool:
        """Replace the live progress message with the final reply.

        The fast-tool fix that started all this: when a tool batch
        completes with a final reply in hand, the bot used to do
        ``await progress.stop()`` (which deletes the 'working on it…'
        message) and then ``await message.reply(response)`` (which
        posts a fresh reply). Three API events, one of them a delete,
        and the user sees a flicker.

        Now: edit the existing progress message in place to show the
        final reply. The bot's tool dispatch calls this right before
        the final ``message.reply(...)`` falls through. If no message
        was ever posted (deferred window won the race, fast tool),
        the bot posts the reply normally — no double-post. If a
        message was posted, we hand it off: it BECOMES the reply, no
        flicker.

        Returns True if the message was transitioned (caller can
        skip its own reply()), False if there's nothing to transition
        (caller should post the reply itself).
        """
        if self._stopped or self._platform != "discord" or not self._posted:
            return False
        if not content:
            return False
        try:
            async with self._lock:
                # Re-check under the lock in case stop() raced us.
                if self._stopped or not self._posted:
                    return False
                await self._posted.edit(content=content)
                # Mark the instance as stopped without deleting the
                # message — it's now the final reply and should stay.
                # The ``_posted`` reference is kept so a followup
                # ``stop()`` call from a finally block is a no-op
                # (we already short-circuit on _stopped).
                self._stopped = True
                self._last_content = content
                if self._deferred_task and not self._deferred_task.done():
                    self._deferred_task.cancel()
                return True
        except Exception as e:  # noqa: BLE001
            logger.debug("Progress transition-to-final edit failed: %s", e)
            # Disable further edits so a followup stop() doesn't try
            # to delete a message we no longer control.
            self._posted = None
            return False


def make_progress(message: Any) -> ToolProgress:
    """Factory used by the bot's tool dispatch. Cheap; one per tool batch."""
    return ToolProgress(message)


__all__ = ["ToolProgress", "make_progress"]
