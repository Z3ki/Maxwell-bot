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
from collections import defaultdict
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

    async def submit(self, guild_id: int, channel_id: int,
                     callback: Callable[[], Awaitable[Any]]) -> Any:
        key = (int(guild_id), int(channel_id))
        async with self._lock:
            queue = self._queues.setdefault(key, asyncio.Queue(self.max_pending))
            worker = self._workers.get(key)
            if worker is None or worker.done():
                worker = asyncio.create_task(self._run(key, queue), name=f"channel-worker-{key[0]}-{key[1]}")
                self._workers[key] = worker
        result: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        try:
            queue.put_nowait(_Work(callback, result))
        except asyncio.QueueFull:
            raise RuntimeError("channel queue is full; try again shortly") from None
        return await result

    async def _run(self, key: tuple[int, int], queue: asyncio.Queue[_Work | None]) -> None:
        while True:
            work = await queue.get()
            try:
                if work is None:
                    return
                try:
                    work.result.set_result(await work.callback())
                except asyncio.CancelledError:
                    if not work.result.done(): work.result.cancel()
                    raise
                except Exception as exc:
                    if not work.result.done(): work.result.set_exception(exc)
            finally:
                queue.task_done()

    async def close(self) -> None:
        workers = list(self._workers.values())
        for queue in self._queues.values():
            with contextlib.suppress(asyncio.QueueFull): queue.put_nowait(None)
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        self._workers.clear(); self._queues.clear()


class ToolConcurrency:
    """Separate budgets prevent heavy tools exhausting shared HTTP capacity."""
    def __init__(self, **limits: int) -> None:
        defaults = {"provider": 8, "media": 2, "tts": 2, "shell": 2, "site": 2}
        defaults.update(limits)
        self._semaphores = {name: asyncio.Semaphore(value) for name, value in defaults.items()}

    def gate(self, name: str) -> asyncio.Semaphore:
        return self._semaphores.setdefault(name, asyncio.Semaphore(1))

    async def run(self, name: str, operation: Awaitable[Any], timeout: float) -> Any:
        async with self.gate(name):
            return await asyncio.wait_for(operation, timeout=timeout)


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
