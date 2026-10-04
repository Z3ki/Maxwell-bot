"""Prompt timestamps and explicit history limits honor user-facing choices."""

import asyncio
import os
import time
from types import SimpleNamespace

import pytest

from bot import MaxwellBot, _format_context_timestamp


class Memory:
    def __init__(self, rows):
        self.rows = rows

    async def get_channel_memory(self, _channel_id, *, requester=None):
        return list(self.rows)


def _owner(rows, **control):
    return SimpleNamespace(
        _control={
            "base_personality": "test",
            "cross_context_enabled": False,
            "emoji_context_enabled": False,
            "entity_memory_enabled": False,
            "long_term_memory_enabled": False,
            "memory_context_budget": 30000,
            "memory_history_messages": 20,
            "tool_history_messages": 4,
            "music_context_enabled": False,
            "tools_enabled": False,
            **control,
        },
        _drugged_until={},
        _guild_emojis={},
        _recent_users={},
        _tool_system_prompt=lambda *args, **kwargs: "",
        _is_short_live_turn=lambda *args: False,
        _reply_parent_context_lines=lambda _message: [],
        bot_name="Maxwell",
        memory=Memory(rows),
        user=SimpleNamespace(display_name="Maxwell", id=1),
    )


def _message():
    return SimpleNamespace(
        author=SimpleNamespace(bot=False, display_name="Alice", id=456),
        channel=SimpleNamespace(id=123),
        guild=None,
        id=999,
        mentions=[],
        reference=None,
    )


def _transcript(owner):
    messages = asyncio.run(
        MaxwellBot._build_messages(owner, _message(), "Latest request")
    )
    return "\n".join(
        str(row["content"])
        for row in messages
        if str(row.get("content", "")).startswith("<previous_conversation>")
    )


@pytest.mark.parametrize(
    "stamp,expected",
    [
        ("2026-10-04T20:15:00Z", "Sun 2026-10-04 16:15"),
        ("2026-10-05T01:00:00+00:00", "Sun 2026-10-04 21:00"),
        ("2026-01-04T20:15:00+00:00", "Sun 2026-01-04 16:15"),
    ],
)
def test_prompt_timestamp_uses_puerto_rico_time_in_every_host_timezone(
    monkeypatch, stamp, expected
):
    original = os.environ.get("TZ")
    try:
        results = []
        for host_timezone in ("UTC", "America/Los_Angeles", "Pacific/Honolulu"):
            monkeypatch.setenv("TZ", host_timezone)
            if hasattr(time, "tzset"):
                time.tzset()
            results.append(_format_context_timestamp(stamp, relative=False))
        assert all(expected in rendered for rendered in results)
        assert len(set(results)) == 1
    finally:
        if original is None:
            monkeypatch.delenv("TZ", raising=False)
        else:
            monkeypatch.setenv("TZ", original)
        if hasattr(time, "tzset"):
            time.tzset()


def test_each_historical_row_renders_its_timestamp_once():
    rows = [
        {
            "message_id": "100",
            "author": "Alice",
            "author_id": "456",
            "content": "Old request",
            "timestamp": "2026-10-04T20:15:00Z",
        },
        {
            "message_id": "101",
            "author": "Maxwell",
            "author_id": "1",
            "content": "Old reply",
            "timestamp": "2026-10-04T20:16:00Z",
        },
    ]
    transcript = _transcript(_owner(rows))
    for row in rows:
        rendered = _format_context_timestamp(row["timestamp"], relative=False)
        assert transcript.count(rendered) == 1


def test_zero_message_history_disables_transcript_users_but_keeps_selected_tools():
    rows = [
        {
            "message_id": "100",
            "author": "Alice",
            "author_id": "456",
            "content": "OLD_USER",
        },
        {"message_id": "101", "is_tool": True, "content": "OLD_TOOL"},
    ]
    transcript = _transcript(
        _owner(rows, memory_history_messages=0, tool_history_messages=1)
    )
    assert "OLD_USER" not in transcript
    assert "OLD_TOOL" in transcript


def test_zero_tool_history_does_not_restore_outside_window_tool_rows():
    rows = [
        {"message_id": "100", "is_tool": True, "content": "OLD_TOOL"},
        {
            "message_id": "101",
            "author": "Alice",
            "author_id": "456",
            "content": "LATEST_USER",
        },
    ]
    transcript = _transcript(
        _owner(rows, memory_history_messages=1, tool_history_messages=0)
    )
    assert "OLD_TOOL" not in transcript
    assert "LATEST_USER" in transcript


def test_zero_message_and_tool_history_produce_no_transcript():
    rows = [
        {
            "message_id": "100",
            "author": "Alice",
            "author_id": "456",
            "content": "OLD_USER",
        },
        {"message_id": "101", "is_tool": True, "content": "OLD_TOOL"},
    ]
    assert (
        _transcript(_owner(rows, memory_history_messages=0, tool_history_messages=0))
        == ""
    )
