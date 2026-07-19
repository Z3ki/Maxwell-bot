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
_TOKEN_TICK_INTERVAL = 1.5

# Total visible budget. The preview is the model's reasoning rendered
# as "<short leading context sentence>.<full stream tail with clean
# sentence end>." Words inside the budget are whole, mid-word never.
_VISIBLE_BUDGET = 220

# Hard-cut fallback. If the model's stream is one giant unbroken token
# (no whitespace, no terminators) longer than this, we hard-cut.
_HARD_FALLBACK_CHARS = 240


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

    The user wanted the visible format to follow the last completed
    sentence. We always show the LAST full sentence (everything up to
    and including the most recent terminator), never partial fragments.

    If the buffer fits the budget and has only one sentence, return
    the whole buffer. If it has multiple sentences, return just the
    last one. The earlier sentences are noise — the user just needs
    the most recent thought.
    """
    if not buffer:
        return ""
    text = " ".join(buffer.split())
    # Always show the LAST complete sentence, never partials.
    last = _last_full_sentence(text)
    if last:
        # Truncate at word boundary if it overflows the budget.
        if len(last) > _VISIBLE_BUDGET:
            return _trim_to_word_boundary(last, _VISIBLE_BUDGET)
        return last
    # No terminator yet (mid-thought). Show a clipped-with-ellipsis
    # preview bounded by the budget so the user sees *something*.
    if len(text) <= _VISIBLE_BUDGET:
        return text
    return _trim_to_word_boundary(text, _VISIBLE_BUDGET)


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
        if self._stopped or self._posted is not None:
            return
        if self._platform != "discord":
            try:
                self._posted = True
                # Reply to the user's message (threads under it) rather
                # than a freestanding channel.send. The 2026-07-19 user
                # directive: a 'working on it…' status should reply to
                # the user, not just be a new top-level post in the
                # channel.
                await self._post_reply("working on it…")
            except Exception as e:  # noqa: BLE001
                logger.debug("Telegram progress post failed: %s", e)
                self._posted = None
            return

        try:
            await asyncio.sleep(_GRACE_BEFORE_FIRST_POST)
            if self._tool_streaming or self._stopped:
                return
            content = self._render()
            self._posted = await self._post_reply(content)
            self._last_content = content
            if self._current_tool or content:
                self._last_edit = time.monotonic()
                self._edits_made = 1
        except Exception as e:  # noqa: BLE001
            logger.debug("Discord progress post failed: %s", e)
            self._posted = None

    async def _post_reply(self, content: str) -> Any:
        """Post the progress message as a REPLY to the user's original
        message so it threads under their question. Falls back to a
        channel send if the message object doesn't support ``reply()``
        (Telegram adapter in this codebase, or mocked tests).
        """
        msg = self._msg
        reply_fn = getattr(msg, "reply", None)
        if reply_fn is not None and callable(reply_fn):
            try:
                return await reply_fn(content)
            except Exception as e:  # noqa: BLE001
                logger.debug("reply() failed, falling back to send: %s", e)
        channel = getattr(msg, "channel", None)
        send_fn = getattr(channel, "send", None)
        if send_fn is not None:
            return await send_fn(content)
        raise RuntimeError("No reply() or channel.send() available for progress post")

    async def update(self, tool_name: str, reasoning: str = "") -> None:
        """Record a tool's progress. Coalesces edits; never raises."""
        if self._stopped or self._tool_streaming:
            return
        if self._platform != "discord":
            return

        reasoning = (reasoning or "").strip()
        if not reasoning:
            return
        prev_tool = self._current_tool
        self._current_tool = tool_name
        self._current_reason = reasoning
        # update() carries a complete reasoning field, not a delta — so
        # we replace the buffer wholesale. Without this a second
        # update() with a longer string would not replace the first
        # half-sentence, and the visible line would stay stuck on the
        # earlier (partial) content.
        self._reasoning_buffer = reasoning
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
            tail = (self._reasoning_buffer + reasoning_delta).strip()
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
          - Reasoning phase, no complete sentence yet: 'working on it…'
          - Reasoning phase with at least one full sentence:
            'thinking: <last full sentence>'. Only complete sentences
            are shown; partial fragments wait for a terminator.
          - Tool active: '<tool>: <last full sentence>'. The tool name
            comes first (so the user instantly sees which tool the
            model committed to), followed by the latest complete
            sentence of the model's reasoning or the tool's own
            description. If there's no complete sentence yet, the
            line is just '<tool>: generating…'.

        2026-07-19 directive: only show whole sentences on the
        progress line, never partials. _has_meaningful_reasoning()
        gates the placeholder-vs-real-content switch on the presence
        of a sentence terminator; _last_full_sentence() picks which
        sentence to render.
        """
        if self._current_tool:
            last = _last_full_sentence(self._reasoning_buffer)
            if last:
                return f"{self._current_tool}: {last}"
            return f"{self._current_tool}: generating…"
        if not self._has_meaningful_reasoning():
            return "working on it…"
        return f"thinking: {_last_full_sentence(self._reasoning_buffer)}"

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
        except asyncio.CancelledError:
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


def make_progress(message: Any) -> ToolProgress:
    """Factory used by the bot's tool dispatch. Cheap; one per tool batch."""
    return ToolProgress(message)


__all__ = ["ToolProgress", "make_progress"]
