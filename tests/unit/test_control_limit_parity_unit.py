"""Dashboard and bot agree on control limits; integral-only int parsing."""

from __future__ import annotations

import json
import math
from types import SimpleNamespace

import pytest

from api.state import _sanitize_control
from bot import MaxwellBot
from control_defaults import DEFAULT_CONTROL, parse_int


class _Provider:
    enable_audio_input = None


def _load(tmp_path, payload):
    (tmp_path / "bot_control.json").write_text(json.dumps(payload), encoding="utf-8")
    bot = object.__new__(MaxwellBot)
    bot.config = SimpleNamespace(DATA_DIR=str(tmp_path))
    bot._control = {}
    bot._control_mtime = None
    bot._conversation_watch = {}
    bot._watch_debounce = {}
    bot.ai_provider = _Provider()
    bot._load_control(force=True)
    return bot._control


@pytest.mark.parametrize(
    "value, expected",
    [
        (30, 30),
        (0, 0),
        (-4, -4),
        (30.0, 30),
        ("30", 30),
        (" 30 ", 30),
        ("30.0", 30),
        ("-2.0", -2),
        ("1e3", 1000),
        (True, 1),
        (30.5, "d"),
        ("30.5", "d"),
        ("abc", "d"),
        ("", "d"),
        (None, "d"),
        (math.inf, "d"),
        (math.nan, "d"),
        ("inf", "d"),
        ("nan", "d"),
        ([30], "d"),
        ({"v": 30}, "d"),
    ],
)
def test_parse_int_accepts_only_integral_values(value, expected):
    sentinel = object()
    out = parse_int(value, sentinel)
    if expected == "d":
        assert out is sentinel
    else:
        assert out == expected and type(out) is int


@pytest.mark.parametrize(
    "key, raw, expected",
    [
        ("tool_history_messages", 25, 25),
        ("tool_history_messages", 30, 30),
        ("tool_history_messages", 31, 30),
        ("prompt_context_budget", 1_500_000, 1_500_000),
        ("prompt_context_budget", 2_000_000, 2_000_000),
        ("prompt_context_budget", 2_000_001, 2_000_000),
        ("prompt_context_budget", 0, DEFAULT_CONTROL["prompt_context_budget"]),
        ("prompt_context_budget", "junk", DEFAULT_CONTROL["prompt_context_budget"]),
    ],
)
def test_dashboard_uses_bot_limits_and_defaults(key, raw, expected):
    assert _sanitize_control({key: raw})[key] == expected


PARITY_VALUES = [
    0,
    1,
    -5,
    19,
    20,
    25,
    "30.0",
    30.0,
    31,
    9_999,
    250_000,
    "600000",
    600_000.0,
    10**9,
    30.5,
    "30.5",
    "junk",
    None,
]


@pytest.mark.parametrize("key", ["tool_history_messages", "prompt_context_budget"])
@pytest.mark.parametrize("raw", PARITY_VALUES)
def test_dashboard_matches_bot_runtime(tmp_path, key, raw):
    bot_value = _load(tmp_path, {key: raw})[key]
    assert _sanitize_control({key: raw})[key] == bot_value


def test_missing_keys_keep_shared_defaults_on_both_sides(tmp_path):
    bot = _load(tmp_path, {})
    api = _sanitize_control({})
    for key in ("tool_history_messages", "prompt_context_budget"):
        assert bot[key] == api[key] == DEFAULT_CONTROL[key]


@pytest.mark.parametrize(
    "key, raw, expected",
    [
        ("max_response_chars", "3000.0", 3000),
        ("max_response_chars", 3000.0, 3000),
        ("tool_history_messages", "12.0", 12),
        ("prompt_context_budget", "300000.0", 300000),
        ("live_max_output_tokens", " 8192 ", 8192),
        ("live_max_output_tokens", "8192.0", 8192),
    ],
)
def test_bot_accepts_integral_strings_and_floats(tmp_path, key, raw, expected):
    assert _load(tmp_path, {key: raw})[key] == expected


@pytest.mark.parametrize(
    "key, raw, fallback",
    [
        ("max_response_chars", 3000.5, 4000),
        ("max_response_chars", "3000.5", 4000),
        ("tool_history_messages", 12.5, 0),
        (
            "prompt_context_budget",
            "300000.25",
            DEFAULT_CONTROL["prompt_context_budget"],
        ),
        ("live_max_output_tokens", 8192.5, 4096),
        ("live_max_output_tokens", "inf", 4096),
    ],
)
def test_bot_rejects_non_integral_values(tmp_path, key, raw, fallback):
    assert _load(tmp_path, {key: raw})[key] == fallback
