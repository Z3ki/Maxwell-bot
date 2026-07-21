import json

from rem import rem_system_prompt, short_term_slice_prompt


def test_rem_system_prompt_shape():
    prompt = rem_system_prompt(2)
    assert "You are Maxwell REM" in prompt
    assert "not answering live chat" in prompt
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
