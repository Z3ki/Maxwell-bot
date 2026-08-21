import json

from rem import rem_system_prompt, short_term_slice_prompt, _extract_rem_json


def test_rem_system_prompt_shape():
    prompt = rem_system_prompt(2)
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


def test_short_term_slice_prompt_bounds_a_huge_slice():
    """500 events x 4000 chars would serialize past any context window."""
    events = [
        {"role": "user", "content": "x" * 4000, "ts": f"2026-01-01T00:00:{i:02d}+00:00"}
        for i in range(500)
    ]
    prompt = short_term_slice_prompt(events)

    assert len(prompt) < 200_000
    payload = json.loads(prompt.split("\n", 1)[1])
    # Every event survives (shortened) — the watermark moves past this slice,
    # so a dropped event would never be assimilated at all.
    assert len(payload) == len(events)
    assert all("chars)" in e["content"] for e in payload)
