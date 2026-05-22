import json

from rem import rem_system_prompt, short_term_slice_prompt


def test_rem_system_prompt_shape():
    prompt = rem_system_prompt(2)
    assert "You are Maxwell REM" in prompt
    assert "not answering live chat" in prompt
    assert "tears in rain" in prompt
    assert "2 REM tool turn(s)" in prompt
    assert "DONE" in prompt


def test_short_term_slice_prompt_serializes_stably():
    events = [{"role": "user", "content": "hello", "ts": "2026-01-01T00:00:00+00:00"}]
    prompt = short_term_slice_prompt(events)
    assert "model reasoning intentionally excluded" in prompt
    payload = prompt.split("\n\n", 1)[1]
    assert json.loads(payload) == events
