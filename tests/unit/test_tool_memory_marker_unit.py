"""The send_message delivery marker must be recognised through the tool prefix.

Production hands ``_remember_tool_call`` the dispatcher's line
(``"Tool send_message: __MESSAGE_SENT__\\n<text>"``), but the same-channel guard
tested ``result.startswith("__MESSAGE_SENT__")``, which is never true for a
prefixed line. Every delivered chunk was therefore also stored as a duplicate
``is_tool=True`` memory row on top of the real message row, and the model
replayed its own echo on the next turn.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from bot import MaxwellBot


def _host(rows):
    async def add_message_to_memory(channel_id, row, message):
        rows.append((channel_id, row))

    host = SimpleNamespace(
        _control={"store_memory": True},
        memory=object(),
        add_message_to_memory=add_message_to_memory,
    )
    host._remember_tool_call = MaxwellBot._remember_tool_call.__get__(host)
    return host


def _message(channel_id=123, author_id=456):
    return SimpleNamespace(
        id=999,
        channel=SimpleNamespace(id=channel_id),
        author=SimpleNamespace(id=author_id, bot=False),
    )


def test_prefixed_same_channel_send_is_not_recorded_as_a_tool_row():
    rows = []
    host = _host(rows)
    asyncio.run(
        host._remember_tool_call(
            _message(),
            "send_message",
            {},
            "Tool send_message: __MESSAGE_SENT__\nhello there",
        )
    )
    assert rows == []


def test_raw_same_channel_send_is_not_recorded_as_a_tool_row():
    rows = []
    host = _host(rows)
    asyncio.run(
        host._remember_tool_call(
            _message(), "send_message", {}, "__MESSAGE_SENT__\nhello there"
        )
    )
    assert rows == []


def test_prefixed_cross_channel_send_records_a_content_free_tool_row():
    rows = []
    host = _host(rows)
    asyncio.run(
        host._remember_tool_call(
            _message(),
            "send_message",
            {"channel_id": "999", "content": "secret body"},
            "Tool send_message: __MESSAGE_SENT__\nsecret body",
        )
    )
    assert len(rows) == 1
    _, row = rows[0]
    assert row["is_tool"] is True
    assert row["tool_result"] == "__MESSAGE_SENT__"
    assert "secret body" not in row["content"]
    assert row["tool_params"].get("channel_id") == "999"


def test_ordinary_tool_calls_still_record():
    rows = []
    host = _host(rows)
    asyncio.run(
        host._remember_tool_call(
            _message(), "web_search", {"query": "x"}, "Tool web_search: 3 results"
        )
    )
    assert len(rows) == 1
    _, row = rows[0]
    assert row["tool_name"] == "web_search"
    assert "3 results" in row["tool_result"]
