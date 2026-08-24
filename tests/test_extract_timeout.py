"""Tests for cross_context_extract_timeout_seconds clamping.

The 20s hardcoded timeout in _extract_shared_context_fact was too tight
for cold-start 1M-context models — the call would time out, retry, fall
back to a smaller model, and flood the provider log. The fix adds a
configurable knob (default 60s, range 5-600) via bot_control.json. The
clamp must run on the API sanitizer side so dashboard saves can't push
the value out of range.
"""

import sys
from pathlib import Path

# The api_server module pulls a lot of optional deps at import time. Use
# the same import shim the other api-related tests rely on.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from api import api_server  # noqa: E402


def test_extract_timeout_default():
    out = api_server._sanitize_control({})
    assert out["cross_context_extract_timeout_seconds"] == 60


def test_extract_timeout_in_range():
    out = api_server._sanitize_control(
        {"cross_context_extract_timeout_seconds": 90}
    )
    assert out["cross_context_extract_timeout_seconds"] == 90


def test_extract_timeout_clamped_low():
    # 0 and 1 should be clamped up to the 5s floor — the call would
    # otherwise time out before the request even leaves the gate.
    out = api_server._sanitize_control(
        {"cross_context_extract_timeout_seconds": 0}
    )
    assert out["cross_context_extract_timeout_seconds"] == 5
    out = api_server._sanitize_control(
        {"cross_context_extract_timeout_seconds": 1}
    )
    assert out["cross_context_extract_timeout_seconds"] == 5


def test_extract_timeout_clamped_high():
    # 9999 should be clamped down to 600s — the operator ceiling.
    out = api_server._sanitize_control(
        {"cross_context_extract_timeout_seconds": 9999}
    )
    assert out["cross_context_extract_timeout_seconds"] == 600


def test_extract_timeout_handles_garbage():
    # Non-numeric input falls back to the default (60s) rather than
    # blowing up the dashboard save.
    out = api_server._sanitize_control(
        {"cross_context_extract_timeout_seconds": "definitely not a number"}
    )
    assert out["cross_context_extract_timeout_seconds"] == 60


def test_mail_poll_interval_is_clamped():
    """IMAP login is not free; a dashboard typo must not poll every second."""
    assert api_server._sanitize_control({})["email_inbox_poll_seconds"] == 120
    assert (
        api_server._sanitize_control({"email_inbox_poll_seconds": 300})[
            "email_inbox_poll_seconds"
        ]
        == 300
    )
    assert (
        api_server._sanitize_control({"email_inbox_poll_seconds": 1})[
            "email_inbox_poll_seconds"
        ]
        == 30
    )
    assert (
        api_server._sanitize_control({"email_inbox_poll_seconds": 99999})[
            "email_inbox_poll_seconds"
        ]
        == 3600
    )


def test_extract_threshold_is_clamped_to_a_probability():
    assert api_server._sanitize_control({})["cross_context_extract_threshold"] == 0.25
    out = api_server._sanitize_control({"cross_context_extract_threshold": 0.8})
    assert out["cross_context_extract_threshold"] == 0.8
    out = api_server._sanitize_control({"cross_context_extract_threshold": 7})
    assert out["cross_context_extract_threshold"] == 1.0
    out = api_server._sanitize_control({"cross_context_extract_threshold": -3})
    assert out["cross_context_extract_threshold"] == 0.0
    out = api_server._sanitize_control({"cross_context_extract_threshold": "nope"})
    assert out["cross_context_extract_threshold"] == 0.25
