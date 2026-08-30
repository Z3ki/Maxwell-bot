"""Concurrency primitives used by the Discord event handlers.

The queue is intentionally keyed by guild and channel: work in one room cannot
hold a global lock while a provider or tool is slow. Blocking filesystem work
should be passed through ``offload`` at its call site.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


async def offload(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run synchronous I/O without occupying the event-loop thread."""
    if kwargs:
        return await asyncio.to_thread(lambda: func(*args, **kwargs))
    return await asyncio.to_thread(func, *args)


@dataclass
class _Work:
    callback: Callable[[], Awaitable[Any]]
    result: asyncio.Future[Any]


class ChannelWorkQueues:
    """Bounded FIFO workers, one independent queue per (guild, channel)."""

    def __init__(self, max_pending: int = 8) -> None:
        if max_pending < 1:
            raise ValueError("max_pending must be positive")
        self.max_pending = max_pending
        self._queues: dict[tuple[int, int], asyncio.Queue[_Work | None]] = {}
        self._workers: dict[tuple[int, int], asyncio.Task[None]] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    async def submit(self, guild_id: int, channel_id: int,
                     callback: Callable[[], Awaitable[Any]]) -> Any:
        key = (int(guild_id), int(channel_id))
        result: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        async with self._lock:
            if self._closed:
                result.cancel()
                raise RuntimeError("channel work queues are closed")
            queue = self._queues.setdefault(key, asyncio.Queue(self.max_pending))
            worker = self._workers.get(key)
            if worker is None or worker.done():
                worker = asyncio.create_task(self._run(key, queue), name=f"channel-worker-{key[0]}-{key[1]}")
                self._workers[key] = worker
            try:
                queue.put_nowait(_Work(callback, result))
            except asyncio.QueueFull:
                result.cancel()
                raise RuntimeError("channel queue is full; try again shortly") from None
        return await result

    async def _run(self, key: tuple[int, int], queue: asyncio.Queue[_Work | None]) -> None:
        current = asyncio.current_task()
        try:
            while True:
                work = await queue.get()
                try:
                    if work is None:
                        return
                    if work.result.cancelled():
                        # The submitter may have timed out or been cancelled
                        # while this item was waiting. Do not execute a
                        # callback whose caller no longer exists.
                        continue
                    try:
                        work.result.set_result(await work.callback())
                    except asyncio.CancelledError:
                        if not work.result.done():
                            work.result.cancel()
                        raise
                    except Exception as exc:
                        if not work.result.done():
                            work.result.set_exception(exc)
                finally:
                    queue.task_done()
                # Workers are demand-driven. Removing an idle worker bounds
                # memory for channels that are used once, while taking the
                # same lock as submit prevents a new item from being lost
                # between the empty check and worker teardown.
                async with self._lock:
                    if (
                        self._workers.get(key) is current
                        and queue.empty()
                    ):
                        self._workers.pop(key, None)
                        if self._queues.get(key) is queue:
                            self._queues.pop(key, None)
                        return
        finally:
            # Cancellation (including close()) must wake every submitter whose
            # work was still queued. Otherwise their Future hangs forever.
            while True:
                try:
                    pending = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if pending is not None and not pending.result.done():
                    pending.result.cancel()
                queue.task_done()
            async with self._lock:
                if self._workers.get(key) is current:
                    self._workers.pop(key, None)
                if self._queues.get(key) is queue:
                    self._queues.pop(key, None)

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
            workers = list(self._workers.values())
            self._workers.clear()
            self._queues.clear()
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)


