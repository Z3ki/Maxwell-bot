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
import contextlib
import logging
import re
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

# Soft target. The hard cap is _REASONING_PREVIEW_CHARS — we always cut at
# the last sentence terminator before that, but if none exists (the model
# is mid-sentence mid-stream), we cut at the last word boundary inside
# [:_SOFT_PREVIEW_CHARS] instead so the user never sees a half-word.
_SOFT_PREVIEW_CHARS = 110

# Sentence terminators we treat as a complete thought. The model's
# `reasoning` field frequently has no period at all; we still want to
# respect inline terminators like "!" or "?" so a one-line tool call
# rationale doesn't get cut at a random word boundary mid-clause.
_SENTENCE_TERMINATORS = ".!?"

# Matches sentence-ending punctuation: . ! ? followed by space/EOL or end of
# string. Used to snap the preview to a complete sentence when one is
# available inside the length cap. Models love to emit `thought` text without
# any terminator at all mid-stream, in which case the word-boundary fallback
# below takes over.
_TRAILING_SENTENCE_RE = re.compile(r"[" + re.escape(".!?") + r"](?:\s|$)")

# Word boundary for the soft fallback. We cut just before the word boundary
# inside the soft window, then append an ellipsis to mark the cut. Without
# this the user sees half-words like "the user wants me to lo" mid-generation
# — the original `len(reasoning) > cap: reasoning[:cap-1].rstrip()+"…"`
# behavior that produced this bug.
_WORD_BOUNDARY_RE = re.compile(r"\s\S+")


def _truncate_preview(
    text: str, soft: int = _SOFT_PREVIEW_CHARS, hard: int = _REASONING_PREVIEW_CHARS
) -> str:
    """Pick a preview of ``text`` that's exactly one coherent sentence when
    possible, and never cuts mid-word.

    Rules, in order (each rule wins if it produced something non-empty):
      1. If the text has a sentence terminator before ``hard`` chars,
         return everything up to and including that terminator (first
         complete thought — no ellipsis; it's actually complete).
      2. Otherwise if the text fits the ``soft`` window as a whole word,
         return it as-is.
      3. Otherwise cut at the last whitespace inside ``soft`` chars and
         append an ellipsis so the user can see the string was truncated.
      4. Fallback: hard-cut at ``hard`` chars and append an ellipsis (rare —
         only fires when the model emits one giant unbroken token).

    Why this exists: the previous version did `reasoning[:139].rstrip()+"…"`
    everywhere, which produced half-words (no real boundary search) and
    silently ate one or more visible chars via ``rstrip()``. The user saw
    truncated sentences that didn't even line up with the visible text.
    """
    if not text:
        return ""
    # Already fits cleanly in the soft window — no truncation needed.
    if len(text) <= soft:
        return text
    # Prefer the first complete sentence if it fits inside the hard cap.
    for m in _TRAILING_SENTENCE_RE.finditer(text[:hard]):
        # The match includes the punctuation + trailing whitespace; trim
        # the trailing whitespace so we don't double-space when this is
        # embedded in a longer line later.
        end = m.end()
        if end <= hard:
            return text[:end].rstrip()
    # No sentence boundary inside the hard cap. Cut at the last word
    # boundary inside the soft window so the visible preview ends on a
    # real word.
    head = text[:soft]
    last_ws = -1
    for m in _WORD_BOUNDARY_RE.finditer(head):
        last_ws = m.start()  # m.start() is the whitespace position
    if last_ws > 0:
        return text[:last_ws].rstrip() + "…"
    # Hard fallback: nothing even has whitespace in the soft window.
    if len(text) <= hard:
        return text
    return text[: max(1, hard - 1)].rstrip() + "…"


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

# Soft target. The hard cap is _REASONING_PREVIEW_CHARS — we always cut at
# the last sentence terminator before that, but if none exists (the model
# is mid-sentence mid-stream), we cut at the last word boundary inside
# [:_SOFT_PREVIEW_CHARS] instead so the user never sees a half-word.
_SOFT_PREVIEW_CHARS = 110

