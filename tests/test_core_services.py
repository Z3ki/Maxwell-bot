"""Behavior boundaries of helpers used by the Discord turn loop."""

import json
import subprocess
import sys

import pytest

from maxwell_core.tools.dispatch import ToolCircuitBreaker, _prepare_tool_params
from maxwell_core.transport.attachments import (
    TEXT_ATTACHMENT_MAX_CHARS,
    _decode_readable_text,
    _is_text_attachment,
)


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16", "latin-1"])
@pytest.mark.parametrize("text", ["", "hello\nworld", "café español", "café"])
def test_attachment_decoder_preserves_supported_text(encoding, text):
    assert _decode_readable_text(text.encode(encoding)) == text


def test_latin1_bytes_are_not_silently_reinterpreted_as_utf16():
    # The old unconditional UTF-16 fallback decoded this to unrelated CJK text.
    assert _decode_readable_text(b"caf\xe9") == "café"


def test_long_attachment_keeps_both_ends_and_reports_truncation():
    text = "HEAD" + "x" * (TEXT_ATTACHMENT_MAX_CHARS + 1000) + "TAIL"
    decoded = _decode_readable_text(text.encode())
    assert decoded.startswith("HEAD") and decoded.endswith("TAIL")
    assert "truncated" in decoded
    assert len(decoded) < TEXT_ATTACHMENT_MAX_CHARS + 100


@pytest.mark.parametrize(
    ("filename", "mime", "data", "expected"),
    [
        ("settings.py", "application/octet-stream", None, True),
        ("settings", "text/plain; charset=UTF-8", None, True),
        ("data.json", "application/json", None, True),
        ("unknown", "application/octet-stream", b"plain prose", True),
        ("unknown", "application/octet-stream", b"\x00\x01\xff", False),
        ("photo.jpg", "image/jpeg", None, False),
    ],
)
def test_attachment_classification(filename, mime, data, expected):
    assert _is_text_attachment(filename, mime, data) is expected


def test_breaker_isolated_failures_expire_without_real_sleep():
    now = [100.0]
    breaker = ToolCircuitBreaker(2, 10, clock=lambda: now[0])
    breaker.record_failure("web_search")
    assert not breaker.is_open("web_search")
    breaker.record_failure("web_search")
    assert breaker.is_open("web_search")
    assert not breaker.is_open("send_message")
    now[0] = 110.0
    assert not breaker.is_open("web_search")
    now[0] = 200.0
    breaker.record_failure("web_search")
    assert not breaker.is_open("web_search")
    breaker.record_failure("web_search")
    assert breaker.is_open("web_search")
    breaker.record_success("web_search")
    assert not breaker.is_open("web_search")


def test_parameter_normalization_does_not_mutate_model_arguments():
    arguments = {"message": "reply", "self": "oops", "reply": False}
    normalized = _prepare_tool_params("send_message", arguments)
    assert normalized == {"content": "reply", "reply": False}
    assert arguments == {"message": "reply", "self": "oops", "reply": False}


def test_blank_message_alias_does_not_hide_a_valid_text_alias():
    assert (
        _prepare_tool_params(
            "send_message", {"message": "   ", "text": "actual reply"}
        )["content"]
        == "actual reply"
    )


def test_existing_content_wins_over_alias():
    assert (
        _prepare_tool_params(
            "send_message", {"message": "alias", "content": "original"}
        )["content"]
        == "original"
    )


def test_prompt_service_imports_without_initializing_discord_bot():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json, sys; "
            "from maxwell_core.prompts.conversation import ConversationPromptBuilder; "
            "print(json.dumps({'bot_loaded': 'bot' in sys.modules}))",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {"bot_loaded": False}