class FairSemaphore:
    """Admission control for a small pool of slots, shared by many rooms.

    A plain semaphore under load is a scramble: every waiter is woken, and
    whoever the loop happens to schedule first wins. With two slots and a
    dozen busy servers that means one chatty room can take slot after slot
    while a quiet server waits minutes for its single question.

    Admission here is decided, not raced. Among the current waiters the
    winner is the one with

      1. the better priority — a person waiting always outranks a background
         tick, no matter how long the tick has queued;
      2. then the least-recently-served key — one turn per room before any
         room gets a second, so a burst in one guild cannot starve the rest;
      3. then arrival order, as the tie-break for a key's first turn.

    Waiters that time out or are cancelled remove themselves and re-notify,
    so a departure can promote whoever was behind them.
    """

    def __init__(
        self,
        capacity: int = 2,
        *,
        priorities: tuple[str, ...] = ("user", "background"),
        history: int = 512,
    ) -> None:
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self._capacity = int(capacity)
        self._rank = {name: index for index, name in enumerate(priorities)}
        self._default_rank = len(priorities)
        self._history = max(16, int(history))
        self._active = 0
        self._seq = 0
        self._waiters: list[dict[str, Any]] = []
        # key -> monotonic time it was last admitted. Bounded; see _mark_served.
        self._last_served: dict[str, float] = {}
        self._cond = asyncio.Condition()

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def active(self) -> int:
        return self._active

    @property
    def waiting(self) -> int:
        return len(self._waiters)

    async def set_capacity(self, capacity: int) -> None:
        """Resize at runtime. Growing it wakes whoever is queued."""
        capacity = max(1, int(capacity))
        async with self._cond:
            if capacity == self._capacity:
                return
            self._capacity = capacity
            self._cond.notify_all()

    def _mark_served(self, key: str, now: float) -> None:
        self._last_served[key] = now
        if len(self._last_served) > self._history:
            # Drop the coldest half in one pass rather than evicting on every
            # admission. Anything dropped simply looks new again, which puts
            # it at the front of the queue — the safe direction to be wrong.
            keep = sorted(self._last_served.items(), key=lambda kv: kv[1])[
                -(self._history // 2) :
            ]
            self._last_served = dict(keep)

    def _next_waiter(self) -> dict[str, Any] | None:
        if not self._waiters:
            return None
        return min(self._waiters, key=self._waiter_rank)

    def _waiter_rank(self, waiter: dict[str, Any]) -> tuple:
        rank = self._rank.get(waiter["priority"], self._default_rank)
        # A key never served sorts as 0.0 — a room's first turn jumps ahead of
        # rooms that have already had one. That is the fairness we want.
        return (rank, self._last_served.get(waiter["key"], 0.0), waiter["seq"])

    async def acquire(
        self, timeout: float, *, key: str = "", priority: str = "background"
    ) -> None:
        """Take a slot or raise asyncio.TimeoutError. Pair with release()."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + float(timeout)
        waiter = {
            "key": str(key or ""),
            "priority": str(priority),
            "seq": self._seq,
        }
        self._seq += 1
        async with self._cond:
            self._waiters.append(waiter)
            try:
                while True:
                    if (
                        self._active < self._capacity
                        and self._next_waiter() is waiter
                    ):
                        self._active += 1
                        self._mark_served(waiter["key"], loop.time())
                        return
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        raise asyncio.TimeoutError()
                    await asyncio.wait_for(self._cond.wait(), timeout=remaining)
            finally:
                # Removed by identity: `list.remove` would compare dicts by
                # value, and this must take out exactly our own entry.
                for index, queued in enumerate(self._waiters):
                    if queued is waiter:
                        del self._waiters[index]
                        break
                # Whether we won, timed out, or were cancelled, the queue just
                # changed shape — somebody behind us may now be next.
                self._cond.notify_all()

    async def release(self) -> None:
        async with self._cond:
            if self._active > 0:
                self._active -= 1
            self._cond.notify_all()

    async def notify(self) -> None:
        """Re-evaluate the queue after an external change (e.g. a resize)."""
        async with self._cond:
            self._cond.notify_all()

    def stats(self) -> dict[str, Any]:
        by_priority: dict[str, int] = {}
        for waiter in self._waiters:
            by_priority[waiter["priority"]] = by_priority.get(waiter["priority"], 0) + 1
        return {
            "capacity": self._capacity,
            "active": self._active,
            "waiting": len(self._waiters),
            "waiting_by_priority": by_priority,
            "known_keys": len(self._last_served),
        }


class KeyedLocks:
    """Per-key asyncio locks that do not accumulate one entry per key forever.

    ``dict[channel_id] -> Lock`` is the obvious implementation and it leaks:
    every channel the bot has ever seen keeps a lock object alive, and across
    a few hundred servers that is a slow, permanent climb. Locks here are
    reclaimed once they are unlocked and nobody is waiting on them.
    """

    def __init__(self, max_idle: int = 256) -> None:
        self.max_idle = max(16, int(max_idle))
        self._locks: dict[str, asyncio.Lock] = {}
        self._used: dict[str, float] = {}

    def get(self, key: str) -> asyncio.Lock:
        k = str(key)
        lock = self._locks.get(k)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[k] = lock
        self._used[k] = time.monotonic()
        if len(self._locks) > self.max_idle:
            self.prune()
        return lock

    def prune(self, *, keep: set[str] | None = None, all_idle: bool = False) -> int:
        """Drop idle, unlocked locks. Returns how many went.

        A held lock is never dropped — that would hand the next caller a fresh
        lock and let two turns into the same room at once. Neither is anything
        in ``keep``. By default this only trims back down toward half the cap,
        which is all the growth bound needs; ``all_idle=True`` is for the
        periodic cleanup pass, which drops every cold room it is allowed to.
        """
        protected = keep or set()
        # Oldest first, so trimming takes the coldest rooms.
        order = sorted(self._used.items(), key=lambda kv: kv[1])
        target = (
            len(self._locks)
            if all_idle
            else max(0, len(self._locks) - self.max_idle // 2)
        )
        removed = 0
        for key, _ts in order:
            if removed >= target:
                break
            if key in protected:
                continue
            lock = self._locks.get(key)
            if lock is None or lock.locked() or getattr(lock, "_waiters", None):
                continue
            self._locks.pop(key, None)
            self._used.pop(key, None)
            removed += 1
        return removed

    def __len__(self) -> int:
        return len(self._locks)

    def __contains__(self, key: object) -> bool:
        return str(key) in self._locks

    def items(self):
        return self._locks.items()

    def pop(self, key: str, default: Any = None) -> Any:
        self._used.pop(str(key), None)
        return self._locks.pop(str(key), default)


# Substrings that place a tool in a budget class. A tool can override this by
# declaring `concurrency_class` on its class; this map is only the fallback for
# the ones that don't. Anything unmatched lands in "default", which is wide.
_TOOL_CLASS_HINTS: tuple[tuple[str, str], ...] = (
    ("image", "media"),
    ("video", "media"),
    ("avatar", "media"),
    ("see_", "media"),
    ("tts", "tts"),
    ("speak", "tts"),
    ("voice", "tts"),
    ("shell", "shell"),
    ("terminal", "shell"),
    ("site", "site"),
    ("deploy", "site"),
    ("fetch", "web"),
    ("search", "web"),
    ("browse", "web"),
    ("url", "web"),
    ("youtube", "web"),
)


def classify_tool(name: str, tool: Any = None) -> str:
    """Which budget a tool draws from.

    Grouping matters more than the exact grouping: the point is that eight
    concurrent image generations cannot also consume every outbound HTTP slot
    the reply path needs.
    """
    declared = getattr(tool, "concurrency_class", None)
    if declared:
        return str(declared)
    lowered = str(name or "").lower()
    for hint, bucket in _TOOL_CLASS_HINTS:
        if hint in lowered:
            return bucket
    return "default"


class ToolConcurrency:
    """Separate budgets prevent heavy tools exhausting shared HTTP capacity.

    ``default`` is deliberately generous — most tools are a cheap API call and
    should never queue behind each other. The narrow budgets are for the ones
    that cost real CPU, disk, or an external worker.
    """

    def __init__(self, **limits: int) -> None:
        defaults = {
            "provider": 8,
            "media": 2,
            "tts": 2,
            "shell": 2,
            "site": 2,
            "web": 6,
            "agent": 2,
            "default": 16,
        }
        defaults.update(limits)
        self._limits = dict(defaults)
        self._semaphores = {name: asyncio.Semaphore(value) for name, value in defaults.items()}
        self._waiting: dict[str, int] = {}

    def gate(self, name: str) -> asyncio.Semaphore:
        return self._semaphores.setdefault(name, asyncio.Semaphore(1))

    @contextlib.asynccontextmanager
    async def slot(self, name: str):
        """Hold a budget slot for the duration of the block.

        Tracks how many callers are queued so a persistently contended budget
        shows up in the logs instead of just looking like a slow tool.
        """
        gate = self.gate(name)
        queued = gate.locked()
        if queued:
            self._waiting[name] = self._waiting.get(name, 0) + 1
            if self._waiting[name] >= max(2, self._limits.get(name, 2)):
                logger.info(
                    "tool budget %r saturated: %s waiting", name, self._waiting[name]
                )
        try:
            async with gate:
                yield
        finally:
            if queued:
                self._waiting[name] = max(0, self._waiting.get(name, 1) - 1)

    async def run(self, name: str, operation: Awaitable[Any], timeout: float) -> Any:
        gate = self.gate(name)
        acquired = False
        try:
            await gate.acquire()
            acquired = True
            return await asyncio.wait_for(operation, timeout=timeout)
        except asyncio.CancelledError:
            # The caller can cancel while queued on the semaphore, before
            # asyncio.wait_for has a chance to consume the coroutine it was
            # handed. Close bare coroutine objects in that case so they do
            # not emit "never awaited" warnings or retain captured state.
            if not acquired:
                close = getattr(operation, "close", None)
                if callable(close):
                    close()
            raise
        finally:
            if acquired:
                gate.release()

    def stats(self) -> dict[str, Any]:
        return {
            name: {
                "limit": self._limits.get(name),
                "free": sem._value,  # noqa: SLF001 - asyncio exposes no public read
                "waiting": self._waiting.get(name, 0),
            }
            for name, sem in self._semaphores.items()
        }


async def loop_watchdog(interval: float = 0.1, warning: float = 0.25) -> None:
    """Log event-loop stalls; cancellation cleanly stops the monitor."""
    expected = time.monotonic() + interval
    try:
        while True:
            await asyncio.sleep(interval)
            now = time.monotonic()
            lag = now - expected
            if lag > warning:
                logger.warning("event loop lag %.3fs exceeds %.3fs", lag, warning)
            expected = now + interval
    except asyncio.CancelledError:
        return
