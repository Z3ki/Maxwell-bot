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
_TOKEN_TICK_INTERVAL = 3.0

# Total visible budget. The last N characters of the streaming buffer
# are what the user sees. Long enough to show real thought, short
# enough to fit a Discord message and not overflow.
_VISIBLE_BUDGET = 200

# Hard-cap the internal buffer so a 50k-char reasoning run doesn't
# accumulate forever.
_HARD_FALLBACK_CHARS = 4000

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

    async def update(self, tool_name: str, reasoning: str = "") -> None:
        """Record a tool's progress. Coalesces edits; never raises.

        update() is called by the tool-dispatch callback with a NEW
        reasoning string each time the model thinks out loud. We
        REPLACE the buffer (not append) because the new string is
        the model's latest thought, not an accumulation. tick() is
        the path that APPENDS per-token streaming deltas.
        """
        if self._stopped or self._tool_streaming:
            return
        if self._platform != "discord":
            return

        prev_tool = self._current_tool
        self._current_tool = tool_name
        if prev_tool and prev_tool != tool_name:
            # Tool switched: also reset the announcement flag so the
            # new tool name bypasses the rate limit on first arrival.
            self._last_tool_name_announced = False
        if reasoning and reasoning != _GENERATING_PLACEHOLDER:
            # Normalize and set (not append — update() carries full
            # reasoning strings from the model's tool-call announcement,
            # not per-token deltas. tick() owns the per-token append.)
            self._reasoning_buffer = " ".join(reasoning.split())

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

    def _render(self) -> str:
        """Render the rolling tail of the buffer as the user sees it.

        The visible line is just the last ``_VISIBLE_BUDGET`` chars of
        the streaming buffer, with a 'thinking: ' prefix when no tool
        has been announced yet. Once a tool name arrives it gets
        prepended ('shell: …') and stays there.
        """
        tail = self._reasoning_buffer
        if not tail:
            return "working on it…"
        # Trim to a word boundary so we don't cut a word in half.
        if len(tail) > _VISIBLE_BUDGET:
            head = tail[-_VISIBLE_BUDGET:]
            last_ws = head.find(" ")
            if 0 < last_ws < len(head) - 1:
                head = head[last_ws + 1 :]
            tail = head
        if self._current_tool:
            return f"{self._current_tool}: {tail}"
        return f"thinking: {tail}"

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
        if self._stopped:
            return
        self._stopped = True
        if self._post_task and not self._post_task.done():
            self._post_task.cancel()
        if self._deferred_task and not self._deferred_task.done():
            self._deferred_task.cancel()
        if not self._posted:
            return
        if self._platform != "discord":
            self._posted = None
            return
        # Final drain so the user sees the model's last words before
        # the message disappears. Best-effort.
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

        When a tool batch completes with a final reply in hand, edit
        the existing progress message in place instead of deleting
        + reposting. Avoids the delete-then-fresh-post flicker.
        """
        if self._stopped or self._platform != "discord" or not self._posted:
            return False
        if not content:
            return False
        try:
            async with self._lock:
                if self._stopped or not self._posted:
                    return False
                await self._posted.edit(content=content)
                self._stopped = True
                self._last_content = content
                if self._deferred_task and not self._deferred_task.done():
                    self._deferred_task.cancel()
                return True
        except Exception as e:  # noqa: BLE001
            logger.debug("Progress transition-to-final edit failed: %s", e)
            self._posted = None
            return False


def make_progress(message: Any) -> ToolProgress:
    """Factory used by the bot's tool dispatch. Cheap; one per tool batch."""
    return ToolProgress(message)


__all__ = ["ToolProgress", "make_progress"]
