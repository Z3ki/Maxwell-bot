"""Live progress messages for tool calls.

What this is
------------
Every time the bot dispatches a non-terminal tool (or a batch of them), we
post a single "working on it…" status message in the channel. While the
tools run, we EDIT that same message with a rotating view of what's
happening (tool name, brief reasoning). When the tool batch finishes
success or fail, we DELETE the status message so the channel is left with
only the tool's real output (the streamed chunks, file attachments, the
final send_message reply).

Why bother
----------
Before this, the channel was silent during a 30s shell call or a 10s
web_search, and the only feedback was the post-hoc reply. Users thought
the bot was stuck. The typing indicator isn't enough for tool calls that
take longer than ~10s or for tools that have meaningful internal phases
(shell waiting on apt, sub_agent running kilo, etc).

Discord vs Telegram
-------------------
Discord supports message.edit() natively. The Telegram adapter in this
codebase does NOT (the channel adapter is a thin shim around the
sendMessage API and has no editMessage). So on Telegram we degrade to:
post a single "working…" message at start, delete it at end, no live
edits. The user still gets the liveness signal; the channel stays clean.

Rate limits
-----------
Discord's per-channel edit limit is 5 edits / 5s. A 30s tool call with
naive "edit every 100ms" would 429 us into oblivion. We coalesce edits
behind a 2-second min interval per progress object, plus a content
change detector so a no-op update doesn't burn a slot.

What about shell that streams its own chunks?
---------------------------------------------
Shell, send_file, image_generator, and friends post their own output
messages while running. When such a tool is mid-stream, our progress
message becomes redundant noise. We detect that via the
`streams_output` class attribute on the Tool — if True, we DELETE our
progress message the moment the tool starts (or right after the first
``__SENT__`` chunk goes out). That way the user sees:

    🔧 running shell: df -h   (the progress message, ~100ms)
    ```ansi                    (the actual shell output starts streaming)
    $ df -h
    Filesystem ...
    ```

…instead of two parallel streams of "I am doing it" + "here is the
result."

Don't confuse this with the LLM trace
------------------------------------
This is user-facing channel feedback, not the dashboard trace. The trace
is a separate file (data/llm_traces.json via tool_registry.record_reasoning)
and tracks the model's internal reasoning for audit. This file is
about what shows up in the Discord/Telegram channel.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

# Minimum seconds between two edits of the same progress message. Discord
# allows 5 edits / 5s; with 2s between edits we can do ~2-3 during a fast
# tool call (which is plenty) and never hit the rate limit on slow ones.
_EDIT_INTERVAL_SECONDS = 2.0

# If a tool's own streaming output started, give it this many seconds of
# grace before we post the progress message at all. Lets the tool's first
# chunk (e.g. shell's "$ command" header) land first so we don't pile two
# messages on top of each other.
_GRACE_BEFORE_FIRST_POST = 0.15

# Truncate the model's reasoning to this many chars in the progress UI.
# Long reasoning is fine for the trace; channel messages are glanceable.
_REASONING_PREVIEW_CHARS = 140


class ToolProgress:
    """One-per-channel ephemeral "working on it…" status message.

    Lifecycle:
        prog = ToolProgress(message)
        await prog.start()                         # posts first message
        await prog.update("shell", "checking disk") # edits (rate-limited)
        await prog.update("web_search", "…")       # coalesces if too soon
        await prog.stop()                          # deletes the message

    On Telegram, start() posts a single "working…" message and update()
    becomes a no-op (we don't have editMessage). stop() still deletes.

    Concurrent safety: a single instance is meant to be used by a single
    tool batch. Don't share it across batches.
    """

    def __init__(self, message: Any):
        self._msg = message
        self._platform = str(getattr(message, "tool_platform", "discord") or "discord")
        self._posted: Any = None  # the Message object once posted
        self._last_edit: float = 0.0
        self._last_content: str = ""
        self._lock = asyncio.Lock()
        self._stopped = False
        # One-sentence-at-a-time: only the CURRENT (tool, reasoning) is
        # shown. A new update() overwrites both. We don't grow a list, we
        # don't keep history — the channel sees one short line that
        # changes as tools come and go. Past tools' progress is implied
        # by the final reply; if users want audit they read the trace
        # file. This is the user-facing liveness signal, not a log.
        self._current_tool: str = ""
        self._current_reason: str = ""
        # True once the tool itself started streaming (via notify_streaming);
        # we then delete our message so the tool's output is the only thing
        # in the channel.
        self._tool_streaming = False
        # A deferred flush scheduled when an update() was rate-limited. The
        # model's reasoning often arrives mid-stream just *after* the tool-name
        # edit (reasoning is usually the first field in the tool-call arguments
        # JSON, so it lands within the 2s edit cooldown). Without this, that
        # reasoning is cached in _current_reason but never flushed — the user
        # sees "tool: generating…" for the whole 20s create_site generation
        # instead of the model's actual intent. The deferred task flushes the
        # latest cached content once the edit window reopens. Cancelled on
        # stop() so we never edit a message we've already deleted.
        self._deferred_task: asyncio.Task | None = None

    @property
    def posted(self) -> Any:
        """The Message we posted, or None if we never got that far."""
        return self._posted

    async def start(self) -> None:
        """Post the initial "working…" message. Idempotent: safe to call twice."""
        if self._stopped or self._posted is not None:
            return
        # Skip on platforms we can't edit AND can't reliably delete via
        # fetch_message (Telegram in this codebase). The typing indicator
        # is enough for the user to see activity.
        if self._platform != "discord":
            # Telegram: we still try to post a single ack message and delete
            # it at stop(). The TelegramChannelAdapter.send returns a dict,
            # not a discord.Message, so we stash a flag instead of the obj.
            try:
                self._posted = True  # sentinel
                await self._msg.channel.send("working on it…")
            except Exception as e:  # noqa: BLE001
                logger.debug("Telegram progress post failed: %s", e)
                self._posted = None
            return

        try:
            # Wait a hair so any "tool started streaming" signal beats us.
            # If the tool returns __SENT__ within _GRACE_BEFORE_FIRST_POST
            # we never post a duplicate.
            await asyncio.sleep(_GRACE_BEFORE_FIRST_POST)
            if self._tool_streaming or self._stopped:
                return
            self._posted = await self._msg.channel.send("working on it…")
        except Exception as e:  # noqa: BLE001
            logger.debug("Discord progress post failed: %s", e)
            self._posted = None

    async def update(self, tool_name: str, reasoning: str = "") -> None:
        """Record a tool's progress. Coalesces edits; never raises.

        The reasoning is the model's `thought` / `reasoning` field —
        exactly what the model wrote to justify this tool call. We show
        the first 140 chars so users see intent, not the whole inner
        monologue.
        """
        logger.info(
            f"[TP] update called: tool={tool_name!r} stopped={self._stopped} streaming={self._tool_streaming} platform={self._platform} posted={self._posted is not None}"
        )
        if self._stopped or self._tool_streaming:
            logger.info(
                f"[TP] update skipped: stopped={self._stopped} streaming={self._tool_streaming}"
            )
            return
        # Telegram has no edit; the start() message is static.
        if self._platform != "discord":
            logger.info("[TP] update skipped: non-discord platform")
            return
        if not self._posted:
            # Post was never created (Discord rejected the first send, or
            # we skipped start). Nothing to edit.
            logger.info("[TP] update skipped: no posted message")
            return

        reasoning = (reasoning or "").strip().replace("\n", " ")
        if len(reasoning) > _REASONING_PREVIEW_CHARS:
            reasoning = reasoning[: _REASONING_PREVIEW_CHARS - 1].rstrip() + "…"
        # One line, one tool. Overwrite whatever was showing before —
        # previous tool's name/reason is gone, not appended. The final
        # reply is the only persistent record of the tool chain.
        self._current_tool = tool_name
        self._current_reason = reasoning

        content = self._render()
        logger.info(
            f"[TP] render: content={content!r} last_content={self._last_content!r} same={content == self._last_content}"
        )
        if content == self._last_content:
            logger.info("[TP] update skipped: content unchanged")
            return
        # Rate limit: skip if we edited too recently. The new content
        # is already cached, so the next update() within 2s will
        # coalesce (and if the tool batch finishes before then, the
        # intermediate line is deleted anyway).
        now = time.monotonic()
        logger.info(
            f"[TP] rate check: now-last_edit={now - self._last_edit:.2f}s interval={_EDIT_INTERVAL_SECONDS}s"
        )
        if now - self._last_edit < _EDIT_INTERVAL_SECONDS:
            # Rate limited right now — but the new content (e.g. the model's
            # reasoning, which often lands just after the tool-name edit) is
            # already cached in _current_tool/_current_reason. Schedule a
            # deferred flush so it actually reaches the user once the edit
            # window reopens, instead of being silently dropped on the floor.
            logger.info(
                f"[TP] update deferred: rate limited ({now - self._last_edit:.2f}s < {_EDIT_INTERVAL_SECONDS}s)"
            )
            self._schedule_deferred_flush()
            return
        logger.info(f"[TP] calling _flush with {content!r}")
        await self._flush(content)

    async def _flush(self, content: str) -> None:
        async with self._lock:
            if self._stopped or not self._posted:
                logger.info(
                    f"[TP] _flush skipped: stopped={self._stopped} posted={self._posted is not None}"
                )
                return
            try:
                logger.info(f"[TP] _flush calling edit({content!r})...")
                await self._posted.edit(content=content)
                self._last_edit = time.monotonic()
                self._last_content = content
                logger.info("[TP] _flush edit succeeded!")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[TP] _flush edit FAILED: {e}")
                # Most common: 429 rate limit, 404 message deleted out
                # from under us, or channel perm lost. Either way: stop
                # trying; the user already has the bot's reply.
                logger.debug("Progress edit failed (%s) — disabling further edits", e)
                self._posted = None

    def _render(self) -> str:
        # One sentence. No emoji, no backticks — the user wants the
        # model's own thought, not a status widget. If we have a
        # reasoning string, that IS the message (it's the model's words,
        # lightly trimmed). The tool name is prefixed in plain text so
        # users know which tool is acting, separated by ": " — same vibe
        # as a shell prompt label without the decoration.
        if not self._current_tool:
            return "working on it…"
        if self._current_reason:
            return f"{self._current_tool}: {self._current_reason}"
        return f"{self._current_tool}: working…"

    def _schedule_deferred_flush(self) -> None:
        """Flush the latest cached content once the edit cooldown reopens.

        Called when ``update()`` was rate-limited. Only the most recent
        deferred flush matters, so any prior one is cancelled first. The
        task is a no-op if stop()/streaming overtook it.
        """
        if self._stopped:
            return
        if self._deferred_task and not self._deferred_task.done():
            self._deferred_task.cancel()
        elapsed = time.monotonic() - self._last_edit
        delay = max(0.05, _EDIT_INTERVAL_SECONDS - elapsed) + 0.05
        try:
            self._deferred_task = asyncio.create_task(self._deferred_flush(delay))
        except RuntimeError:
            # No running loop (e.g. called from a synchronous test context) —
            # nothing to defer to, the next update() will pick up the cache.
            self._deferred_task = None

    async def _deferred_flush(self, delay: float) -> None:
        """Wait out the rate-limit window, then flush whatever is cached now."""
        try:
            await asyncio.sleep(delay)
            if self._stopped or self._tool_streaming or not self._posted:
                return
            content = self._render()
            if content == self._last_content:
                return
            logger.info(f"[TP] deferred flush firing with {content!r}")
            await self._flush(content)
        except asyncio.CancelledError:
            pass
        except Exception as e:  # noqa: BLE001
            logger.debug("Deferred progress flush failed: %s", e)

    def notify_streaming(self) -> None:
        """Tool signaled it's about to stream its own output.

        Marks the progress message for deletion at the next stop() — we
        don't want two parallel streams in the channel. Idempotent.
        """
        self._tool_streaming = True

    async def stop(self) -> None:
        """Delete the progress message. Safe to call from finally blocks.

        On success, leaves the channel exactly as it was before start().
        On failure (we never posted, or delete threw), the worst case is
        one extra "⏳ working on it…" message — which the bot's own reply
        will visually out-shout anyway.
        """
        if self._stopped:
            return
        self._stopped = True
        # Cancel any pending deferred flush so we never edit a message we're
        # about to delete (and never leave a dangling task after the batch).
        if self._deferred_task and not self._deferred_task.done():
            self._deferred_task.cancel()
        if not self._posted:
            return
        if self._platform != "discord":
            # Telegram sentinel mode: try to delete the ack message by
            # looking up the most recent bot message in the chat. The
            # adapter doesn't expose a clean fetch-by-id, so we just
            # leave the ack and accept the minor noise — Telegram users
            # are used to bot acks. (And the sendMessage-then-delete
            # round trip is enough latency to make the ack feel laggy.)
            self._posted = None
            return
        try:
            await self._posted.delete()
        except Exception as e:  # noqa: BLE001
            # 404 = already gone (rare race with a moderator). 403 = perm
            # lost (channel got locked mid-tool-call). Either way, the
            # bot's final reply is the thing the user reads.
            logger.debug("Progress delete failed: %s", e)
        finally:
            self._posted = None


def make_progress(message: Any) -> ToolProgress:
    """Factory used by the bot's tool dispatch. Cheap; one per tool batch."""
    return ToolProgress(message)


__all__ = ["ToolProgress", "make_progress"]
