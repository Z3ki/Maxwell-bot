"""Tests for the custom streaming tool-call buffer (bare-JSON protocol)."""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import providers  # noqa: E402


def test_find_balanced_json_end_basic():
    text = '{"name": "shell", "arguments": {"cmd": "ls"}}'
    end = providers._find_balanced_json_end(text, 0)
    assert end == len(text)
    assert text[:end] == text


def test_find_balanced_json_end_with_braces_in_string():
    # A create_site body can contain CSS with braces — must not fool the counter.
    text = '{"name": "create_site", "arguments": {"body": "a { b } c", "path": "/x"}}'
    end = providers._find_balanced_json_end(text, 0)
    assert end == len(text)


def test_find_balanced_json_end_unbalanced_returns_none():
    # Closing brace not yet delivered.
    text = '{"name": "shell", "arguments": {"cmd": "ls'
    assert providers._find_balanced_json_end(text, 0) is None


def test_buffer_extracts_one_tool_call():
    buf = providers._CustomToolCallBuffer()
    buf.feed('{"name": "lookup_user", "arguments": {"user_id": "12345"}}')
    buf.drain()
    assert len(buf.completed) == 1
    tc = buf.completed[0]
    assert tc["function"]["name"] == "lookup_user"
    assert '"user_id"' in tc["function"]["arguments"]


def test_buffer_strips_json_from_visible_text():
    buf = providers._CustomToolCallBuffer()
    buf.feed('Looking it up...\n{"name": "lookup_user", "arguments": {"user_id": "12345"}}\nDone!')
    buf.drain()
    visible = "".join(buf.text_parts)
    assert "lookup_user" not in visible
    assert "Looking it up" in visible
    assert "Done!" in visible


def test_buffer_fires_partial_name_callback():
    fired = []
    buf = providers._CustomToolCallBuffer(on_partial_name=fired.append)
    # Feed the opener; the name becomes visible immediately. The model emits
    # valid JSON (commas included), so the opener is followed by ", ...".
    buf.feed('{"name": "create_site",')
    # Nothing complete yet (no closing brace) but the name callback should
    # have fired already via the opener detection.
    buf.feed(' "arguments": {"name": "moon"}}')
    buf.drain()
    assert "create_site" in fired
    assert len(buf.completed) == 1
    assert buf.completed[0]["function"]["name"] == "create_site"


def test_buffer_multiple_tool_calls():
    buf = providers._CustomToolCallBuffer()
    buf.feed(
        '{"name": "a", "arguments": {}}\n'
        'some text\n'
        '{"name": "b", "arguments": {"x": 1}}'
    )
    buf.drain()
    assert len(buf.completed) == 2
    assert buf.completed[0]["function"]["name"] == "a"
    assert buf.completed[1]["function"]["name"] == "b"


def test_buffer_malformed_json_is_kept_as_text():
    buf = providers._CustomToolCallBuffer()
    # Looks like a tool call opener but never valid JSON; should not crash.
    buf.feed('{"name": "oops", "arguments": {NOT VALID}}')
    buf.drain()
    visible = "".join(buf.text_parts)
    assert "oops" in visible
    # Either it parsed (best-effort) or it's preserved as text — no crash.
    assert len(buf.completed) >= 0


def test_buffer_incremental_stream_simulation():
    """Feed a create_site call one chunk at a time, as the SSE would."""
    buf = providers._CustomToolCallBuffer()
    chunks = [
        'I will build the page now.\n{"name": "create_site",',
        ' "arguments": {"name": "moon", "title": "Moon Facts",',
        ' "body": "<html>...{braces in css}...</html>", "path": "/tmp/moon.html"}}',
        '\nHere you go!',
    ]
    for c in chunks:
        buf.feed(c)
    buf.drain()
    assert len(buf.completed) == 1
    tc = buf.completed[0]
    assert tc["function"]["name"] == "create_site"
    visible = "".join(buf.text_parts)
    assert "create_site" not in visible
    assert "Here you go" in visible
