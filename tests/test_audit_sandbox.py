"""Regressions for privileged automation and remote mail boundaries."""

import asyncio
import imaplib
import ssl
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import autofix
import bot_tools
import mail_transport


def test_autofix_requires_regression_tests(tmp_path):
    with pytest.raises(RuntimeError, match="requires a regression test"):
        asyncio.run(autofix._run_pytest(tmp_path, []))


@pytest.mark.parametrize("outcome", ["pass", "fail", "timeout", "cancel"])
def test_autofix_uses_isolation_and_always_removes_container(
    tmp_path, monkeypatch, outcome
):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_fix.py").write_text("def test_fix(): pass")
    monkeypatch.setenv("MAXWELL_AUTOFIX_TEST_IMAGE", "trusted-tests:local")
    monkeypatch.setenv("GITHUB_TOKEN", "test-secret-not-for-container")
    calls = []

    async def docker(*args, **kwargs):
        calls.append((args, kwargs))
        if args[0] == "run":
            if outcome == "timeout":
                raise TimeoutError
            if outcome == "cancel":
                raise asyncio.CancelledError
            return (b"pytest output", b""), int(outcome == "fail")
        return (b"", b""), 0

    monkeypatch.setattr(bot_tools, "_run_docker_cmd", docker)
    if outcome == "pass":
        assert (
            asyncio.run(autofix._run_pytest(tmp_path, ["tests/test_fix.py"]))
            == "pytest output"
        )
    else:
        error = asyncio.CancelledError if outcome == "cancel" else RuntimeError
        with pytest.raises(error):
            asyncio.run(autofix._run_pytest(tmp_path, ["tests/test_fix.py"]))
    run, opts = calls[0]
    assert run[run.index("--network") + 1] == "none"
    assert run[run.index("--user") + 1] == "65534:65534"
    assert "--read-only" in run and "--pull=never" in run
    assert "no-new-privileges:true" in run
    assert "trusted-tests:local" in run
    assert opts["output_limit"] == 64_000
    assert not any("docker.sock" in value or "test-secret" in value for value in run)
    assert calls[-1][0] == ("rm", "-f", run[run.index("--name") + 1])


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
