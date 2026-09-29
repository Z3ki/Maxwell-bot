import json

import pytest

from rem import (
    REM_SLICE_MAX_CHARS,
    rem_system_prompt,
    short_term_slice_prompt,
    _extract_rem_json,
)


def test_rem_system_prompt_shape():
    prompt = rem_system_prompt(2, bot_name="Maxwell")
    assert "You are Maxwell REM" in prompt
    assert "not live chat" in prompt
    # REM is a single pass (no multi-turn loop), so the prompt must not
    # advertise a remaining turn count that the runner never honors.
    assert "REM turn(s)" not in prompt
    # 2026-07-21: REM now ends with a JSON actions block (ltm_add / shared_add
    # / etc) and the runner parses it. The prompt must instruct the model
    # to emit that JSON and to provide an audit field, but must NOT
    # advertise "DONE" as the response — that was the old bypass-tools
    # contract.
    assert "JSON" in prompt
    assert "actions" in prompt
    assert "DONE" not in prompt


def test_rem_system_prompt_uses_custom_bot_name():
    prompt = rem_system_prompt(0, bot_name="Nova")
    assert "You are Nova REM" in prompt
    assert "You are Maxwell REM" not in prompt


def test_short_term_slice_prompt_serializes_stably():
    events = [{"role": "user", "content": "hello", "ts": "2026-01-01T00:00:00+00:00"}]
    prompt = short_term_slice_prompt(events)
    assert "reasoning excluded" in prompt
    payload = prompt.split("\n", 1)[1]
    assert json.loads(payload) == events


def test_extract_rem_json_allows_braces_inside_strings():
    raw = 'notes here {"audit":"text } here","actions":{}}'
    payload = _extract_rem_json(raw)
    assert payload["audit"] == "text } here"
    assert payload["actions"] == {}


def test_extract_rem_json_ignores_trailing_unrelated_objects():
    raw = '{"actions":{"ltm_add":["cats"]},"audit":"kept"} then {"ok":true}'
    payload = _extract_rem_json(raw)
    assert payload["audit"] == "kept"
    assert payload["actions"]["ltm_add"] == ["cats"]


def test_rem_prompt_example_is_valid_json():
    prompt = rem_system_prompt(0, bot_name="Nova")
    payload = _extract_rem_json(prompt)
    assert payload is not None
    assert "actions" in payload
    assert "audit" in payload
    json.dumps(payload)


@pytest.mark.parametrize("content", ["x" * 4000, '"\\\n' * 1500])
def test_short_term_slice_prompt_bounds_serialized_slice_without_losing_events(content):
    events = [
        {
            "role": "user",
            "content": content,
            "ts": f"2026-01-01T00:00:{i:02d}+00:00",
            "channel_id": str(i),
        }
        for i in range(500)
    ]
    prompt = short_term_slice_prompt(events)
    assert len(prompt) <= REM_SLICE_MAX_CHARS
    payload = json.loads(prompt.split("\n", 1)[1])
    assert [e["channel_id"] for e in payload] == [e["channel_id"] for e in events]
    assert [e["ts"] for e in payload] == [e["ts"] for e in events]
    assert all(content.startswith(e["content"].rstrip("…")) for e in payload)
    assert events[0]["content"] == content


def test_short_term_slice_rejects_metadata_overflow_without_dropping_events():
    with pytest.raises(ValueError, match="metadata"):
        short_term_slice_prompt([{"content": "fact", "source": "x" * REM_SLICE_MAX_CHARS}])
