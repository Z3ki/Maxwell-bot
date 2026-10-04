"""Small lifecycle contracts shared by the plugin host.

Loading remains synchronous for existing callers. Awaitable setup results are
completed explicitly by the async host before publishing tools or starting jobs.
"""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from .manifest import PluginManifest


@dataclass
class PendingSetup:
    plugin: str
    entry: Path
    manifest: PluginManifest
    awaitable: Awaitable[Any] | None = None


def invoke_hook(hook: Callable[..., Any], bot: Any, context: Any) -> Any:
    """Support legacy ``hook(bot)`` and context-aware hooks without retrying.

    A TypeError raised inside a hook is a plugin failure, not an indication
    that the host should execute it a second time with a different signature.
    """
    try:
        signature = inspect.signature(hook)
    except (TypeError, ValueError):
        return hook(bot)
    try:
        signature.bind(bot, context)
    except TypeError:
        try:
            signature.bind(bot, ctx=context)
        except TypeError:
            return hook(bot)
        return hook(bot, ctx=context)
    return hook(bot, context)


def discard_awaitable(value: Any) -> None:
    """Release an unstarted setup coroutine or cancel an already-scheduled one."""
    if isinstance(value, asyncio.Future):
        value.cancel()
    else:
        close = getattr(value, "close", None)
        if callable(close):
            close()


@dataclass
class ToolWrapper:
    """A removable link in a tool's execution chain.

    Wrappers receive an indirection so unloading a lower wrapper does not
    leave its executable captured in another plugin's closure.
    """

    tool: Any
    original: Callable[..., Any]
    replacement: Callable[..., Any] | None = None

    def delegate(self) -> Callable[..., Any]:
        if inspect.iscoroutinefunction(self.original):
            async def execute(*args: Any, **kwargs: Any) -> Any:
                return await self.original(*args, **kwargs)
        else:
            def execute(*args: Any, **kwargs: Any) -> Any:
                return self.original(*args, **kwargs)
        return execute
