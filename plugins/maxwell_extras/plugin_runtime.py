"""Runtime hardening and observability for Maxwell plugins.

The original manager already owns plugin tools, events and recurring jobs. This
layer adds bounded callback execution, per-plugin runtime health counters, and
tracked ad-hoc background tasks without changing the on-disk plugin format.
It is installed on the live manager by maxwell_extras and is intentionally
backwards compatible with existing plugins.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from types import MethodType
from typing import Any

from plugin_manager import PluginContext

_EVENT_TIMEOUT = max(1.0, float(os.getenv("MAXWELL_PLUGIN_EVENT_TIMEOUT", "15") or 15))
_JOB_TIMEOUT = max(5.0, float(os.getenv("MAXWELL_PLUGIN_JOB_TIMEOUT", "120") or 120))
_MAX_TRACKED_TASKS_PER_PLUGIN = 32


def _stats(manager: Any, plugin: str) -> dict[str, Any]:
    table = getattr(manager, "_maxwell_runtime_stats", None)
    if not isinstance(table, dict):
        table = {}
        manager._maxwell_runtime_stats = table
    return table.setdefault(
        str(plugin),
        {
            "event_calls": 0,
            "event_errors": 0,
            "event_timeouts": 0,
            "job_calls": 0,
            "job_errors": 0,
            "job_timeouts": 0,
            "background_started": 0,
            "background_errors": 0,
            "last_error": "",
            "last_event": "",
            "last_ok_at": 0.0,
            "last_error_at": 0.0,
        },
    )


def _error(row: dict[str, Any], exc: BaseException) -> None:
    row["last_error"] = f"{type(exc).__name__}: {exc}"[:1000]
    row["last_error_at"] = time.time()


def _cancel_managed(manager: Any) -> list[asyncio.Task]:
    groups = getattr(manager, "_maxwell_managed_tasks", None)
    if not isinstance(groups, dict):
        return []
    tasks = [task for values in groups.values() for task in values if isinstance(task, asyncio.Task)]
    groups.clear()
    for task in tasks:
        if not task.done():
            task.cancel()
    return tasks


def _spawn_plugin_task(manager: Any, plugin: str, awaitable: Any, *, name: str | None = None) -> asyncio.Task:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError as exc:
        close = getattr(awaitable, "close", None)
        if callable(close):
            close()
        raise RuntimeError("ctx.spawn() needs a running event loop; call it from an event/job callback") from exc

    groups = getattr(manager, "_maxwell_managed_tasks", None)
    if not isinstance(groups, dict):
        groups = {}
        manager._maxwell_managed_tasks = groups
    group: set[asyncio.Task] = groups.setdefault(str(plugin), set())
    group = {task for task in group if not task.done()}
    groups[str(plugin)] = group
    if len(group) >= _MAX_TRACKED_TASKS_PER_PLUGIN:
        close = getattr(awaitable, "close", None)
        if callable(close):
            close()
        raise RuntimeError(
            f"plugin {plugin!r} already has {_MAX_TRACKED_TASKS_PER_PLUGIN} managed background tasks"
        )

    task = loop.create_task(awaitable, name=name or f"plugin-bg-{plugin}")
    group.add(task)
    row = _stats(manager, str(plugin))
    row["background_started"] += 1

    def done(t: asyncio.Task) -> None:
        group.discard(t)
        if t.cancelled():
            return
        try:
            exc = t.exception()
        except (asyncio.CancelledError, Exception) as error:
            exc = error
        if exc is not None:
            row["background_errors"] += 1
            _error(row, exc)
            logger = getattr(PluginContext(manager, str(plugin)), "log", None)
            if logger is not None:
                logger.error("managed background task failed: %s", exc)

    task.add_done_callback(done)
    return task


def _ctx_spawn(self: PluginContext, awaitable: Any, *, name: str | None = None) -> asyncio.Task:
    """Start a background coroutine that the plugin manager owns and cancels on reload."""

    if not hasattr(awaitable, "__await__"):
        raise TypeError("ctx.spawn() expects a coroutine/awaitable")
    return _spawn_plugin_task(self._manager, self.name, awaitable, name=name)


def _ctx_after(
    self: PluginContext,
    seconds: float,
    callback: Any,
    *,
    name: str | None = None,
) -> asyncio.Task:
    """Run one async callback later as a managed task."""

    if not callable(callback):
        raise TypeError("callback must be callable")
    delay = float(seconds)
    if delay < 0 or delay > 31_536_000:
        raise ValueError("seconds must be between 0 and 31536000")

    async def delayed() -> None:
        await asyncio.sleep(delay)
        result = callback()
        if hasattr(result, "__await__"):
            await result
        elif result is not None:
            raise TypeError("ctx.after callback must return an awaitable or None")

    return _spawn_plugin_task(self._manager, self.name, delayed(), name=name or f"plugin-after-{self.name}")


def install_plugin_runtime_guards(bot: Any) -> None:
    manager = getattr(bot, "plugin_manager", None)
    if manager is None or getattr(manager, "_maxwell_runtime_guards_installed", False):
        return

    manager._maxwell_managed_tasks = {}
    manager._maxwell_runtime_stats = getattr(manager, "_maxwell_runtime_stats", {}) or {}

    # Add API methods to PluginContext. Existing context instances see class
    # methods immediately, so this also upgrades plugins loaded before us.
    if not hasattr(PluginContext, "spawn"):
        PluginContext.spawn = _ctx_spawn  # type: ignore[attr-defined]
    if not hasattr(PluginContext, "after"):
        PluginContext.after = _ctx_after  # type: ignore[attr-defined]

    async def dispatch_event(self: Any, event: str, *args: Any, **kwargs: Any) -> int:
        entries = list((getattr(self, "_listeners", {}) or {}).get(event) or [])
        if not entries:
            return 0
        actor = self._event_actor_id(args)
        filtered = [
            (plugin, callback)
            for plugin, callback in entries
            if self.is_plugin_enabled_for_user(plugin, actor)
        ]
        if not filtered:
            return 0

        async def run(plugin: str, callback: Any) -> None:
            row = _stats(self, plugin)
            row["event_calls"] += 1
            row["last_event"] = str(event)
            try:
                await asyncio.wait_for(callback(*args, **kwargs), timeout=_EVENT_TIMEOUT)
                row["last_ok_at"] = time.time()
            except asyncio.TimeoutError as exc:
                row["event_timeouts"] += 1
                row["event_errors"] += 1
                _error(row, exc)
                log = getattr((self.loaded_plugins.get(plugin) or {}).get("context"), "log", None)
                if log is not None:
                    log.warning("event %s timed out after %.1fs", event, _EVENT_TIMEOUT)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                row["event_errors"] += 1
                _error(row, exc)
                log = getattr((self.loaded_plugins.get(plugin) or {}).get("context"), "log", None)
                if log is not None:
                    log.exception("event %s failed", event)

        await asyncio.gather(*(run(plugin, cb) for plugin, cb in filtered), return_exceptions=True)
        return len(filtered)

    async def run_job(self: Any, plugin_name: str, spec: dict) -> None:
        interval = float(spec["interval"])
        callback = spec["callback"]
        if not spec.get("run_immediately"):
            await asyncio.sleep(interval)
        while True:
            started = time.monotonic()
            row = _stats(self, plugin_name)
            try:
                if self.is_plugin_enabled_for_user(plugin_name, None):
                    row["job_calls"] += 1
                    await asyncio.wait_for(callback(), timeout=_JOB_TIMEOUT)
                    row["last_ok_at"] = time.time()
            except asyncio.TimeoutError as exc:
                row["job_timeouts"] += 1
                row["job_errors"] += 1
                _error(row, exc)
                log = getattr((self.loaded_plugins.get(plugin_name) or {}).get("context"), "log", None)
                if log is not None:
                    log.warning("scheduled job timed out after %.1fs", _JOB_TIMEOUT)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                row["job_errors"] += 1
                _error(row, exc)
                log = getattr((self.loaded_plugins.get(plugin_name) or {}).get("context"), "log", None)
                if log is not None:
                    log.exception("scheduled job failed")
            elapsed = time.monotonic() - started
            await asyncio.sleep(max(1.0, interval - elapsed))

    original_reload = manager.reload_plugins
    original_teardown = manager.teardown

    def reload_plugins(self: Any) -> str:
        _cancel_managed(self)
        return original_reload()

    async def teardown(self: Any) -> None:
        tasks = _cancel_managed(self)
        for task in tasks:
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await task
        await original_teardown()

    def runtime_health(self: Any, plugin_name: str | None = None) -> dict[str, Any]:
        table = getattr(self, "_maxwell_runtime_stats", {}) or {}
        if plugin_name is not None:
            return dict(table.get(str(plugin_name)) or {})
        return {str(name): dict(row) for name, row in table.items() if isinstance(row, dict)}

    manager.dispatch_event = MethodType(dispatch_event, manager)
    manager._run_job = MethodType(run_job, manager)
    manager.reload_plugins = MethodType(reload_plugins, manager)
    manager.teardown = MethodType(teardown, manager)
    manager.runtime_health = MethodType(runtime_health, manager)
    manager._maxwell_runtime_guards_installed = True


__all__ = ["install_plugin_runtime_guards"]
