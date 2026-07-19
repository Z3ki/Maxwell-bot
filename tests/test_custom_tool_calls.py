"""Tests for the custom streaming tool-call buffer (bare-JSON protocol)."""
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


def test_feed_return_value_never_leaks_raw_tool_json():
    """Regression test: feed() must return "" (not raw JSON fragments)
    while a tool-call opener is mid-stream and still balancing.

    This is the value bot.py's progress preview ("thinking: …") is built
    from. Before the fix, callers used the raw SSE delta instead of this
    return value, so a streamed '{"name": "shell", "arguments": {"command"'
    fragment would show up verbatim in the Discord status message.
    """
    buf = providers._CustomToolCallBuffer()
    chunks = [
        '{"name": "shell",',
        ' "arguments": {"command": "rm -rf /tmp/x",',
        ' "reasoning": "cleaning up"}}',
        '\nAll done!',
    ]
    revealed = []
    for c in chunks:
        visible = buf.feed(c)
        revealed.append(visible)
        # No partial or complete tool-call JSON should ever be revealed.
        assert '"name"' not in visible
        assert '"arguments"' not in visible
        assert "shell" not in visible
        assert "rm -rf" not in visible
    buf.drain()
    assert len(buf.completed) == 1
    assert buf.completed[0]["function"]["name"] == "shell"
    # The only visible text across the whole stream is the trailing reply.
    assert "".join(revealed).strip() == "All done!"


def test_feed_returns_visible_text_immediately_when_no_tool_call():
    """Plain reply text (no tool call) should be revealed immediately,
    not buffered — otherwise the live progress preview would lag."""
    buf = providers._CustomToolCallBuffer()
    assert buf.feed("Hello ") == "Hello "
    assert buf.feed("there!") == "there!"


def test_on_token_preview_uses_filtered_text_not_raw_delta():
    """End-to-end-ish: simulate what bot.py's _on_token callback receives
    when custom_tool_calls streaming feeds it deltas, and confirm the
    accumulated preview text never contains the raw tool-call JSON."""
    on_partial_name_calls = []
    buf = providers._CustomToolCallBuffer(
        on_partial_name=on_partial_name_calls.append
    )
    preview_deltas = []
    raw_chunks = [
        "Let me check that.\n",
        '{"name": "web_search", "argum',
        'ents": {"query": "weather today", "reasoning": "looking it up"}}',
        "\nHere's what I found.",
    ]
    for c in raw_chunks:
        visible = buf.feed(c)
        if visible:
            preview_deltas.append(visible)
    buf.drain()
    preview_text = "".join(preview_deltas)
    assert "web_search" not in preview_text
    assert "weather today" not in preview_text
    assert "Let me check that." in preview_text
    assert "Here's what I found." in preview_text
    # The tool name still reaches the progress UI via the dedicated
    # partial-name callback, just not embedded as raw JSON text.
    assert on_partial_name_calls == ["web_search"]


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
