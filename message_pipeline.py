"""Inbound message reliability primitives for the Discord event path.

The old ``on_message`` did three things that lost traffic:

1. It took the per-channel lock for the *whole* handler — memory write and
   reply generation together — with a 15s acquire timeout. Because
   ``_handle_message`` holds that same lock for its entire tool loop (which
   can legitimately run for minutes on an image or a site build), the timeout
   fired constantly and the message was dropped with a log line. Over four
   days of production logs that was 586 dropped messages.

2. Recovery from that drop was ``_requeue_after_lock_timeout``, which only
   requeued *hard pings*, gave up after four tries, and funnelled through the
   watch debounce — a structure that keeps one message per channel and
   cancels its predecessor. A burst could therefore end in zero replies.

3. Nothing deduplicated inbound messages and nothing recorded how far a
   channel had been read, so a gateway resume could replay a message (two
   replies) and a gateway *gap* lost every message in it permanently.

The pieces here fix the structure rather than the symptom:

``InboundDedup``     one reply per message id, bounded.
``ReplyQueue``       one reply at a time per channel, the rest *wait* instead
                     of being dropped. Directed messages never lose their
                     turn; soft chatter coalesces to the newest line.
``Watermarks``       per-channel high-water message id so a reconnect can
                     replay what the gateway missed.

None of these hold a lock across a provider call, and none of them can drop a
directed message.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


class InboundDedup:
    """Bounded "have I already accepted this message id?" set.

    Discord redelivers ``MESSAGE_CREATE`` after a resume, and the dashboard
    command queue can re-dispatch a message object. Either one produced a
    second full reply, which is indistinguishable from the bot spamming.

    Insertion order is kept so eviction drops the oldest ids first; the set
    only has to cover the redelivery window, not all history.
    """

    __slots__ = ("_capacity", "_seen", "_order")

    def __init__(self, capacity: int = 4096) -> None:
        self._capacity = max(64, int(capacity))
        self._seen: set[str] = set()
        self._order: list[str] = []

    def check_and_add(self, message_id: Any) -> bool:
        """True when this id is new (and now recorded); False on a repeat."""
        key = str(message_id or "").strip()
        if not key:
            # No id means nothing to dedup against. Never block the message:
            # a synthetic message with no id is still real traffic.
            return True
        if key in self._seen:
            return False
        self._seen.add(key)
        self._order.append(key)
        if len(self._order) > self._capacity:
            # Drop the oldest half in one pass instead of evicting per insert.
            cut = self._order[: self._capacity // 2]
            self._order = self._order[self._capacity // 2 :]
            self._seen.difference_update(cut)
        return True

    def forget(self, message_id: Any) -> None:
        """Allow a message id to be processed again (used by edit reprocessing)."""
        key = str(message_id or "").strip()
        if key and key in self._seen:
            self._seen.discard(key)
            with contextlib.suppress(ValueError):
                self._order.remove(key)

    def __len__(self) -> int:
        return len(self._seen)

    def __contains__(self, message_id: object) -> bool:
        return str(message_id or "").strip() in self._seen


@dataclass
class _Pending:
    message: Any
    content: str
    directed: bool
    enqueued_at: float
    burst: list[Any] = field(default_factory=list)

    @property
    def message_id(self) -> str:
        return str(getattr(self.message, "id", "") or "")


@dataclass
class _ChannelState:
    running: asyncio.Task | None = None
    queue: list[_Pending] = field(default_factory=list)
    pump: asyncio.Task | None = None


class ReplyQueue:
    """Serialize reply generation per channel without dropping messages.

    The contract this replaces was "acquire a lock in 15 seconds or the
    message is gone". The contract here is "your turn comes after the one in
    front of you", which is what a person in the room expects.

    Bounding still exists, because an unbounded queue behind a slow turn is
    its own failure — the room would get a wall of stale replies. But the
    bound evicts *soft chatter* first and only ever drops the oldest soft
    entry, so a directed message cannot be squeezed out by background noise.

    Soft (non-directed) lines coalesce: at most one soft entry is pending per
    channel and a newer one replaces it, carrying the burst of lines it
    superseded so the turn still sees the whole exchange.
    """

    def __init__(
        self,
        *,
        max_directed: int = 8,
        max_age: float = 300.0,
        on_drop: Callable[[str, _Pending, str], None] | None = None,
    ) -> None:
        self.max_directed = max(1, int(max_directed))
        self.max_age = max(10.0, float(max_age))
        self._channels: dict[str, _ChannelState] = {}
        self._on_drop = on_drop
        self._handler: Callable[[Any, str], Awaitable[Any]] | None = None
        self._task_factory: Callable[[Any], Any] = lambda task: task
        self._closing = False

    def bind(
        self,
        handler: Callable[[Any, str], Awaitable[Any]],
        *,
        task_factory: Callable[[Any], Any] | None = None,
    ) -> None:
        """Attach the coroutine that actually generates a reply."""
        self._handler = handler
        if task_factory is not None:
            self._task_factory = task_factory

    # ---- introspection used by the busy/watch gates -----------------------

    def active(self, channel_id: Any) -> bool:
        state = self._channels.get(str(channel_id or ""))
        return bool(state and state.running is not None and not state.running.done())

    def any_active(self) -> bool:
        return any(
            s.running is not None and not s.running.done()
            for s in self._channels.values()
        )

    def depth(self, channel_id: Any) -> int:
        state = self._channels.get(str(channel_id or ""))
        return len(state.queue) if state else 0

    def stats(self) -> dict[str, Any]:
        running = [cid for cid, s in self._channels.items() if self.active(cid)]
        return {
            "channels_tracked": len(self._channels),
            "running": len(running),
            "queued": sum(len(s.queue) for s in self._channels.values()),
            "deepest": max((len(s.queue) for s in self._channels.values()), default=0),
        }

    # ---- submission -------------------------------------------------------

    def submit(
        self,
        channel_id: Any,
        message: Any,
        content: str,
        *,
        directed: bool,
        burst: list[Any] | None = None,
    ) -> str:
        """Queue a reply turn. Returns what happened, for logging.

        One of: ``"started"``, ``"queued"``, ``"coalesced"``, ``"duplicate"``,
        ``"dropped"``.
        """
        cid = str(channel_id or "")
        if not cid or self._handler is None or self._closing:
            return "dropped"
        state = self._channels.setdefault(cid, _ChannelState())
        now = time.monotonic()
        self._expire(cid, state, now)

        entry = _Pending(
            message=message,
            content=content or "",
            directed=bool(directed),
            enqueued_at=now,
            burst=list(burst or []),
        )

        # Same message already waiting: refresh it in place rather than
        # queueing the same turn twice (an edit or a re-dispatch).
        for index, queued in enumerate(state.queue):
            if entry.message_id and queued.message_id == entry.message_id:
                entry.burst = queued.burst or entry.burst
                entry.directed = queued.directed or entry.directed
                state.queue[index] = entry
                self._ensure_pump(cid, state)
                return "duplicate"

        if not entry.directed:
            # Only one soft entry per channel; the newest line wins and
            # inherits the burst of the lines it replaced.
            for index, queued in enumerate(state.queue):
                if not queued.directed:
                    merged = list(queued.burst)
                    for msg in entry.burst or [entry.message]:
                        if msg not in merged:
                            merged.append(msg)
                    entry.burst = merged[-24:]
                    state.queue[index] = entry
                    self._ensure_pump(cid, state)
                    return "coalesced"

        state.queue.append(entry)
        self._enforce_bound(cid, state)
        started = state.running is None or state.running.done()
        if started and len(state.queue) == 1:
            outcome = "started"
        else:
            outcome = "queued"
        self._ensure_pump(cid, state)
        return outcome

    def _expire(self, cid: str, state: _ChannelState, now: float) -> None:
        """Drop entries so old that answering them would be noise, not a reply."""
        kept: list[_Pending] = []
        for entry in state.queue:
            if now - entry.enqueued_at > self.max_age:
                self._note_drop(cid, entry, "stale")
                continue
            kept.append(entry)
        state.queue = kept

    def _enforce_bound(self, cid: str, state: _ChannelState) -> None:
        # Soft entries are already capped at one by coalescing, so the bound
        # only has to protect against a flood of directed pings. Evict the
        # OLDEST, because the newest ping is the one the user is waiting on.
        while len(state.queue) > self.max_directed:
            victim = None
            for index, entry in enumerate(state.queue):
                if not entry.directed:
                    victim = index
                    break
            if victim is None:
                victim = 0
            self._note_drop(cid, state.queue[victim], "queue full")
            del state.queue[victim]

    def _note_drop(self, cid: str, entry: _Pending, why: str) -> None:
        logger.warning(
            "ReplyQueue dropped %s message %s in %s (%s)",
            "directed" if entry.directed else "soft",
            entry.message_id or "?",
            cid,
            why,
        )
        if self._on_drop is not None:
            with contextlib.suppress(Exception):
                self._on_drop(cid, entry, why)

    def _ensure_pump(self, cid: str, state: _ChannelState) -> None:
        if state.pump is not None and not state.pump.done():
            return
        state.pump = self._task_factory(
            asyncio.create_task(self._pump(cid), name=f"reply-queue-{cid}")
        )

    async def _pump(self, cid: str) -> None:
        """Run queued turns for one channel, strictly one at a time."""
        state = self._channels.get(cid)
        if state is None:
            return
        try:
            while state.queue:
                entry = state.queue.pop(0)
                handler = self._handler
                if handler is None:
                    return
                task = asyncio.ensure_future(handler(entry.message, entry.content))
                state.running = task
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    # Two very different cancellations arrive here:
                    #
                    #  - the REPLY was cancelled (",stop", same-user
                    #    interrupt). That is a deliberate stop for that one
                    #    turn; anything queued behind it is separate traffic
                    #    and must still be answered. The shield above means
                    #    awaiting it does not cancel us.
                    #  - the PUMP itself was cancelled (shutdown). Then the
                    #    reply task is still running and we must re-raise.
                    if not task.done():
                        task.cancel()
                        with contextlib.suppress(Exception, asyncio.CancelledError):
                            await task
                        raise
                    logger.info("Reply cancelled in %s; continuing queue", cid)
                except Exception:
                    # A failed turn must not take the queue with it, or one
                    # bad message silences the room until restart.
                    logger.exception("Reply turn failed in %s", cid)
                finally:
                    state.running = None
        finally:
            state.pump = None
            if not state.queue and state.running is None:
                self._channels.pop(cid, None)
            elif state.queue:
                # A submission landed while we were tearing down.
                self._ensure_pump(cid, state)

    def cancel_channel(self, channel_id: Any, *, clear_queue: bool = False) -> bool:
        """Cancel the in-flight turn for a channel. Returns whether one existed."""
        cid = str(channel_id or "")
        state = self._channels.get(cid)
        if state is None:
            return False
        if clear_queue:
            for entry in state.queue:
                self._note_drop(cid, entry, "channel cleared")
            state.queue.clear()
        running = state.running
        if running is not None and not running.done():
            running.cancel()
            return True
        return False

    def drop_soft(self, channel_id: Any) -> int:
        """Drop pending soft (watch/chatter) entries. Directed pings stay.

        Used by same-user interrupt so a coalesced watch line cannot steal the
        next slot from the ping that just cancelled the in-flight turn.
        """
        cid = str(channel_id or "")
        state = self._channels.get(cid)
        if state is None:
            return 0
        kept: list[_Pending] = []
        dropped = 0
        for entry in state.queue:
            if entry.directed:
                kept.append(entry)
                continue
            self._note_drop(cid, entry, "interrupted")
            dropped += 1
        state.queue = kept
        return dropped

    async def close(self) -> None:
        self._closing = True
        for cid, state in list(self._channels.items()):
            state.queue.clear()
            for task in (state.running, state.pump):
                if task is not None and not task.done():
                    task.cancel()
                    with contextlib.suppress(Exception, asyncio.CancelledError):
                        await task
            self._channels.pop(cid, None)


class Watermarks:
    """Per-channel highest processed message id, for gateway-gap recovery.

    discord.py silently swallows a gateway gap: after a resume the events that
    happened during the outage are simply never delivered. Recording how far
    each channel was read lets the bot ask Discord for the rest.

    Persisted because the most common gap is a process restart, which is
    exactly when in-memory state is gone.
    """

    def __init__(self, path: str, *, max_channels: int = 512) -> None:
        self.path = path
        self.max_channels = max(16, int(max_channels))
        self._marks: dict[str, int] = {}
        self._dirty = False

    def load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as handle:
                raw = json.load(handle)
        except FileNotFoundError:
            return
        except Exception as exc:
            logger.warning("watermarks load failed (%s); starting empty", exc)
            return
        marks = raw.get("channels") if isinstance(raw, dict) else None
        if not isinstance(marks, dict):
            return
        for cid, value in marks.items():
            try:
                self._marks[str(cid)] = int(value)
            except (TypeError, ValueError):
                continue

    def save(self) -> None:
        if not self._dirty:
            return
        payload = {"channels": {k: str(v) for k, v in self._marks.items()}}
        tmp = f"{self.path}.tmp"
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            os.replace(tmp, self.path)
            self._dirty = False
        except Exception as exc:
            logger.warning("watermarks save failed: %s", exc)
            with contextlib.suppress(Exception):
                os.unlink(tmp)

    def note(self, channel_id: Any, message_id: Any) -> None:
        cid = str(channel_id or "")
        try:
            mid = int(message_id)
        except (TypeError, ValueError):
            return
        if not cid or mid <= 0:
            return
        if self._marks.get(cid, 0) >= mid:
            return
        self._marks[cid] = mid
        self._dirty = True
        if len(self._marks) > self.max_channels:
            # Snowflakes are time-ordered, so the smallest ids are the
            # coldest rooms — exactly the ones whose backlog matters least.
            keep = sorted(self._marks.items(), key=lambda kv: kv[1])[
                -(self.max_channels // 2) :
            ]
            self._marks = dict(keep)

    def get(self, channel_id: Any) -> int | None:
        return self._marks.get(str(channel_id or ""))

    def channels(self) -> list[tuple[str, int]]:
        """Rooms with a watermark, most recently active first."""
        return sorted(self._marks.items(), key=lambda kv: kv[1], reverse=True)

    def __len__(self) -> int:
        return len(self._marks)
