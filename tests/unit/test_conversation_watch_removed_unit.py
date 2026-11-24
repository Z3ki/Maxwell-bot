"""Conversation watch is gone: leftover keys are ignored and undirected lines stay quiet."""

from __future__ import annotations

import asyncio
import importlib.util
from types import SimpleNamespace

from api.state import _sanitize_control
from bot import MaxwellBot
from bot_parts import __all__ as bot_parts_exports
from control_defaults import DEAD_CONTROL_KEYS, DEFAULT_CONTROL

WATCH_KEYS = (
    "conversation_watch_enabled",
    "conversation_watch_seconds",
    "conversation_watch_debounce_seconds",
    "conversation_watch_pressure",
)


def test_conversation_watch_keys_are_retired():
    for key in WATCH_KEYS:
        assert key not in DEFAULT_CONTROL
        assert key in DEAD_CONTROL_KEYS


def test_sanitize_control_strips_legacy_watch_keys():
    out = _sanitize_control(
        {
            "conversation_watch_enabled": True,
            "conversation_watch_seconds": 120,
            "conversation_watch_debounce_seconds": 0.5,
            "conversation_watch_pressure": 0.9,
        }
    )
    for key in WATCH_KEYS:
        assert key not in out


def test_watch_modules_and_mixin_are_gone():
    assert importlib.util.find_spec("watch_policy") is None
    assert importlib.util.find_spec("bot_parts.watch") is None
    assert "WatchMixin" not in bot_parts_exports
    assert not any(cls.__name__ == "WatchMixin" for cls in MaxwellBot.__mro__)
    assert not hasattr(MaxwellBot, "_arm_conversation_watch")
    assert not hasattr(MaxwellBot, "_conversation_watch_active")
    assert not hasattr(MaxwellBot, "_should_live_reply")


def _reply_bot(*, direct: bool = False, interrupt: bool = False, latest: bool = True):
    calls: list[tuple[object, str, bool]] = []
    outcomes: list[tuple[str, str]] = []
    bot = SimpleNamespace(
        _directly_addressed=lambda _message: direct,
        _is_same_user_interrupt=lambda _message: interrupt,
        _note_same_user_latest=lambda _message: latest,
    )

    async def dispatch(message, content, *, directed):
        calls.append((message, content, directed))

    async def record(message, status, reason):
        outcomes.append((status, reason))

    bot._dispatch_reply = dispatch
    bot._record_request_outcome = record
    bot._maybe_live_reply = MaxwellBot._maybe_live_reply.__get__(bot)
    return bot, calls, outcomes


def test_maybe_live_reply_dispatches_hard_pings_only():
    async def scenario():
        message = SimpleNamespace(id=1, content="hello")
        bot, calls, outcomes = _reply_bot(direct=True)
        await bot._maybe_live_reply(message, "hello")
        assert calls == [(message, "hello", True)]
        assert outcomes == []

        bot, calls, outcomes = _reply_bot(direct=False)
        await bot._maybe_live_reply(message, "ambient chatter")
        assert calls == []
        assert outcomes == []

    asyncio.run(scenario())


def test_maybe_live_reply_same_user_interrupt_still_dispatches():
    async def scenario():
        message = SimpleNamespace(id=2, content="wait no")
        bot, calls, outcomes = _reply_bot(direct=False, interrupt=True, latest=True)
        await bot._maybe_live_reply(message, "wait no")
        assert calls == [(message, "wait no", True)]
        assert outcomes == []

        bot, calls, outcomes = _reply_bot(direct=False, interrupt=True, latest=False)
        await bot._maybe_live_reply(message, "wait no")
        assert calls == []
        assert outcomes == [("superseded", "same_user_interrupt")]

    asyncio.run(scenario())
