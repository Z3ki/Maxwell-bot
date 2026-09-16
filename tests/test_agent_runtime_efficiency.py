"""Regression coverage for background-agent prompting and context efficiency."""

from jobs import (
    BG_CONTEXT_CHARS_HARD_CAP,
    BG_CONTEXT_CHARS_MIN,
    SpawnBackgroundTool,
    _call_signature,
    _compact_worker_messages,
    _message_chars,
    _worker_system_body,
    resolve_job_context_chars,
)


def test_spawn_background_requests_self_contained_brief():
    tool = SpawnBackgroundTool(None)
    text = tool.get_description().lower()
    assert "self-contained goal" in text
    assert "do not paste" in text
    assert "transcript" in text
    assert "essential facts" in text


def test_worker_prompt_pushes_convergence_and_avoids_repeat_work():
    text = _worker_system_body("abc123", "research the launch", "prefer primary sources").lower()
    assert "smallest sufficient tool sequence" in text
    assert "do not repeat a successful search" in text
    assert "verify externally visible or destructive work once" in text
    assert "fail concretely instead of looping" in text
    assert "finishing is the job" in text


def test_context_budget_clamps_control_values():
    assert resolve_job_context_chars({"bg_context_chars": 1}) == BG_CONTEXT_CHARS_MIN
    assert (
        resolve_job_context_chars({"bg_context_chars": 999999999})
        == BG_CONTEXT_CHARS_HARD_CAP
    )


def test_context_compaction_keeps_goal_recent_tail_and_bounded_size():
    messages = [
        {"role": "system", "content": "system rules"},
        {"role": "user", "content": "the original goal"},
    ]
    for i in range(30):
        messages.extend(
            [
                {"role": "assistant", "content": f"step {i} " + "x" * 700},
                {"role": "user", "content": f"result {i} " + "y" * 700},
            ]
        )

    compacted = _compact_worker_messages(messages, max_chars=9000, keep_recent=8)

    assert compacted[0] == messages[0]
    assert compacted[1] == messages[1]
    assert compacted[-1] == messages[-1]
    assert "assistant:" in compacted[2]["content"]
    assert "result 25" in compacted[2]["content"]
    assert sum(_message_chars(m) for m in compacted) <= 9000
    assert len(compacted) < len(messages)


def test_context_compaction_does_not_start_recent_tail_with_tool_result():
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "goal"},
        {"role": "assistant", "content": "old" + "x" * 9000},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "shell", "arguments": '{"cmd":"pwd"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "/workspace"},
        {"role": "user", "content": "continue"},
    ]

    compacted = _compact_worker_messages(messages, max_chars=8000, keep_recent=2)
    roles = [m.get("role") for m in compacted]
    tool_index = roles.index("tool")
    assert tool_index > 0
    assert compacted[tool_index - 1].get("role") == "assistant"
    assert compacted[tool_index - 1].get("tool_calls")


def test_call_signature_normalizes_json_argument_order():
    left = {
        "function": {"name": "shell", "arguments": '{"b":2,"a":1}'},
    }
    right = {
        "function": {"name": "shell", "arguments": '{"a":1,"b":2}'},
    }
    assert _call_signature(left) == _call_signature(right)