# Sentence terminators we treat as a complete thought. The model's
# `reasoning` field frequently has no period at all; we still want to
# respect inline terminators like "!" or "?" so a one-line tool call
# rationale doesn't get cut at a random word boundary mid-clause.
_SENTENCE_TERMINATORS = ".!?"

# Per-token ticks fire on EVERY streamed delta (reasoning + content). Most
# providers chunk into ~50-200 char deltas, so on a long create_site the SSE
# can emit 30+ deltas over 10s. We MUST coalesce — Discord's edit limit is
# 5 / 5s, and a naive "edit on every delta" would 429 us into silence.
# 1.5s is the right balance: fast enough that the user sees the model's
# thoughts scroll by (one update per ~1-2 reasoning chunks), slow enough to
# stay under the rate limit during a long generation.
_TOKEN_TICK_INTERVAL = 1.5


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
        # Number of successful edits we've made to the posted message. The
        # FIRST edit (the "working on it…" → "tool: …" transition) is exempt
        # from the edit cooldown so the user always sees the tool name appear
        # instantly — without this, a tool name arriving ~100ms after start()
        # posts would be rate-limited and discarded, leaving the user staring
        # at "working on it…" until stop() deletes it. Subsequent edits keep
        # the 2s cooldown for Discord rate-limit safety.
        self._edits_made: int = 0
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
        # Rolling buffer of reasoning text accumulated from per-token SSE
        # deltas. tick() grows this and renders the latest tail; we don't
        # render every tick (that would 429 us) but we DO keep the buffer
        # fresh so the next allowed edit has the latest model words.
        self._reasoning_buffer: str = ""
        # One-shot flag: after the first tool_name arrives via tick(), we
        # always allow the next tick flush through even if we're inside the
        # rate-limit window — the model committed to a tool, that's news.
        self._last_tool_name_announced: bool = False

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
            # Post whatever is cached right now: normally "working on it…",
            # but if a tool-name/reasoning callback already fired during the
            # grace sleep (fast generation — the model emits the tool call
            # within ~150ms), post the "tool: …" line directly instead of a
            # stale placeholder the user would never see updated.
            content = self._render()
            self._posted = await self._msg.channel.send(content)
            self._last_content = content
            if self._current_tool:
                # A tool was cached before we posted — that post IS the
                # first visible edit, so count it for rate-limiting.
                self._last_edit = time.monotonic()
                self._edits_made = 1
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
        logger.debug(
            f"[TP] update called: tool={tool_name!r} stopped={self._stopped} streaming={self._tool_streaming} platform={self._platform} posted={self._posted is not None}"
        )
        if self._stopped or self._tool_streaming:
            logger.debug(
                f"[TP] update skipped: stopped={self._stopped} streaming={self._tool_streaming}"
            )
            return
        # Telegram has no edit; the start() message is static.
        if self._platform != "discord":
            logger.debug("[TP] update skipped: non-discord platform")
            return

        reasoning = (reasoning or "").strip().replace("\n", " ")
        # Sentence-aware truncation. The previous `[:cap-1].rstrip()+"…"`
        # cut mid-word and silently dropped whitespace, producing visible
        # half-sentences. See _truncate_preview for the full rule list.
        reasoning = _truncate_preview(reasoning)
        # Cache the tool/reasoning NOW, before the posted-check. On fast
        # generations the tool-name callback can fire while start() is still
        # in its grace sleep (no posted message yet); caching here means
        # start() will post the "tool: …" line directly instead of a stale
        # "working on it…" the user would never see updated.
        prev_tool = self._current_tool
        self._current_tool = tool_name
        self._current_reason = reasoning
        # If we switched tools (e.g. shell → web_search in the same batch),
        # reset the rolling reasoning buffer so the new tool's preview
        # doesn't show the previous tool's tail. Tick-driven updates
        # accumulate the buffer per tool.
        if prev_tool and prev_tool != tool_name:
            self._reasoning_buffer = reasoning
            self._last_tool_name_announced = False
        elif not self._reasoning_buffer:
            self._reasoning_buffer = reasoning

        if not self._posted:
            # Post was never created (Discord rejected the first send, or
            # we're still inside start()'s grace sleep). The cached values
            # are picked up when start() posts (or the next update flushes).
            logger.debug("[TP] update skipped: no posted message (cached for start)")
            return

        content = self._render()
        logger.debug(
            f"[TP] render: content={content!r} last_content={self._last_content!r} same={content == self._last_content}"
        )
        if content == self._last_content:
            logger.debug("[TP] update skipped: content unchanged")
            return
        # Rate limit: skip if we edited too recently. The FIRST edit (the
        # "working on it…" → "tool: …" transition) is ALWAYS allowed through
        # so the user instantly sees which tool fired — without this, a tool
        # name arriving ~100ms after start() posts gets discarded and the
        # user only ever sees "working on it…". Subsequent edits keep the
        # 2s cooldown for Discord rate-limit safety.
        now = time.monotonic()
        logger.debug(
            f"[TP] rate check: edits_made={self._edits_made} now-last_edit={now - self._last_edit:.2f}s interval={_EDIT_INTERVAL_SECONDS}s"
        )
        if self._edits_made > 0 and now - self._last_edit < _EDIT_INTERVAL_SECONDS:
            # Rate limited (not the first edit) — the new content is cached,
            # so schedule a deferred flush once the window reopens instead of
            # dropping it on the floor.
            logger.debug(
                f"[TP] update deferred: rate limited ({now - self._last_edit:.2f}s < {_EDIT_INTERVAL_SECONDS}s)"
            )
            self._schedule_deferred_flush()
            return
        logger.debug(f"[TP] calling _flush with {content!r}")
        await self._flush(content)

    async def tick(
        self,
        reasoning_delta: str = "",
        tool_name: str | None = None,
    ) -> None:
        """Update the progress message with a rolling preview of the model's
        own streaming reasoning.

        Fired on every SSE reasoning/content delta. We coalesce hard —
        Discord's per-channel edit limit is 5 / 5s, and a 10s create_site
        can produce 30+ deltas. Most ticks are no-ops; we only do an actual
        edit every ``_TOKEN_TICK_INTERVAL`` seconds, with the latest
        accumulated reasoning text. The user sees their model's thoughts
        scroll by at human-readable speed instead of staring at "working on
        it…" for the whole generation.

        ``tool_name`` switches the UI from "thinking: …" to "<tool>: …"
        the moment the model commits to a tool call.

        Safe to call from anywhere. Never raises. No-op if we've been
        stopped or the tool started streaming its own output.
        """
        if self._stopped or self._tool_streaming:
            return
        if self._platform != "discord":
            return

        # Tool name switchover is cheap and one-shot — the first delta that
        # introduces a tool name should update the message immediately even
        # if a tick just fired (the model decided, that's news).
        if tool_name:
            self._current_tool = tool_name
        # Capture the switchover state BEFORE we mutate the flag, so the
        # rate-limit bypass below can see it as a real "first tool name
        # arrived" signal instead of always-True-after-the-fact.
        tool_name_switch = bool(tool_name and not self._last_tool_name_announced)
        if tool_name_switch:
            self._last_tool_name_announced = True

        # Accumulate the new reasoning delta. We don't render every tick —
        # we just keep growing the buffer, and the rate-limited flush below
        # picks up the latest tail.
        if reasoning_delta:
            tail = (self._reasoning_buffer + reasoning_delta).strip()
            # Bound growth so a 10k-char reasoning doesn't accumulate in RAM.
            if len(tail) > _REASONING_PREVIEW_CHARS * 4:
                tail = tail[-_REASONING_PREVIEW_CHARS * 4 :]
            self._reasoning_buffer = tail

        if not self._posted:
            return

        now = time.monotonic()
        # First tick is always allowed through (the user just posted
        # "working on it…" and is staring at it — show them SOMETHING).
        # Also: if a tool_name just arrived, always allow it through even
        # if we're inside the rate-limit window — the model committed to
        # a tool, that's news the user needs to see immediately.
        if (
            self._edits_made > 0
            and not tool_name_switch
            and now - self._last_edit < _TOKEN_TICK_INTERVAL
        ):
            return  # coalesce: the next tick or the deferred flush will fire

        if self._current_tool:
            preview = self._reasoning_buffer
            if preview:
                preview = _truncate_preview(preview.replace("\n", " ").strip())
                content = f"{self._current_tool}: {preview}"
            else:
                # Reasoning hadn't buffered yet when the tool name landed
                # (the tool-name callback arrived mid-token). Show the tool
                # alone — never fall back to "thinking: …" once we know
                # the model is acting on a tool, or the user perceives the
                # status as stuck on 'thinking' during the tool's execution.
                content = f"{self._current_tool}: working…"
        else:
            preview = self._reasoning_buffer
            if preview:
                preview = _truncate_preview(preview.replace("\n", " ").strip())
                content = f"thinking: {preview}"
            else:
                content = "working on it…"
        if content == self._last_content:
            return
        await self._flush(content)

    async def _flush(self, content: str) -> None:
        async with self._lock:
            if self._stopped or not self._posted:
                logger.debug(
                    f"[TP] _flush skipped: stopped={self._stopped} posted={self._posted is not None}"
                )
                return
            try:
                logger.debug(f"[TP] _flush calling edit({content!r})...")
                await self._posted.edit(content=content)
                self._last_edit = time.monotonic()
                self._last_content = content
                self._edits_made += 1
                logger.debug("[TP] _flush edit succeeded!")
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
            logger.debug(f"[TP] deferred flush firing with {content!r}")
            await self._flush(content)
        except asyncio.CancelledError:
            pass
        except Exception as e:  # noqa: BLE001
            logger.debug("Deferred progress flush failed: %s", e)

    def notify_streaming(self) -> None:
        """Tool signaled it's about to stream its own output.

        Marks the progress message for deletion at the next stop() — we
        don't want two parallel streams in the channel. Also cancels any
        pending deferred flush so a rate-limited reasoning update never
        edits the progress message ON TOP of the tool's own streamed
        output (which was producing a stray "shell: …" line after the
        shell block had already appeared). Idempotent.
        """
        self._tool_streaming = True
        if self._deferred_task and not self._deferred_task.done():
            self._deferred_task.cancel()

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
        # Drain any pending tick update BEFORE deleting so the user sees
        # the model's final thought/tool name at least once. Without this,
        # a tick that fired <_TOKEN_TICK_INTERVAL before stop() (very
        # common — the LLM often finishes streaming the same instant the
        # tool dispatch begins) gets its content cached in the buffer but
        # never rendered, so the user stares at the second-to-last update
        # while the tool runs. Bypasses the rate-limit for this one final
        # edit so the message reflects the latest model state when it
        # disappears. Best-effort: if the edit fails (message already
        # gone, channel lost perms) we still delete.
        try:
            tail = self._reasoning_buffer.strip()
            if tail or self._current_tool:
                if self._current_tool:
                    if tail:
                        preview = _truncate_preview(tail.replace("\n", " "))
                        content = f"{self._current_tool}: {preview}"
                    else:
                        content = f"{self._current_tool}: working…"
                else:
                    preview = tail.replace("\n", " ")
                    if preview:
                        content = f"thinking: {_truncate_preview(preview)}"
                    else:
                        content = ""
                if content and content != self._last_content:
                    with contextlib.suppress(Exception):
                        await self._posted.edit(content=content)
                        self._last_content = content
        except Exception as e:  # noqa: BLE001
            logger.debug("Final progress drain edit failed: %s", e)
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
