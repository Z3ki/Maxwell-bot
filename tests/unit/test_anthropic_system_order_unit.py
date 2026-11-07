"""Issue #138: only leading system turns become Anthropic's top-level system."""

import pytest

from maxwell_core.providers.anthropic import translate_messages
from maxwell_core.providers.protocol import ProviderProtocolError


def _worker_payload(round_note):
    return [
        {"role": "system", "content": "Complete only the assigned task."},
        {"role": "developer", "content": "Use tools carefully."},
        {"role": "user", "content": "Build a thing"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": "shell", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "1", "content": "ok"},
        {"role": "system", "content": round_note},
    ]


def test_trailing_system_turn_stays_at_the_end():
    system, messages = translate_messages(
        _worker_payload("[Worker budget] Round 3 of 20.")
    )
    assert [block["text"] for block in system] == [
        "Complete only the assigned task.",
        "Use tools carefully.",
    ]
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    last = messages[-1]["content"]
    assert last[0]["type"] == "tool_result"
    assert last[-1] == {
        "type": "text",
        "text": "[System note]\n[Worker budget] Round 3 of 20.",
    }


def test_system_prefix_is_stable_across_worker_rounds():
    first, _ = translate_messages(_worker_payload("[Worker budget] Round 1 of 20."))
    later, _ = translate_messages(_worker_payload("[Worker budget] Round 9 of 20."))
    assert first == later


def test_private_context_after_transcript_keeps_its_position():
    system, messages = translate_messages(
        [
            {"role": "system", "content": "Static instructions."},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "system", "content": "[PRIVATE INTERNAL CONTEXT] per-turn"},
            {"role": "user", "content": "what now?"},
        ]
    )
    assert [block["text"] for block in system] == ["Static instructions."]
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    assert [b["text"] for b in messages[-1]["content"]] == [
        "[System note]\n[PRIVATE INTERNAL CONTEXT] per-turn",
        "what now?",
    ]


def test_later_system_turn_must_be_text():
    with pytest.raises(ProviderProtocolError):
        translate_messages(
            [
                {"role": "user", "content": "hi"},
                {
                    "role": "system",
                    "content": [
                        {"type": "image_url", "image_url": {"url": "https://x/y.png"}}
                    ],
                },
            ]
        )
