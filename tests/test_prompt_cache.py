"""Prompt-cache and prompt-size guarantees.

Providers with automatic prefix caching (DeepSeek, Qwen/Moonshot via Ollama
cloud, xAI, OpenAI-compatible gateways) only reuse a BYTE-IDENTICAL prefix, so
two things have to hold on every turn: the static system block and the replayed
transcript must not change, and the volatile per-turn block must sit behind
them. These tests pin both, plus the size bounds on the tool-loop tail.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from bot import MaxwellBot, ToolCircuitBreaker, _format_context_timestamp
from tool_schemas import trim_tool_tail


class FakeMemory:
    def __init__(self, messages=None):
        self.messages = list(messages or [])

    async def get_channel_memory(self, channel_id):
        return list(self.messages)

    def get_server_prompt(self, server_id):
        return None


def _bot(memory):
    bot = SimpleNamespace(
        _tool_breaker=ToolCircuitBreaker(failure_threshold=999, recovery_seconds=0),
        _control={
            "base_personality": "test",
            "cross_context_enabled": False,
            "emoji_context_enabled": False,
            "long_term_memory_enabled": False,
            "memory_context_budget": 30000,
            "memory_history_messages": 20,
            "music_context_enabled": False,
            "tools_enabled": False,
        },
        _drugged_until={},
        _guild_emojis={},
        _recent_users={},
        _conversation_watch={},
        _tool_system_prompt=lambda *args, **kwargs: "",
        bot_name="Maxwell",
        memory=memory,
        user=SimpleNamespace(display_name="Maxwell", id=1),
    )
    bot._reply_parent = MaxwellBot._reply_parent.__get__(bot)
    bot._replying_to_own_message = MaxwellBot._replying_to_own_message.__get__(bot)
    bot._render_reply_parent = MaxwellBot._render_reply_parent.__get__(bot)
    bot._author_is_self = MaxwellBot._author_is_self.__get__(bot)
    bot._iter_resolved_reply_chain = MaxwellBot._iter_resolved_reply_chain.__get__(bot)
    bot._reply_parent_context_lines = MaxwellBot._reply_parent_context_lines.__get__(
        bot
    )
    bot._directly_addressed = MaxwellBot._directly_addressed.__get__(bot)
    bot._conversation_watch_active = MaxwellBot._conversation_watch_active.__get__(bot)
    bot._is_short_live_turn = MaxwellBot._is_short_live_turn.__get__(bot)
    return bot


def _message():
    return SimpleNamespace(
        author=SimpleNamespace(bot=False, display_name="alice", id=456),
        channel=SimpleNamespace(id=123),
        guild=None,
        id=789,
        mentions=[],
        reference=None,
    )


def test_history_timestamps_are_stable_across_calls():
    """A replayed transcript line must render the same bytes an hour later."""
    stamp = "2026-08-20T12:00:00+00:00"
    early = _format_context_timestamp(
        stamp, now=datetime(2026, 8, 20, 12, 5, tzinfo=timezone.utc), relative=False
    )
    later = _format_context_timestamp(
        stamp, now=datetime(2026, 8, 20, 18, 5, tzinfo=timezone.utc), relative=False
    )
    assert early == later
    assert "ago" not in early
    # The relative form is still available for one-shot (uncached) rendering.
    assert "ago" in _format_context_timestamp(
        stamp, now=datetime(2026, 8, 20, 18, 5, tzinfo=timezone.utc)
    )


def test_static_prefix_and_transcript_are_identical_across_turns():
    old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    memory = FakeMemory(
        [
            {"author": "alice", "author_id": "456", "content": "hi", "timestamp": old},
            {"author": "Maxwell", "content": "hey", "timestamp": old},
        ]
    )
    bot = _bot(memory)

    async def run():
        return (
            await MaxwellBot._build_messages(bot, _message(), "latest"),
            await MaxwellBot._build_messages(bot, _message(), "latest"),
        )

    first, second = asyncio.run(run())
    # Static system block and the whole transcript are byte-identical, so the
    # provider can serve them from its prefix cache.
    assert first[0] == second[0]
    transcript = [m for m in first if "<previous_conversation>" in str(m["content"])]
    assert transcript and transcript == [
        m for m in second if "<previous_conversation>" in str(m["content"])
    ]


def test_volatile_block_sits_after_the_transcript():
    old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    memory = FakeMemory(
        [{"author": "alice", "author_id": "456", "content": "hi", "timestamp": old}]
    )

    async def run():
        return await MaxwellBot._build_messages(_bot(memory), _message(), "latest")

    messages = asyncio.run(run())
    # The per-turn user/time line is what changes every call; it must not be in
    # the leading system message (that would poison every token after it).
    assert "Memory scope:" not in messages[0]["content"]
    volatile = next(
        i for i, m in enumerate(messages) if "Memory scope:" in str(m["content"])
    )
    transcript = next(
        i
        for i, m in enumerate(messages)
        if "<previous_conversation>" in str(m["content"])
    )
    assert volatile > transcript
    assert messages[-1]["role"] == "user"


def test_trim_conversation_tail_keeps_tool_calls_paired():
    def round_msgs(i, size=1):
        return [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"call_{i}",
                        "function": {"name": "shell", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": f"call_{i}", "content": "x" * size},
        ]

    tail = [m for i in range(30) for m in round_msgs(i)]
    trimmed = trim_tool_tail(tail)
    assert len(trimmed) <= 24
    # Every tool message still has its assistant message ahead of it.
    open_ids = set()
    for msg in trimmed:
        if msg["role"] == "assistant":
            open_ids.update(c["id"] for c in msg["tool_calls"])
        else:
            assert msg["tool_call_id"] in open_ids
    assert trimmed[-1]["tool_call_id"] == "call_29"


def test_trim_conversation_tail_enforces_char_budget():
    tail = []
    for i in range(10):
        tail += [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"call_{i}",
                        "function": {"name": "shell", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": f"call_{i}", "content": "x" * 32_000},
        ]

    trimmed = trim_tool_tail(tail)
    used = sum(MaxwellBot._message_content_chars(m) for m in trimmed)
    assert used <= 96_000
    # The newest round always survives, even on its own.
    assert trimmed[-1]["tool_call_id"] == "call_9"


def test_message_content_chars_counts_tool_call_arguments():
    msg = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"function": {"name": "create_site", "arguments": "y" * 5_000}}],
    }
    assert MaxwellBot._message_content_chars(msg) >= 5_000


def test_history_window_start_holds_still_when_a_new_message_arrives():
    """A one-message slide per turn would move the transcript's first bytes."""
    old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    history = [
        {
            "author": "alice",
            "author_id": "456",
            "content": f"msg {i}",
            "timestamp": old,
        }
        for i in range(300)
    ]
    memory = FakeMemory(history)
    bot = _bot(memory)
    bot._control["memory_history_messages"] = 160

    def first_line():
        async def run():
            messages = await MaxwellBot._build_messages(bot, _message(), "latest")
            block = next(
                m for m in messages if "<previous_conversation>" in str(m["content"])
            )
            return block["content"].splitlines()[1]

        return asyncio.run(run())

    before = first_line()
    memory.messages.append(
        {
            "author": "alice",
            "author_id": "456",
            "content": "brand new",
            "timestamp": old,
        }
    )
    assert first_line() == before
