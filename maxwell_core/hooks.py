"""Ordered hook bus. Plugins register callbacks; the host emits events.

Hooks cannot skip security gates. The taint check, Discord permission
check, and admin check run in the host before ``before_tool`` handlers
see the call. A handler that mutates arguments cannot set a forged
``_confirmed`` flag — the host strips ``_`` keys after hooks return.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import math
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable

logger = logging.getLogger("maxwell.hooks")

HookCallback = Callable[..., Any | Awaitable[Any]]

# Stable hook names. Plugins should use these strings; typos are rejected.
HOOK_NAMES: frozenset[str] = frozenset(
    {
        "before_message",
        "after_message",
        "before_prompt",
        "after_prompt",
        "before_model",
        "after_model",
        "before_tool",
        "after_tool",
        "before_response",
        "after_response",
        "interaction",
        "format_output",
        "progress",
        "memory_event",
        "task_lifecycle",
        "plugin_lifecycle",
        "personality",
        "turn_sent",
        "channel_send",
    }
)

# Default timeout so a hung plugin cannot stall the turn forever.
DEFAULT_HOOK_TIMEOUT = 8.0


@dataclass
class HookResult:
    """Value returned by a hook callback."""

    stop: bool = False
    value: Any = None
    skip: bool = False


@dataclass
class HookRegistration:
    plugin: str
    name: str
    callback: HookCallback
    priority: int = 100
    timeout: float = DEFAULT_HOOK_TIMEOUT


@dataclass
class HookPayload:
    """Mutable bag passed to hook callbacks."""

    data: dict[str, Any] = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self.data[key] = value


class HookBus:
    """Per-process hook registry. Registrations are dropped on plugin teardown."""

    def __init__(self) -> None:
        self._hooks: dict[str, list[HookRegistration]] = {name: [] for name in HOOK_NAMES}

    def register(
        self,
        plugin: str,
        name: str,
        callback: HookCallback,
        *,
        priority: int = 100,
        timeout: float | None = None,
    ) -> None:
        if name not in HOOK_NAMES:
            raise ValueError(
                f"Unknown hook {name!r}. Known: {', '.join(sorted(HOOK_NAMES))}"
            )
        if not callable(callback):
            raise TypeError("hook callback must be callable")
        timeout_value = float(timeout if timeout is not None else DEFAULT_HOOK_TIMEOUT)
        if not math.isfinite(timeout_value) or timeout_value <= 0:
            raise ValueError("hook timeout must be a finite positive number")
        entry = HookRegistration(
            plugin=str(plugin),
            name=name,
            callback=callback,
            priority=int(priority),
            timeout=timeout_value,
        )
        bucket = self._hooks.setdefault(name, [])
        bucket.append(entry)
        bucket.sort(key=lambda item: (item.priority, item.plugin))

    def unregister_plugin(self, plugin: str) -> int:
        removed = 0
        name = str(plugin)
        for hook_name, entries in self._hooks.items():
            kept = [item for item in entries if item.plugin != name]
            removed += len(entries) - len(kept)
            self._hooks[hook_name] = kept
        return removed

    def handlers(self, name: str, *, plugins: Iterable[str] | None = None) -> list[HookRegistration]:
        allowed = None if plugins is None else set(plugins)
        return [
            item
            for item in self._hooks.get(name, [])
            if allowed is None or item.plugin in allowed
        ]

    def plugin_hooks(self, plugin: str) -> list[str]:
        name = str(plugin)
        return sorted(
            hook_name
            for hook_name, entries in self._hooks.items()
            if any(item.plugin == name for item in entries)
        )

    def _is_registered(self, entry: HookRegistration) -> bool:
        # An earlier handler can unload another plugin while an emit is
        # awaiting it. Do not invoke stale entries from the initial snapshot.
        return any(item is entry for item in self._hooks.get(entry.name, ()))

    @staticmethod
    def _apply_result(bag: HookPayload, entry: HookRegistration, result: Any) -> bool:
        """Apply one callback's result consistently for async and sync emits."""
        if isinstance(result, HookResult):
            if isinstance(result.value, dict):
                bag.data.update(result.value)
            stopped = result.stop or result.skip
        elif isinstance(result, dict):
            bag.data.update(result)
            stopped = result.get("stop") or result.get("skip")
        else:
            stopped = False
        if stopped:
            bag["stop"] = True
            bag["skipped_by"] = entry.plugin
        return bool(stopped)

    async def emit(
        self,
        name: str,
        payload: HookPayload | dict[str, Any] | None = None,
        *,
        plugins: Iterable[str] | None = None,
    ) -> HookPayload:
        """Run handlers in priority order. First ``stop`` wins.

        Failures are isolated per plugin. ``CancelledError`` propagates.
        """
        bag = payload if isinstance(payload, HookPayload) else HookPayload(dict(payload or {}))
        for entry in self.handlers(name, plugins=plugins):
            if not self._is_registered(entry):
                continue
            try:
                result = entry.callback(bag)
                if inspect.isawaitable(result):
                    result = await asyncio.wait_for(result, timeout=entry.timeout)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Plugin %r hook %s failed", entry.plugin, name)
                continue
            if self._apply_result(bag, entry, result):
                return bag
        return bag

    def emit_sync(
        self,
        name: str,
        payload: HookPayload | dict[str, Any] | None = None,
        *,
        plugins: Iterable[str] | None = None,
    ) -> HookPayload:
        """Synchronous emit for prompt/personality transforms.

        Async callbacks are skipped (logged) rather than blocking the loop.
        """
        bag = payload if isinstance(payload, HookPayload) else HookPayload(dict(payload or {}))
        for entry in self.handlers(name, plugins=plugins):
            if not self._is_registered(entry):
                continue
            if inspect.iscoroutinefunction(entry.callback):
                logger.warning(
                    "Plugin %r registered async callback on sync hook %s; skipped",
                    entry.plugin,
                    name,
                )
                continue
            try:
                result = entry.callback(bag)
            except Exception:
                logger.exception("Plugin %r hook %s failed", entry.plugin, name)
                continue
            if inspect.isawaitable(result):
                with contextlib.suppress(Exception):
                    if isinstance(result, asyncio.Future):
                        result.cancel()
                    else:
                        getattr(result, "close", lambda: None)()
                logger.warning(
                    "Plugin %r hook %s returned awaitable on sync emit; skipped",
                    entry.plugin,
                    name,
                )
                continue
            if self._apply_result(bag, entry, result):
                return bag
        return bag
