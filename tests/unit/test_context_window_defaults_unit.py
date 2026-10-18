"""Fresh installs must send a real conversation window, not a handful of lines.

Regression: with 20 messages / 24k history / 48k prompt defaults, the ~20k
char system prompt plus a quarter-budget tail reserve left history ~3k chars,
so each turn reached the model with about six history messages.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from api.state import _sanitize_control
from bot import MaxwellBot, ToolCircuitBreaker
from control_defaults import DEFAULT_CONTROL
from maxwell_core.prompts.conversation import transcript_tail_reserve
from provider_limits import turn_timeout_seconds

# Roughly the size of the real static system prompt with the bundled tools.
SYSTEM_PROMPT_CHARS = 21000


def test_context_defaults_match_the_production_deployment():
    assert DEFAULT_CONTROL["memory_history_messages"] >= 40
    assert DEFAULT_CONTROL["memory_context_budget"] >= 48000
    assert DEFAULT_CONTROL["prompt_context_budget"] >= 96000
    assert DEFAULT_CONTROL["tool_history_messages"] >= 8
    assert DEFAULT_CONTROL["max_tool_iterations"] >= 50
    assert DEFAULT_CONTROL["live_turn_timeout_seconds"] >= 900


def test_history_budget_is_half_the_prompt_budget():
    assert (
        DEFAULT_CONTROL["memory_context_budget"] * 2
        == DEFAULT_CONTROL["prompt_context_budget"]
    )


def test_live_turn_outlasts_a_full_default_tool_loop_budget():
    # A whole turn must leave room for more than a couple of tool rounds.
    assert DEFAULT_CONTROL["live_turn_timeout_seconds"] >= 5 * 60


def test_tail_reserve_scales_by_an_eighth_and_yields_to_large_tails():
    assert transcript_tail_reserve(96000, 1000, 100) == 12000
    assert transcript_tail_reserve(32000, 1000, 100) == 8000
    big = transcript_tail_reserve(96000, 30000, 100)
    assert big == 30000 + 4000 + 1500


def test_missing_control_keys_fall_back_to_the_shared_defaults():
    with_defaults = SimpleNamespace(_control=dict(DEFAULT_CONTROL), ai_provider=None)
    empty = SimpleNamespace(_control={}, ai_provider=None)
    assert MaxwellBot._prompt_budget_chars(empty) == MaxwellBot._prompt_budget_chars(
        with_defaults
    )
    garbage = SimpleNamespace(
        _control={"prompt_context_budget": "lots"}, ai_provider=None
    )
    assert MaxwellBot._prompt_budget_chars(garbage) == MaxwellBot._prompt_budget_chars(
        with_defaults
    )
    bot = SimpleNamespace(config=None, _control={})
    assert turn_timeout_seconds(bot) == DEFAULT_CONTROL["live_turn_timeout_seconds"]


def test_dashboard_sanitizer_keeps_default_budgets():
    out = _sanitize_control(dict(DEFAULT_CONTROL))
    for key in (
        "memory_history_messages",
        "memory_context_budget",
        "prompt_context_budget",
        "tool_history_messages",
        "max_tool_iterations",
        "live_turn_timeout_seconds",
    ):
        assert out[key] == DEFAULT_CONTROL[key], key
    junk = _sanitize_control({**DEFAULT_CONTROL, "prompt_context_budget": "x"})
    assert junk["prompt_context_budget"] == DEFAULT_CONTROL["prompt_context_budget"]


class _Memory:
    def __init__(self, rows):
        self.rows = rows

    async def get_channel_memory(self, channel_id, *, requester=None):
        return list(self.rows)


def _rows(count, chars):
    rows = []
    for i in range(count):
        mine = i % 2 == 1
        rows.append(
            {
                "message_id": str(1000 + i),
                "author": "Maxwell" if mine else "alice",
                "author_id": "1" if mine else "456",
                "author_is_bot": mine,
                "content": f"m{i:03d} " + "x" * chars,
                "timestamp": "2026-10-18T00:00:00+00:00",
            }
        )
    return rows


def _bot(rows, control=None):
    base = dict(DEFAULT_CONTROL)
    base.update(
        {
            "emoji_context_enabled": False,
            "music_context_enabled": False,
            "tools_enabled": False,
        }
    )
    base.update(control or {})
    bot = SimpleNamespace(
        _tool_breaker=ToolCircuitBreaker(failure_threshold=999, recovery_seconds=0),
        _control=base,
        _drugged_until={},
        _guild_emojis={},
        _recent_users={},
        _conversation_watch={},
        _tool_system_prompt=lambda *args, **kwargs: "s" * SYSTEM_PROMPT_CHARS,
        bot_name="Maxwell",
        memory=_Memory(rows),
        user=SimpleNamespace(display_name="Maxwell", id=1),
        ai_provider=None,
    )
    for name in (
        "_reply_parent",
        "_replying_to_own_message",
        "_render_reply_parent",
        "_author_is_self",
        "_iter_resolved_reply_chain",
        "_reply_parent_context_lines",
        "_directly_addressed",
        "_conversation_watch_active",
        "_is_short_live_turn",
    ):
        setattr(bot, name, getattr(MaxwellBot, name).__get__(bot))
    return bot


def _message():
    return SimpleNamespace(
        author=SimpleNamespace(bot=False, display_name="alice", id=456),
        channel=SimpleNamespace(id=123),
        guild=None,
        id=9999,
        mentions=[],
        reference=None,
    )


def _history(bot):
    messages = asyncio.run(
        MaxwellBot._build_messages(
            bot, _message(), "maxwell what did we decide earlier about the deploy?"
        )
    )
    blocks = [
        m["content"]
        for m in messages
        if isinstance(m.get("content"), str)
        and m["content"].startswith("<previous_conversation>")
    ]
    total = sum(MaxwellBot._message_content_chars(m) for m in messages)
    return (blocks[0] if blocks else ""), total


def test_default_history_window_carries_forty_short_messages():
    bot = _bot(_rows(120, 150))
    history, _ = _history(bot)
    kept = [f"m{i:03d}" for i in range(120) if f"m{i:03d}" in history]
    assert len(kept) >= DEFAULT_CONTROL["memory_history_messages"]


def test_default_history_is_not_starved_by_a_large_system_prompt():
    bot = _bot(_rows(120, 1200))
    history, total = _history(bot)
    # The old defaults left ~3k chars here; history must get most of its cap.
    assert len(history) >= 30000
    assert len(history) <= DEFAULT_CONTROL["memory_context_budget"]
    # And it must fit, or the final budget pass drops the whole transcript.
    assert total <= MaxwellBot._prompt_budget_chars(bot)


def test_history_budget_scales_with_the_prompt_budget():
    rows = _rows(200, 1200)
    wide = {"memory_context_budget": 96000, "memory_history_messages": 200}
    small, _ = _history(_bot(rows, {"prompt_context_budget": 64000, **wide}))
    large, total = _history(_bot(rows, {"prompt_context_budget": 160000, **wide}))
    assert len(large) > len(small) + 40000
    assert total <= 160000
