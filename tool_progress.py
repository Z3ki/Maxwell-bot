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


def _format_thinking(buffer: str) -> str:
    """Render a mid-stream reasoning buffer as a single coherent line.

    The user wanted the visible format to be:

        <last complete sentence before the previous dot>. <mid-stream
        tail with clean ending>.

    So a stream like

        \"message here blah blah. half a thought in progress. and then
        it ends.\"

    renders as

        \"blah. half a thought in progress. and then it ends.\"

    Mechanics:
      - Drop everything before the most recent sentence terminator
        (the "PRE") — that's the recent context window the user wanted
        a glimpse of. If no terminator exists, the whole buffer is the
        tail.
      - Keep the remaining tail verbatim (the "MIDDLE" — still streaming).
      - If the tail itself ends without a terminator, cut at the last
        word boundary inside the budget so we never expose a half-word.
      - Reattach a leading PRE rounded to a single clean sentence, so the
        visible message has the structure "<PRE>. <TAIL>" with no
        stranded period at the seam.
      - If the whole thing fits in the budget, just return it as-is.

    Returns the empty string only if the buffer itself is empty.
    """
    if not buffer:
        return ""
    text = " ".join(buffer.split())  # squash newlines/whitespace
    if len(text) <= _VISIBLE_BUDGET:
        return text
    # Find the most recent sentence terminator and split there.
    matches = list(_TRAILING_SENTENCE_RE.finditer(text))
    if matches:
        last_terminator = matches[-1]
        pre = text[: last_terminator.end()].rstrip()
        tail = text[last_terminator.end() :].lstrip()
    else:
        pre = ""
        tail = text
    # Trim pre to at most ~80 chars (keep ONE recent settled context).
    if len(pre) > 80:
        pre_head = pre[:80]
        cut = pre_head.rfind(" ")
        if cut > 0:
            pre = pre_head[:cut].rstrip() + "."
        else:
            pre = pre_head.rstrip() + "."
    # Now budget the whole preview to fit _VISIBLE_BUDGET.
    full_no_budget = (pre + " " + tail).strip() if pre else tail
    if len(full_no_budget) <= _VISIBLE_BUDGET:
        return full_no_budget
    # Tail too long; cut at the last word boundary inside the budget.
    # We always want the tail (most recent stream content), so shrink
    # the pre first, then trim tail if necessary.
    if pre and len(tail) <= _VISIBLE_BUDGET - len(pre) - 2:
        # Tail fits next to (smaller) pre. Trim pre to fit.
        remaining = _VISIBLE_BUDGET - len(tail) - 2  # for " " separator
        if len(pre) > remaining:
            pre = _trim_to_word_boundary(pre, remaining)
            if not pre.endswith((".", "!", "?")):
                pre = pre.rstrip() + "."
        return (pre + " " + tail).strip()
    # Tail alone is too long — drop the pre entirely if necessary.
    return _trim_to_word_boundary(tail, _VISIBLE_BUDGET)


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
        self._platform = str(
            getattr(message, "tool_platform", "discord") or "discord"
        )
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
                await self._msg.channel.send("working on it…")
            except Exception as e:  # noqa: BLE001
                logger.debug("Telegram progress post failed: %s", e)
                self._posted = None
            return

        try:
            await asyncio.sleep(_GRACE_BEFORE_FIRST_POST)
            if self._tool_streaming or self._stopped:
                return
            content = self._render()
            self._posted = await self._msg.channel.send(content)
            self._last_content = content
            if self._current_tool or content:
                self._last_edit = time.monotonic()
                self._edits_made = 1
        except Exception as e:  # noqa: BLE001
            logger.debug("Discord progress post failed: %s", e)
            self._posted = None

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
        if prev_tool and prev_tool != tool_name:
            self._reasoning_buffer = reasoning
            self._last_tool_name_announced = False
        elif not self._reasoning_buffer:
            self._reasoning_buffer = reasoning

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
            try:
                await self._posted.edit(content=content)
                self._last_edit = time.monotonic()
                self._last_content = content
                self._edits_made += 1
            except Exception as e:  # noqa: BLE001
                logger.debug(
                    "Progress edit failed (%s) — disabling further edits", e
                )
                self._posted = None

    def _has_meaningful_reasoning(self) -> bool:
        """True when the buffered reasoning has enough substance to be
        worth showing instead of the static 'working on it…' placeholder.

        The 2026-07-19 bug was a single 4-char SSE token flashing as
        'thinking: Cas' before any word boundary arrived. We hold off
        until the buffer contains a real sentence terminator OR is at
        least 6 chars of substance.
        """
        buf = self._reasoning_buffer.strip()
        if not buf:
            return False
        if any(c in buf for c in ".!?"):
            return True
        return len(buf) >= 6

    def _render(self) -> str:
        """Render the current state as a single user-facing line.

        Format:
          - Reasoning phase, no real content yet: "working on it…"
          - Reasoning phase with enough substance: "thinking: <sentence>"
            (we KEEP the "thinking:" prefix here — that's the signal
            that says "the model is reasoning").
          - Tool active: just "<reasoning>" with NO "tool:" prefix. The
            2026-07-19 UX report said "tool: dool shit here" reads as
            noise; show the model's reasoning verbatim.
          - Tool active but no reasoning buffer yet: "working…".

        The "<reasoning>" itself uses _format_thinking() above so the
        visible line follows the user's preferred structure of
        "<PRE sentence>.<TAIL with clean end>.".
        """
        if self._current_tool:
            buf = self._reasoning_buffer.strip()
            if buf:
                return _format_thinking(buf)
            return "working…"
        if not self._has_meaningful_reasoning():
            return "working on it…"
        buf = self._reasoning_buffer.strip()
        return f"thinking: {_format_thinking(buf)}"

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
