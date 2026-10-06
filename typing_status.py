"""Channel-scoped Discord typing indicator with deterministic shutdown.

Discord renders "X is typing..." until the user's typing state expires
(about 10s after the most recent typing event) or until that user posts a
message. A refresh loop that outlives the reply therefore leaves Maxwell
visibly "typing" for seconds after he has already answered — and one
typing event that lands after the message re-lights the indicator for
another full expiry window.

This module owns one ref-counted typing loop per channel and the two
transitions that matter:

* ``typing_suspend()`` — drain typing BEFORE any visible output is
  posted, so no typing event can trail the message (the message itself
  then clears whatever indicator is still on screen).
* ``typing_resume()`` — the next generation step is working again, so
  typing shows only while there is still work behind it.

The registry lives on the owning bot object (``owner._typing_statuses``)
so real bots, duck-typed test doubles, and adapter objects each keep
their own isolated state.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any


class TypingHandle:
    """One-shot lease on a channel's typing indicator.

    ``release()`` is idempotent so overlapping turn cleanups (an inner
    ``finally`` and the outer dispatcher) can both call it safely.
    """

    def __init__(self, status: "ChannelTyping") -> None:
        self._status: "ChannelTyping | None" = status

    async def release(self) -> None:
        status, self._status = self._status, None
        if status is not None:
            await status.release()


class ChannelTyping:
    """Ref-counted typing loop for one channel.

    ``_holders`` counts the turns/tools that want typing shown.
    ``_suspended`` is set whenever the bot posts visible output and stays
    sticky until the next generation step resumes, so the indicator never
    comes back behind the user's back after Maxwell has spoken.
    """

    def __init__(self, channel: Any) -> None:
        self._channel = channel
        self._holders = 0
        self._suspended = False
        self._cm: Any = None

    @property
    def holders(self) -> int:
        return self._holders

    @property
    def active(self) -> bool:
        return self._cm is not None

    async def acquire(self) -> TypingHandle:
        self._holders += 1
        await self._sync()
        return TypingHandle(self)

    async def release(self) -> None:
        self._holders = max(0, self._holders - 1)
        if self._holders == 0:
            self._suspended = False
        await self._sync()

    async def suspend(self) -> None:
        self._suspended = True
        await self._sync()

    async def resume(self) -> None:
        if self._holders <= 0:
            return
        self._suspended = False
        await self._sync()

    async def _sync(self) -> None:
        if self._holders > 0 and not self._suspended:
            if self._cm is None:
                typing = getattr(self._channel, "typing", None)
                if not callable(typing):
                    return
                try:
                    cm = typing()
                    await cm.__aenter__()
                except Exception:
                    return
                self._cm = cm
            return
        await self._close()

    async def _close(self) -> None:
        """Stop the typing loop and drain it so no event can land late."""
        cm, self._cm = self._cm, None
        if cm is None:
            return
        task = getattr(cm, "task", None)
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await cm.__aexit__(None, None, None)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


def _registry(owner: Any) -> dict[str, ChannelTyping] | None:
    store = getattr(owner, "_typing_statuses", None)
    if store is None:
        try:
            store = {}
            owner._typing_statuses = store
        except Exception:
            return None
    return store


def _key(channel: Any) -> str:
    channel_id = str(getattr(channel, "id", "") or "")
    return channel_id or f"obj:{id(channel)}"


def status_for(owner: Any, channel: Any, *, create: bool = False) -> ChannelTyping | None:
    if channel is None:
        return None
    store = _registry(owner)
    if store is None:
        return None
    key = _key(channel)
    status = store.get(key)
    if status is None and create and callable(getattr(channel, "typing", None)):
        status = ChannelTyping(channel)
        store[key] = status
    return status


async def typing_acquire(owner: Any, channel: Any) -> TypingHandle | None:
    status = status_for(owner, channel, create=True)
    if status is None:
        return None
    return await status.acquire()


async def typing_release(handle: Any) -> None:
    release = getattr(handle, "release", None)
    if callable(release):
        await release()


async def typing_suspend(owner: Any, channel: Any) -> None:
    status = status_for(owner, channel)
    if status is not None:
        await status.suspend()


async def typing_resume(owner: Any, channel: Any) -> None:
    status = status_for(owner, channel)
    if status is not None:
        await status.resume()


__all__ = [
    "ChannelTyping",
    "TypingHandle",
    "status_for",
    "typing_acquire",
    "typing_release",
    "typing_resume",
    "typing_suspend",
]
