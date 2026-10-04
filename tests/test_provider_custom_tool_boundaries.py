"""Custom protocol recovery must preserve text and avoid inventing arguments."""

import json

import pytest

from providers import _CustomToolCallBuffer


@pytest.mark.parametrize(
    "candidate",
    [
        '{"name":"oops","arguments":{NOT VALID}}',
        '{"name":"oops","arguments":[]}',
        '{"name":"oops","arguments":"not an object"}',
        '{"name":"","arguments":{}}',
    ],
)
def test_false_tool_opener_preserves_every_character_and_does_not_dispatch(candidate):
    text = "Before " + candidate + " after."
    for split in range(1, len(text)):
        buffer = _CustomToolCallBuffer()
        buffer.feed(text[:split])
        buffer.feed(text[split:])
        buffer.drain()
        assert "".join(buffer.text_parts) == text
        assert buffer.completed == []


def test_custom_callback_receives_decoded_tool_name():
    seen = []
    buffer = _CustomToolCallBuffer(on_partial_name=seen.append)
    buffer.feed('{"name":"web\\u005fsearch","arguments":{}}')
    buffer.drain()
    assert seen == ["web_search"]
    assert buffer.completed[0]["function"]["name"] == "web_search"


def test_custom_parser_does_not_retain_duplicate_copies_of_released_prose():
    buffer = _CustomToolCallBuffer()
    for _ in range(1000):
        assert buffer.feed("normal prose ") == "normal prose "
    assert len(buffer._buf) <= 256
    assert "".join(buffer.text_parts) == "normal prose " * 1000


def test_custom_parser_extracts_valid_calls_after_false_opener_without_losing_prose():
    valid = json.dumps({"name": "web_search", "arguments": {"query": "weather"}})
    before = '{"name":"broken","arguments":{not json}}\n'
    buffer = _CustomToolCallBuffer()
    buffer.feed(before + valid + "\nFinished.")
    buffer.drain()
    assert "".join(buffer.text_parts) == before + "\nFinished."
    assert [call["function"]["name"] for call in buffer.completed] == ["web_search"]
