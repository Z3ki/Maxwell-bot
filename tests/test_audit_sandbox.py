"""Regressions for privileged automation and remote mail boundaries."""

import imaplib
import ssl
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import bot_tools
import mail_transport


@pytest.mark.parametrize("host", ["mail.example.test", "192.168.1.5", "8.8.8.8"])
def test_remote_mail_verifies_certificates(host):
    context = mail_transport.mail_ssl_context(host)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname


@pytest.mark.parametrize(
    "host", ["127.0.0.1", "::1", "localhost", "host.docker.internal"]
)
def test_local_mail_retains_self_signed_compatibility(host):
    context = mail_transport.mail_ssl_context(host)
    assert context.verify_mode == ssl.CERT_NONE
    assert not context.check_hostname


def test_imap_login_failure_closes_connection(monkeypatch):
    connection = SimpleNamespace(
        login=Mock(side_effect=imaplib.IMAP4.error("denied")), shutdown=Mock()
    )
    constructor = Mock(return_value=connection)
    monkeypatch.setattr(mail_transport.imaplib, "IMAP4_SSL", constructor)
    with pytest.raises(imaplib.IMAP4.error):
        mail_transport.connect_imap("mail.example.test", 993, "user", "pass")
    connection.shutdown.assert_called_once()
    assert constructor.call_args.kwargs["timeout"] == 60
    assert constructor.call_args.kwargs["ssl_context"].verify_mode == ssl.CERT_REQUIRED


def test_mail_fetch_never_substitutes_sequence_number(monkeypatch):
    connection = SimpleNamespace(
        select=Mock(return_value=("OK", [])),
        uid=Mock(return_value=("OK", [None])),
        fetch=Mock(),
        close=Mock(),
        logout=Mock(),
    )
    monkeypatch.setattr(bot_tools, "_imap_connect_sync", lambda *_: connection)
    result = bot_tools._imap_get_message_sync("localhost", 993, "u", "p", "5", 2000)
    assert result.startswith("Error:")
    connection.select.assert_called_once_with("INBOX", readonly=True)
    connection.fetch.assert_not_called()
    connection.uid.assert_called_once_with("FETCH", "5", "(BODY.PEEK[])")


def test_mail_fetch_includes_raw_authentication_headers(monkeypatch):
    raw_message = (
        b"From: sender@example.test\r\n"
        b"Authentication-Results: mx.example.test; spf=pass; dkim=pass\r\n"
        b"Received-SPF: pass (mx.example.test: domain of sender@example.test)\r\n"
        b"DKIM-Signature: v=1; d=example.test;\r\n"
        b"\tb=signature-value\r\n"
        b"Received: by mx.example.test with ESMTP; id abc123\r\n"
        b"X-Internal-Tracking: should-not-be-included\r\n"
        b"\r\nmessage body"
    )
    connection = SimpleNamespace(
        select=Mock(return_value=("OK", [])),
        uid=Mock(return_value=("OK", [(b"1 (BODY[])", raw_message)])),
        close=Mock(),
        logout=Mock(),
    )
    monkeypatch.setattr(bot_tools, "_imap_connect_sync", lambda *_: connection)

    result = bot_tools._imap_get_message_sync(
        "localhost", 993, "u", "p", "5", 2000
    )

    assert "Authentication-Results: mx.example.test; spf=pass; dkim=pass" in result
    assert "Received-SPF: pass" in result
    assert "DKIM-Signature: v=1; d=example.test;" in result
    assert "\tb=signature-value" in result
    assert "Received: by mx.example.test" in result
    assert "X-Internal-Tracking" not in result
    assert result.endswith("message body")


def test_mail_fetch_caps_raw_authentication_headers(monkeypatch):
    raw_message = b"Received: " + (b"x" * 15_000) + b"\r\n\r\nmessage body"
    connection = SimpleNamespace(
        select=Mock(return_value=("OK", [])),
        uid=Mock(return_value=("OK", [(b"1 (BODY[])", raw_message)])),
        close=Mock(),
        logout=Mock(),
    )
    monkeypatch.setattr(bot_tools, "_imap_connect_sync", lambda *_: connection)

    result = bot_tools._imap_get_message_sync(
        "localhost", 993, "u", "p", "5", 2000
    )

    assert "[truncated]" in result
    assert len(result) < 13_000
    assert result.endswith("message body")
