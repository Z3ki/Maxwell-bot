"""Regression: the dashboard must keep fractional watch debounce values."""

from __future__ import annotations

import pytest

from api.state import _sanitize_control


@pytest.mark.parametrize(
    "raw, expected",
    [
        (0.5, 0.5),
        ("0.5", 0.5),
        (2.5, 2.5),
        ("1.25", 1.25),
        (1, 1.0),
        (0.01, 0.05),
        (99, 5.0),
        ("junk", 1.0),
        (None, 1.0),
    ],
)
def test_fractional_debounce_survives_sanitizing(raw, expected):
    out = _sanitize_control({"conversation_watch_debounce_seconds": raw})
    assert out["conversation_watch_debounce_seconds"] == pytest.approx(expected)


def test_missing_debounce_uses_the_default():
    out = _sanitize_control({})
    assert out["conversation_watch_debounce_seconds"] == pytest.approx(1.0)
