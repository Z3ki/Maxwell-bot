import json
import logging
import sys

import pytest

from diagnostics_safety import SafeLogFormatter, log_context, redact_diagnostics


def _record(level, message, **extra):
    record = logging.LogRecord("test", level, __file__, 10, message, (), None)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_info_preserves_message_and_propagates_context():
    token = log_context.set({"channel_id": "123"})
    try:
        data = json.loads(SafeLogFormatter(json_format=True).format(
            _record(logging.INFO, "gateway reconnected")
        ))
    finally:
        log_context.reset(token)
    assert data["message"] == "gateway reconnected"
    assert data["channel_id"] == "123"


def test_safe_events_and_debug_are_redacted_and_bounded(monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "test-credential-123")
    formatter = SafeLogFormatter(json_format=True)
    record = _record(logging.DEBUG, "test-credential-123 " + "x" * 5000)
    data = json.loads(formatter.format(record))
    assert "test-credential-123" not in data["message"]
    assert len(data["message"]) <= 200
    event = _record(logging.INFO, "turn finished", safe_log=True, duration_ms=12)
    assert json.loads(formatter.format(event))["message"] == "turn finished"


def test_exception_contains_type_message_and_stack_locations():
    try:
        raise ValueError("provider unavailable")
    except ValueError:
        record = _record(logging.ERROR, "provider call failed")
        record.exc_info = sys.exc_info()
    data = json.loads(SafeLogFormatter(json_format=True).format(record))
    assert data["message"] == "provider call failed"
    assert data["exception_type"] == "ValueError"
    assert data["exception_message"] == "provider unavailable"
    assert data["stack"]


@pytest.mark.parametrize("json_format", [False, True])
def test_messages_and_exceptions_redact_secrets_before_truncation(monkeypatch, json_format):
    monkeypatch.setenv("CUSTOM_SECRET", "configured-private-value")
    secrets = [
        "configured-private-value",
        "sk-abcdefghijklmnop",
        "Bearer abcdefghijklmnopqrstuvwxyz",
        "https://discord.com/api/webhooks/123456/very-private-token",
        "abcdefghijklmnopqrstuvwx.abcdef.abcdefghijklmnopqrstuvwxyz",
    ]
    text = " ".join(secrets)
    try:
        raise ValueError(text + " " + "x" * 400)
    except ValueError:
        record = _record(logging.WARNING, text + " " + "x" * 400)
        record.exc_info = sys.exc_info()
    output = SafeLogFormatter(json_format=json_format).format(record)
    for secret in secrets:
        assert secret not in output
    assert "[redacted" in output
    assert "ValueError" in output
    if json_format:
        data = json.loads(output)
        assert len(data["message"]) <= 300
        assert len(data["exception_message"]) <= 300
        assert data["message"].endswith("…")
        assert data["exception_message"].endswith("…")


def test_discord_webhooks_and_tokens_are_redacted():
    webhook = "https://discord.com/api/webhooks/123456/very-private-token"
    token = "abcdefghijklmnopqrstuvwx.abcdef.abcdefghijklmnopqrstuvwxyz"
    result = redact_diagnostics(f"{webhook} {token}")
    assert webhook not in result
    assert token not in result


def test_generic_environment_secrets_are_redacted(monkeypatch):
    monkeypatch.setenv("CUSTOM_KEY", "xyzw")
    monkeypatch.setenv("AUDIT_WEBHOOK", "webhook-secret-value")
    monkeypatch.setenv("DISCORD_TOKEN", "abc123def456ghi789")
    text = redact_diagnostics(
        "xyzw webhook-secret-value token=abc123def456ghi789; (abc123def456ghi789)"
    )
    assert "xyzw" not in text
    assert "webhook-secret-value" not in text
    assert "abc123def456ghi789" not in text
    assert text.count("[redacted]") == 4


def test_non_secret_settings_do_not_scrub_logs(monkeypatch):
    # Issue #136: counts and flags whose names contain "token"/"key".
    monkeypatch.setenv("AI_MAX_OUTPUT_TOKENS", "2048")
    monkeypatch.setenv("AI_WORKER_MAX_OUTPUT_TOKENS", "32768")
    monkeypatch.setenv("LIVE_TEST_WEBHOOK_IDS", "1204812345,99")
    monkeypatch.setenv("SAND_BOX_PERSISTENT_KEYRING", "1")
    monkeypatch.setenv("SESSION_ENABLED", "true")
    monkeypatch.setenv("CUSTOM_KEY", "xy")
    text = "Requested max_tokens=2048; channel 1204812345; image 2048x1536; id 123 xy"
    assert redact_diagnostics(text) == text


def test_env_secret_is_redacted_only_at_token_boundaries(monkeypatch):
    monkeypatch.setenv("CUSTOM_SECRET", "s3cr3tv4lue")
    text = redact_diagnostics("x s3cr3tv4lue id 9s3cr3tv4lue9")
    assert text == "x [redacted] id 9s3cr3tv4lue9"
