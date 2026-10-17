"""Process drain and Discord shard launch settings.

Read from the environment at call time so a missing variable stays the
historical default and tests can change the process environment.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any


def drain_timeout_seconds() -> float:
    """How long to wait for in-flight work after SIGINT or SIGTERM.

    Unset or unparsable values default to 30 seconds. ``0`` does not wait.
    Values are clamped to 0–600.
    """
    raw = os.getenv("DRAIN_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return 30.0
    try:
        value = float(raw)
    except ValueError:
        return 30.0
    if value < 0:
        return 0.0
    if value > 600:
        return 600.0
    return value


def shard_launch_kwargs() -> dict[str, Any]:
    """Keyword arguments for an auto-sharded Discord client.

    Both variables unset means automatic sharding (pass nothing). ``SHARD_IDS``
    without ``SHARD_COUNT`` uses ``max(id) + 1``. Negative ids are dropped.
    """
    raw_count = os.getenv("SHARD_COUNT", "").strip()
    raw_ids = os.getenv("SHARD_IDS", "").strip()
    ids: list[int] = []
    if raw_ids:
        for part in raw_ids.split(","):
            piece = part.strip()
            if not piece:
                continue
            try:
                value = int(piece)
            except ValueError:
                continue
            if value >= 0:
                ids.append(value)
    count: int | None = None
    if raw_count:
        try:
            parsed = int(raw_count)
        except ValueError:
            parsed = 0
        if parsed >= 1:
            count = parsed
    if ids and count is None:
        count = max(ids) + 1
    if count is None and not ids:
        return {}
    kwargs: dict[str, Any] = {}
    if count is not None:
        kwargs["shard_count"] = count
    if ids:
        kwargs["shard_ids"] = ids
    return kwargs


def _idle_event(bot: Any) -> asyncio.Event | None:
    current = getattr(bot, "_inflight_idle", None)
    if isinstance(current, asyncio.Event):
        return current
    try:
        current = asyncio.Event()
        current.set()
        bot._inflight_idle = current
    except Exception:
        return None
    return current


def _publish_idle(bot: Any) -> None:
    event = _idle_event(bot)
    if event is None:
        return
    if inflight_busy(bot):
        event.clear()
    else:
        event.set()


def begin_inflight(bot: Any, kind: str) -> None:
    """Count one finite unit of work. Perpetual loops must not stay counted."""
    if bot is None:
        return
    counts = getattr(bot, "_inflight_counts", None)
    if not isinstance(counts, dict):
        counts = {}
        try:
            bot._inflight_counts = counts
        except Exception:
            return
    key = str(kind or "work")
    counts[key] = int(counts.get(key, 0) or 0) + 1
    _publish_idle(bot)


def end_inflight(bot: Any, kind: str) -> None:
    counts = getattr(bot, "_inflight_counts", None)
    if not isinstance(counts, dict):
        return
    key = str(kind or "work")
    counts[key] = max(0, int(counts.get(key, 0) or 0) - 1)
    _publish_idle(bot)


def _task_running(task: Any) -> bool:
    return isinstance(task, asyncio.Task) and not task.done()


def _inflight_tasks(bot: Any) -> list[asyncio.Task]:
    active = getattr(bot, "_active_requests", None) or {}
    found = [task for task in active.values() if _task_running(task)]
    queue = getattr(bot, "_reply_queue", None)
    running = [
        getattr(state, "running", None)
        for state in (getattr(queue, "_channels", None) or {}).values()
    ]
    found.extend(task for task in running if _task_running(task))
    manager = getattr(bot, "plugin_manager", None)
    groups = getattr(manager, "_maxwell_managed_tasks", None) or {}
    if isinstance(groups, dict):
        found.extend(
            task
            for group in groups.values()
            for task in (group or ())
            if _task_running(task)
        )
    jobs = getattr(getattr(bot, "_task_jobs", None), "jobs", {}) or {}
    found.extend(job.task for job in jobs.values() if _task_running(job.task))
    return found


def inflight_busy(bot: Any) -> bool:
    """True while a turn, tool call, or counted background callback is running."""
    if bot is None:
        return False
    jobs = getattr(getattr(bot, "_task_jobs", None), "jobs", {}) or {}
    if any(_task_running(job.task) for job in jobs.values()):
        return True
    active = getattr(bot, "_active_requests", None) or {}
    for task in active.values():
        if isinstance(task, asyncio.Task) and not task.done():
            return True
    queue = getattr(bot, "_reply_queue", None)
    any_active = getattr(queue, "any_active", None)
    if callable(any_active):
        try:
            if any_active():
                return True
        except Exception:
            pass
    counts = getattr(bot, "_inflight_counts", None) or {}
    if isinstance(counts, dict) and any(int(value or 0) > 0 for value in counts.values()):
        return True
    manager = getattr(bot, "plugin_manager", None)
    groups = getattr(manager, "_maxwell_managed_tasks", None) or {}
    if isinstance(groups, dict):
        for group in groups.values():
            for task in group or ():
                if isinstance(task, asyncio.Task) and not task.done():
                    return True
    return False


async def drain_inflight(bot: Any, timeout: float) -> None:
    """Wait until in-flight work finishes or ``timeout`` seconds elapse."""
    if timeout <= 0 or bot is None:
        return
    loop = asyncio.get_running_loop()
    deadline = loop.time() + float(timeout)
    while inflight_busy(bot):
        remaining = deadline - loop.time()
        if remaining <= 0:
            return
        tasks = _inflight_tasks(bot)
        event = _idle_event(bot)
        extras: list[asyncio.Task] = []
        if event is not None and not event.is_set():
            extras.append(asyncio.create_task(event.wait(), name="drain-idle"))
        if not tasks and not extras:
            return
        try:
            await asyncio.wait([*tasks, *extras], timeout=remaining)
        finally:
            for task in extras:
                if not task.done():
                    task.cancel()
