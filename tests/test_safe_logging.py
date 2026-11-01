import json
import logging
import sys

from diagnostics_safety import SafeLogFormatter, log_context, redact_diagnostics


def _record(level, message, **extra):
    record = logging.LogRecord("test", level, __file__, 10, message, (), None)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_info_omits_untrusted_message_and_propagates_context():
    token = log_context.set({"channel_id": "123", "message": "private text"})
    try:
        data = json.loads(SafeLogFormatter(json_format=True).format(
            _record(logging.INFO, "private text")
        ))
    finally:
        log_context.reset(token)
    assert data["message"] == "[message omitted]"
    assert data["channel_id"] == "123"
    assert "private text" not in json.dumps(data)


def test_safe_events_and_debug_are_redacted_and_bounded(monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "test-credential-123")
    formatter = SafeLogFormatter(json_format=True)
    record = _record(logging.DEBUG, "test-credential-123 " + "x" * 5000)
    data = json.loads(formatter.format(record))
    assert "test-credential-123" not in data["message"]
    assert len(data["message"]) <= 200
    event = _record(logging.INFO, "turn finished", safe_log=True, duration_ms=12)
    assert json.loads(formatter.format(event))["message"] == "turn finished"


def test_exception_only_contains_type_and_stack_locations():
    try:
        raise ValueError("private message")
    except ValueError:
        record = _record(logging.ERROR, "failed: private message")
        record.exc_info = sys.exc_info()
    output = SafeLogFormatter(json_format=True).format(record)
    data = json.loads(output)
    assert "private message" not in output
    assert data["exception_type"] == "ValueError"
    assert data["stack"]


def test_discord_webhooks_and_tokens_are_redacted():
    webhook = "https://discord.com/api/webhooks/123456/very-private-token"
    token = "abcdefghijklmnopqrstuvwx.abcdef.abcdefghijklmnopqrstuvwxyz"
    result = redact_diagnostics(f"{webhook} {token}")
    assert webhook not in result
    assert token not in result


def test_short_and_generic_environment_secrets_are_redacted(monkeypatch):
    monkeypatch.setenv("CUSTOM_KEY", "xy")
    monkeypatch.setenv("AUDIT_WEBHOOK", "webhook-secret-value")
    text = redact_diagnostics("xy webhook-secret-value")
    assert "xy" not in text
    assert "webhook-secret-value" not in text
