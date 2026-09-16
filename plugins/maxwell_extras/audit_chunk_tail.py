"""Keep the tool-disclosure button on the final chunk of a Discord reply.

``audit_ui`` sees the reply-bearing first chunk immediately, while later chunks
are ordinary channel sends without ``reply_to``. Attaching the disclosure at
that first send therefore leaves the button in the middle of a split response.

This layer makes audit attachment task-aware. The first candidate establishes
which inbound turn owns the trace; every later send in that same response task
replaces the candidate. Only when the task is finished do we finalize the audit
store and edit the last sent message with the disclosure view.
"""

from __future__ import annotations

import asyncio
import logging
from types import MethodType
from typing import Any

from . import audit_ui

logger = logging.getLogger(__name__)

_PENDING: dict[asyncio.Task[Any], tuple[Any, Any, Any]] = {}
_REGISTERED: set[asyncio.Task[Any]] = set()
_ORIGINAL_ATTACH: Any = None


def _consume_attach_result(task: asyncio.Task[Any]) -> None:
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception("Failed attaching final-chunk tool disclosure")


def _flush_when_done(task: asyncio.Task[Any]) -> None:
    _REGISTERED.discard(task)
    payload = _PENDING.pop(task, None)
    if payload is None:
        return
    try:
        loop = task.get_loop()
    except Exception:
        return
    if loop.is_closed() or _ORIGINAL_ATTACH is None:
        return
    try:
        attach_task = loop.create_task(_ORIGINAL_ATTACH(*payload))
    except RuntimeError:
        return
    attach_task.add_done_callback(_consume_attach_result)


def _remember_candidate(bot: Any, sent: Any, inbound: Any | None = None) -> None:
    if sent is None:
        return
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    if task is None:
        return

    previous = _PENDING.get(task)
    if inbound is None:
        if previous is None or previous[0] is not bot:
            return
        inbound = previous[2]
    if inbound is None:
        return

    _PENDING[task] = (bot, sent, inbound)
    if task not in _REGISTERED:
        _REGISTERED.add(task)
        task.add_done_callback(_flush_when_done)


async def _task_aware_attach(bot: Any, sent: Any, inbound: Any) -> None:
    """Queue on opted-in bots; preserve audit_ui's old behavior elsewhere."""

    if not getattr(bot, "_maxwell_audit_chunk_tail_enabled", False):
        if _ORIGINAL_ATTACH is not None:
            await _ORIGINAL_ATTACH(bot, sent, inbound)
        return

    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    if task is None:
        if _ORIGINAL_ATTACH is not None:
            await _ORIGINAL_ATTACH(bot, sent, inbound)
        return
    _remember_candidate(bot, sent, inbound)


def _patch_audit_attach() -> None:
    global _ORIGINAL_ATTACH

    current = getattr(audit_ui, "_attach_trace", None)
    if not callable(current):
        return
    if getattr(current, "_maxwell_final_chunk_dispatch", False):
        original = getattr(current, "_maxwell_original_attach", None)
        if callable(original):
            _ORIGINAL_ATTACH = original
        return

    _ORIGINAL_ATTACH = current
    _task_aware_attach._maxwell_final_chunk_dispatch = True  # type: ignore[attr-defined]
    _task_aware_attach._maxwell_original_attach = current  # type: ignore[attr-defined]
    audit_ui._attach_trace = _task_aware_attach


def install_audit_chunk_tail(bot: Any) -> bool:
    """Install final-chunk disclosure placement for one Maxwell bot instance."""

    _patch_audit_attach()
    bot._maxwell_audit_chunk_tail_enabled = True

    if getattr(bot, "_maxwell_audit_chunk_tail_send_wrapped", False):
        return True

    original_send = getattr(bot, "_send_with_slowmode", None)
    if not callable(original_send):
        return False

    async def send_wrapper(
        self_obj: Any,
        channel: Any,
        content: str | None = None,
        *,
        reply_to: Any = None,
        file: Any = None,
        **kwargs: Any,
    ) -> Any:
        sent = await original_send(
            channel,
            content,
            reply_to=reply_to,
            file=file,
            **kwargs,
        )
        # audit_ui queues the first reply-bearing message. Later split chunks
        # have reply_to=None, so this wrapper advances the candidate to each
        # newest message without prematurely attaching the button.
        _remember_candidate(self_obj, sent, reply_to)
        return sent

    bot._send_with_slowmode = MethodType(send_wrapper, bot)
    bot._maxwell_audit_chunk_tail_send_wrapped = True
    return True


__all__ = ["install_audit_chunk_tail"]
