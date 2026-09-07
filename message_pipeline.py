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
                     of being dropped. Accepted directed messages never lose
                     their turn; overflow is deferred to the durable journal.
``RequestJournal``   durable request IDs and lifecycle metadata, so restarts
                     can retry work that has not begun irreversible effects.
``Watermarks``       per-channel high-water message id so a reconnect can
                     replay what the gateway missed.

None of these hold a lock across a provider call.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sqlite3
import time
from collections.abc import Awaitable, Callable, Iterator
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
        mid = getattr(self.message, "id", None)
        return "" if mid is None else str(mid).strip()


@dataclass
class _ChannelState:
    running: asyncio.Task | None = None
    running_entry: _Pending | None = None
    queue: list[_Pending] = field(default_factory=list)
    pump: asyncio.Task | None = None


class ReplyQueue:
    """Serialize reply generation per channel without dropping messages.

    The contract this replaces was "acquire a lock in 15 seconds or the
    message is gone". The contract here is "your turn comes after the one in
    front of you", which is what a person in the room expects.

    At most ``max_directed`` entries wait behind one running turn per channel.
    Soft chatter is evicted first. If only directed entries remain, a NEW
    directed request is returned as ``deferred`` for durable requeueing by the
    caller; accepted directed requests never expire or lose their place.

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

    def contains(self, channel_id: Any, message_id: Any) -> bool:
        """Whether a message is waiting or still owned by the running turn."""
        state = self._channels.get(str(channel_id or ""))
        mid = "" if message_id is None else str(message_id).strip()
        if state is None or not mid:
            return False
        return bool(
            (state.running_entry and state.running_entry.message_id == mid)
            or any(entry.message_id == mid for entry in state.queue)
        )

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
        ``"deferred"``, ``"dropped"``. Deferred directed requests were NOT
        accepted into memory and must be persisted/retried by the caller.
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

        if (
            entry.message_id
            and state.running_entry is not None
            and state.running_entry.message_id == entry.message_id
        ):
            return "duplicate"

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
                    merged = list(queued.burst or [queued.message])
                    for msg in entry.burst or [entry.message]:
                        if msg not in merged:
                            merged.append(msg)
                    entry.burst = merged[-24:]
                    state.queue[index] = entry
                    self._note_drop(cid, queued, "superseded")
                    self._ensure_pump(cid, state)
                    return "coalesced"

        if not self._make_room(cid, state):
            reason = "deferred" if entry.directed else "queue full"
            self._note_drop(cid, entry, reason)
            return "deferred" if entry.directed else "dropped"
        state.queue.append(entry)
        started = state.running is None or state.running.done()
        if started and len(state.queue) == 1:
            outcome = "started"
        else:
            outcome = "queued"
        self._ensure_pump(cid, state)
        return outcome

    def _expire(self, cid: str, state: _ChannelState, now: float) -> None:
        """Expire only soft chatter, never an accepted directed request."""
        kept: list[_Pending] = []
        for entry in state.queue:
            if not entry.directed and now - entry.enqueued_at > self.max_age:
                self._note_drop(cid, entry, "stale")
                continue
            kept.append(entry)
        state.queue = kept

    def _make_room(self, cid: str, state: _ChannelState) -> bool:
        if len(state.queue) < self.max_directed:
            return True
        for index, entry in enumerate(state.queue):
            if not entry.directed:
                del state.queue[index]
                self._note_drop(cid, entry, "queue full")
                return True
        return False

    def _note_drop(self, cid: str, entry: _Pending, why: str) -> None:
        logger.warning(
            "ReplyQueue dropped %s message %s in %s (%s)",
            "directed" if entry.directed else "soft",
            entry.message_id or "?",
            cid,
            why,
        )
        if self._on_drop is not None:
            try:
                self._on_drop(cid, entry, why)
            except Exception:
                logger.exception(
                    "ReplyQueue drop callback failed for message %s in %s",
                    entry.message_id,
                    cid,
                )

    def _ensure_pump(self, cid: str, state: _ChannelState) -> None:
        if self._closing or (state.pump is not None and not state.pump.done()):
            return
        state.pump = self._task_factory(
            asyncio.create_task(self._pump(cid), name=f"reply-queue-{cid}")
        )

    async def _pump(self, cid: str) -> None:
        """Run queued turns for one channel, strictly one at a time."""
        state = self._channels.get(cid)
        if state is None:
            return
        cancelled = False
        try:
            while state.queue and not self._closing:
                self._expire(cid, state, time.monotonic())
                if not state.queue:
                    break
                entry = state.queue.pop(0)
                handler = self._handler
                if handler is None:
                    return
                task = asyncio.ensure_future(handler(entry.message, entry.content))
                state.running = task
                state.running_entry = entry
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
                    pump = asyncio.current_task()
                    if self._closing or (pump is not None and pump.cancelling()):
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
                    state.running_entry = None
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            state.pump = None
            if not state.queue and state.running is None:
                self._channels.pop(cid, None)
            elif state.queue and not self._closing and not cancelled:
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
        """Stop all channels before awaiting cancellation; never restart pumps."""
        self._closing = True
        tasks = set()
        for state in self._channels.values():
            state.queue.clear()
            for task in (state.running, state.pump):
                if task is not None:
                    tasks.add(task)
                    if not task.done():
                        task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._channels.clear()


class RequestJournal:
    """Durable request IDs and metadata, never message content or credentials.

    Call ``recover()`` once at startup, then fetch Discord messages using
    ``pending()``. Mark ``effects_started`` BEFORE a tool call or message send:
    those requests are never automatically retried, even if the process died
    before it could record delivery. Reasons must be non-sensitive reason codes.

    Connections are opened, committed/rolled back, and closed per operation.
    Database errors propagate; a corrupt/unwritable journal is never replaced.
    ``update()`` explicitly permits terminal transitions for edited messages;
    ``accept()`` and ``begin()`` never silently reopen terminal requests.
    """

    _PENDING = ("received", "queued", "deferred", "running")
    _TERMINAL = ("delivered", "suppressed", "failed", "superseded")
    _TERMINAL_RETENTION = 10_000

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = os.fspath(path)
        if not self.path or self.path == ":memory:":
            raise ValueError("RequestJournal requires an on-disk database path")
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with self._transaction(write=True) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS requests (
                    message_id TEXT PRIMARY KEY,
                    channel_id TEXT NOT NULL,
                    author_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL CHECK (status IN (
                        'received', 'queued', 'deferred', 'running',
                        'delivered', 'suppressed', 'failed', 'superseded'
                    )),
                    directed INTEGER NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    effects_started INTEGER NOT NULL DEFAULT 0,
                    response_id TEXT,
                    reason TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS requests_pending "
                "ON requests(created_at, message_id) "
                "WHERE status IN ('received', 'queued', 'deferred', 'running') "
                "AND effects_started = 0"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS requests_terminal_age "
                "ON requests(updated_at DESC, message_id DESC) "
                "WHERE status IN ('delivered', 'suppressed', 'failed', 'superseded')"
            )
            self._prune(connection)

    @contextlib.contextmanager
    def _transaction(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5.0)
        try:
            connection.row_factory = sqlite3.Row
            with connection:
                if write:
                    connection.execute("BEGIN IMMEDIATE")
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _record(row: sqlite3.Row) -> dict[str, Any]:
        record = dict(row)
        record["id"] = record["message_id"]
        record["directed"] = bool(record["directed"])
        record["effects_started"] = bool(record["effects_started"])
        return record

    @staticmethod
    def _require(connection: sqlite3.Connection, message_id: Any) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM requests WHERE message_id = ?", (str(message_id).strip(),)
        ).fetchone()
        if row is None:
            raise KeyError(f"Unknown request ID: {message_id}")
        return row

    @staticmethod
    def _log_transition(previous: str | None, record: dict[str, Any]) -> None:
        logger.info(
            "request_transition %s",
            json.dumps(
                {
                    "message_id": record["message_id"],
                    "channel_id": record["channel_id"],
                    "previous_status": previous,
                    "status": record["status"],
                    "duration_ms": round(
                        max(0.0, record["updated_at"] - record["created_at"]) * 1000, 3
                    ),
                    "reason": record["reason"],
                    "attempts": record["attempts"],
                    "directed": record["directed"],
                    "effects_started": record["effects_started"],
                    "response_id": record["response_id"],
                },
                sort_keys=True,
            ),
        )

    def _prune(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            DELETE FROM requests WHERE message_id IN (
                SELECT message_id FROM requests
                WHERE status IN ('delivered', 'suppressed', 'failed', 'superseded')
                ORDER BY updated_at DESC, message_id DESC
                LIMIT -1 OFFSET ?
            )
            """,
            (self._TERMINAL_RETENTION,),
        )

    def accept(
        self,
        message_id: Any,
        channel_id: Any,
        author_id: Any = "",
        *,
        directed: bool = False,
    ) -> bool:
        """Insert a received request once; existing metadata is never reset."""
        mid = "" if message_id is None else str(message_id).strip()
        cid = "" if channel_id is None else str(channel_id).strip()
        if not mid or not cid:
            raise ValueError("Request message_id and channel_id must be nonempty")
        now = time.time()
        with self._transaction(write=True) as connection:
            inserted = connection.execute(
                """
                INSERT INTO requests (
                    message_id, channel_id, author_id, status, directed,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 'received', ?, ?, ?)
                ON CONFLICT(message_id) DO NOTHING
                """,
                (mid, cid, str(author_id or ""), bool(directed), now, now),
            ).rowcount
            if not inserted:
                return False
            record = self._record(self._require(connection, mid))
        self._log_transition(None, record)
        return True

    def get(self, message_id: Any) -> dict[str, Any] | None:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM requests WHERE message_id = ?", (str(message_id).strip(),)
            ).fetchone()
            return self._record(row) if row is not None else None

    def _change(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        status: str,
        *,
        reason: str = "",
        directed: bool | None = None,
        effects_started: bool | None = None,
        response_id: Any = None,
        attempts: int | None = None,
    ) -> dict[str, Any]:
        record = self._record(row)
        record.update(status=status, reason=reason, updated_at=time.time())
        if directed is not None:
            record["directed"] = bool(directed)
        if effects_started is not None:
            record["effects_started"] = bool(effects_started)
        if response_id is not None:
            record["response_id"] = str(response_id)
        if attempts is not None:
            record["attempts"] = attempts
        connection.execute(
            """
            UPDATE requests SET status = :status, reason = :reason,
                updated_at = :updated_at, directed = :directed,
                effects_started = :effects_started, response_id = :response_id,
                attempts = :attempts
            WHERE message_id = :message_id
            """,
            record,
        )
        return record

    def update(
        self,
        message_id: Any,
        status: str,
        *,
        reason: str = "",
        directed: bool | None = None,
        effects_started: bool | None = None,
        response_id: Any = None,
    ) -> None:
        """Persist an explicit transition; omitted optional fields are preserved."""
        if status not in self._PENDING + self._TERMINAL:
            raise ValueError(f"Unknown request status: {status}")
        with self._transaction(write=True) as connection:
            row = self._require(connection, message_id)
            record = self._change(
                connection,
                row,
                status,
                reason=reason,
                directed=directed,
                effects_started=effects_started,
                response_id=response_id,
            )
            if status in self._TERMINAL:
                self._prune(connection)
        self._log_transition(row["status"], record)

    def pending(
        self,
        limit: int = 100,
        *,
        after: tuple[float, str] | None = None,
    ) -> list[dict[str, Any]]:
        """Oldest unfinished, effect-free requests, with stable keyset paging.

        Pass ``(last_row["created_at"], last_row["message_id"])`` as ``after``
        to inspect the next page even when earlier requests remain busy. Start
        a fresh sweep without a cursor after reaching the end. Status updates
        and terminal pruning do not invalidate the cursor.
        """
        query = """
            SELECT * FROM requests
            WHERE status IN ('received', 'queued', 'deferred', 'running')
                AND effects_started = 0
        """
        parameters: list[Any] = []
        if after is not None:
            query += " AND (created_at, message_id) > (?, ?)"
            parameters.extend((float(after[0]), str(after[1])))
        query += " ORDER BY created_at, message_id LIMIT ?"
        parameters.append(max(0, int(limit)))
        with self._transaction() as connection:
            rows = connection.execute(query, parameters).fetchall()
            return [self._record(row) for row in rows]

    def begin(self, message_id: Any) -> None:
        """Atomically count an attempt and start an unfinished, effect-free turn."""
        with self._transaction(write=True) as connection:
            row = self._require(connection, message_id)
            if row["status"] not in self._PENDING or row["effects_started"]:
                raise ValueError("Cannot begin a terminal or effects-started request")
            record = self._change(
                connection, row, "running", attempts=row["attempts"] + 1
            )
        self._log_transition(row["status"], record)

    def recover(self) -> None:
        """Recover interrupted work once at startup, never replaying effects."""
        transitions = []
        with self._transaction(write=True) as connection:
            rows = connection.execute(
                """
                SELECT * FROM requests
                WHERE status IN ('received', 'queued', 'running')
                    OR (status = 'deferred' AND effects_started = 1)
                """
            ).fetchall()
            for row in rows:
                interrupted = bool(row["effects_started"])
                record = self._change(
                    connection,
                    row,
                    "failed" if interrupted else "deferred",
                    reason="interrupted_after_effect" if interrupted else "recovered",
                )
                transitions.append((row["status"], record))
            self._prune(connection)
        for previous, record in transitions:
            self._log_transition(previous, record)

    def close(self) -> None:
        """Lifecycle hook; every operation already closes its own connection."""


class Watermarks:
    """Per-channel highest processed message id, for gateway-gap recovery.

    A successful gateway resume can replay buffered events, but non-resumable
    reconnects and process restarts can leave gaps. Recording how far each
    channel was read lets the bot reconcile those gaps against Discord history,
    subject to message retention and channel permissions.

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
