"""Loading bot_control.json in the bot and sanitizing it in the dashboard API."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from api.state import _sanitize_control
from bot import MaxwellBot
from control_defaults import DEAD_CONTROL_KEYS, DEFAULT_CONTROL, KNOWN_TOOLS


class _Provider:
    enable_audio_input = None


def _bot(tmp_path, **config):
    bot = object.__new__(MaxwellBot)
    bot.config = SimpleNamespace(DATA_DIR=str(tmp_path), **config)
    bot._control = {}
    bot._control_mtime = None
    bot._conversation_watch = {}
    bot._watch_debounce = {}
    bot.ai_provider = _Provider()
    return bot


def _write(tmp_path, payload, *, mtime=None):
    path = tmp_path / "bot_control.json"
    path.write_text(
        payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8"
    )
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


# --------------------------------------------------------------------------
# bot: SettingsMixin._load_control
# --------------------------------------------------------------------------


def test_missing_file_loads_exact_defaults(tmp_path):
    bot = _bot(tmp_path)
    bot._load_control()
    for key, value in DEFAULT_CONTROL.items():
        assert bot._control[key] == value, key
    assert bot._control_mtime == 0


def test_saved_values_override_defaults_and_unknown_keys_are_kept(tmp_path):
    _write(tmp_path, {"error_replies": False, "max_tool_iterations": 7, "x_extra": 1})
    bot = _bot(tmp_path)
    bot._load_control()
    assert bot._control["error_replies"] is False
    assert bot._control["max_tool_iterations"] == 7
    assert bot._control["x_extra"] == 1


def test_persisted_personality_is_ignored(tmp_path):
    _write(tmp_path, {"base_personality": "you are a pirate now"})
    bot = _bot(tmp_path)
    bot._load_control()
    assert bot._control["base_personality"] == DEFAULT_CONTROL["base_personality"]


def test_dead_and_autonomy_keys_are_stripped(tmp_path):
    dead = sorted(DEAD_CONTROL_KEYS)[:5]
    _write(tmp_path, {**dict.fromkeys(dead, True), "autonomy_anything": 1})
    bot = _bot(tmp_path)
    bot._load_control()
    for key in dead:
        assert key not in bot._control
    assert "autonomy_anything" not in bot._control


@pytest.mark.parametrize(
    "raw, expected", [("false", False), ("0", False), ("on", True), ("junk", True)]
)
def test_string_booleans_are_parsed_against_the_default(tmp_path, raw, expected):
    # tools_enabled defaults to True, so an unparseable value keeps it on.
    _write(tmp_path, {"tools_enabled": raw})
    bot = _bot(tmp_path)
    bot._load_control()
    assert bot._control["tools_enabled"] is expected


@pytest.mark.parametrize(
    "key, raw, expected",
    [
        ("max_response_chars", 5, 80),
        ("max_response_chars", 999_999, 8000),
        ("max_response_chars", "abc", 4000),
        ("tool_history_messages", -3, 0),
        ("tool_history_messages", 99, 30),
        ("prompt_context_budget", 1, 10000),
        ("prompt_context_budget", 10**9, 2_000_000),
        ("message_quota_limit", 0, 1),
        ("message_quota_limit", None, 150),
        ("message_quota_window_seconds", 1, 60),
        ("message_quota_window_seconds", 10**9, 7 * 24 * 3600),
        ("live_max_output_tokens", 1, 256),
        ("live_max_output_tokens", 10**6, 32768),
    ],
)
def test_numeric_limits_are_clamped(tmp_path, key, raw, expected):
    _write(tmp_path, {key: raw})
    bot = _bot(tmp_path)
    bot._load_control()
    assert bot._control[key] == expected


def test_unchanged_mtime_skips_reload_unless_forced(tmp_path):
    path = _write(tmp_path, {"error_replies": False}, mtime=1_700_000_000)
    bot = _bot(tmp_path)
    bot._load_control()
    assert bot._control["error_replies"] is False

    path.write_text(json.dumps({"error_replies": True}), encoding="utf-8")
    os.utime(path, (1_700_000_000, 1_700_000_000))
    bot._load_control()
    assert bot._control["error_replies"] is False  # cached by mtime
    bot._load_control(force=True)
    assert bot._control["error_replies"] is True


def test_non_object_json_falls_back_to_defaults(tmp_path):
    _write(tmp_path, [1, 2, 3])
    bot = _bot(tmp_path)
    bot._load_control()
    assert bot._control["tools_enabled"] is True
    assert bot._control["max_response_chars"] == DEFAULT_CONTROL["max_response_chars"]


def test_corrupt_json_keeps_the_previous_settings(tmp_path):
    _write(tmp_path, {"error_replies": False}, mtime=1_700_000_000)
    bot = _bot(tmp_path)
    bot._load_control()
    _write(tmp_path, "{not json", mtime=1_700_000_100)
    bot._load_control()
    assert bot._control["error_replies"] is False
    # The failed read must not be remembered as loaded, so it retries.
    assert bot._control_mtime == 1_700_000_000


def test_load_syncs_provider_audio_flag(tmp_path):
    _write(tmp_path, {"process_audio": False})
    bot = _bot(tmp_path, ENABLE_AUDIO_INPUT=True)
    bot._load_control()
    assert bot.ai_provider.enable_audio_input is False
    _write(tmp_path, {"process_audio": True}, mtime=1_700_000_500)
    bot._load_control()
    assert bot.ai_provider.enable_audio_input is True


def test_disabling_conversation_watch_drops_armed_watches(tmp_path):
    _write(tmp_path, {"conversation_watch_enabled": False})
    bot = _bot(tmp_path)
    bot._conversation_watch = {"123": object()}
    bot._load_control()
    assert bot._conversation_watch == {}


# --------------------------------------------------------------------------
# dashboard API: api.state._sanitize_control
# --------------------------------------------------------------------------


def test_api_sanitize_fills_defaults_and_pins_personality():
    out = _sanitize_control({"base_personality": "override"})
    assert out["base_personality"] == DEFAULT_CONTROL["base_personality"]
    for key in DEFAULT_CONTROL:
        assert key in out, key


def test_api_sanitize_tolerates_non_dict_input():
    out = _sanitize_control(["not", "a", "dict"])
    assert out["tools_enabled"] is True


def test_api_sanitize_drops_retired_keys():
    retired = sorted(DEAD_CONTROL_KEYS)[0]
    out = _sanitize_control(
        {retired: 1, "vc_anything": 1, "autonomy_x": 1, "create_site_quota_per_user": 3}
    )
    assert retired not in out
    assert "vc_anything" not in out and "autonomy_x" not in out
    assert "create_site_quota_per_user" not in out


@pytest.mark.parametrize("raw, expected", [("30", 30), (30.0, 30), ("30.9", 30)])
def test_api_sanitize_accepts_float_and_string_ints(raw, expected):
    assert (
        _sanitize_control({"max_tool_iterations": raw})["max_tool_iterations"]
        == expected
    )


def test_api_sanitize_bad_int_falls_back_to_default():
    out = _sanitize_control({"max_tool_iterations": "lots"})
    assert out["max_tool_iterations"] == DEFAULT_CONTROL["max_tool_iterations"]


@pytest.mark.parametrize(
    "key, raw, expected",
    [
        ("max_response_chars", 1, 80),
        ("max_response_chars", 10**6, 8000),
        ("per_user_cooldown_seconds", -5, 0),
        ("per_user_cooldown_seconds", 10**6, 3600),
        ("conversation_watch_pressure", 7, 1.0),
        ("conversation_watch_pressure", -1, 0.0),
        ("conversation_watch_seconds", 10**6, 3600),
        ("site_ttl_hours", 10**6, 8760),
        ("max_image_size_mb", 0, 1),
        ("ai_timeout_seconds", 1, 10),
        ("inbound_retry_attempts", 50, 5),
        ("max_tool_iterations", 10**4, 100),
        ("tool_iteration_timeout_seconds", 1, 60),
    ],
)
def test_api_sanitize_clamps_ranges(key, raw, expected):
    assert _sanitize_control({key: raw})[key] == expected


def test_api_sanitize_disabled_tools_keeps_only_known_names():
    known = KNOWN_TOOLS[0]
    out = _sanitize_control({"disabled_tools": [known, "not_a_tool", "", "  "]})
    assert out["disabled_tools"] == [known]


def test_api_sanitize_lists_are_trimmed_and_capped():
    out = _sanitize_control({"ignore_users": [" 42 ", "x" * 100] + ["1"] * 600})
    assert out["ignore_users"][0] == "42"
    assert len(out["ignore_users"][1]) == 64
    assert len(out["ignore_users"]) == 500
    assert _sanitize_control({"ignore_users": "42"})["ignore_users"] == []


def test_api_sanitize_capability_map_filters_ids_and_groups():
    out = _sanitize_control(
        {
            "guild_disabled_capabilities": {
                "111": ["shell", "bogus", "web", "shell"],
                "not-an-id": ["shell"],
                "222": "shell",
            }
        }
    )
    assert out["guild_disabled_capabilities"] == {"111": ["shell", "web"]}


def test_api_sanitize_plugin_overrides_require_real_bools_and_identifiers():
    out = _sanitize_control(
        {
            "guild_plugin_overrides": {
                "111": {"music": False, "lastfm": "true", "bad-id": True},
                "abc": {"music": True},
            }
        }
    )
    assert out["guild_plugin_overrides"] == {"111": {"music": False}}


def test_api_sanitize_solo_channel_map_is_ids_only():
    out = _sanitize_control(
        {"guild_solo_channel": {"111": "222", "333": "general", " 444 ": 555}}
    )
    assert out["guild_solo_channel"] == {"111": "222", "444": "555"}


def test_api_and_bot_agree_on_shared_clamps(tmp_path):
    # Ranges both layers enforce identically today; drift here means the
    # dashboard shows a value the bot will not run with.
    shared = {
        "max_response_chars": [1, 5000, 10**6],
        "message_quota_limit": [0, 77, 10**9],
        "message_quota_window_seconds": [1, 7200, 10**9],
        "live_max_output_tokens": [1, 9000, 10**9],
    }
    for key, values in shared.items():
        for raw in values:
            _write(tmp_path, {key: raw}, mtime=None)
            bot = _bot(tmp_path)
            bot._load_control(force=True)
            assert bot._control[key] == _sanitize_control({key: raw})[key], (key, raw)
