"""Control sanitizer coverage for the dashboard's Controls panel.

The admin dashboard renders one input per DEFAULT_CONTROL key with a min/max
that mirrors the clamp in api.state._sanitize_control. These tests pin the two
properties that pairing depends on: every key survives a round trip, and a
legitimate 0 is not mistaken for "unset".
"""

import pytest

from api.state import _sanitize_control
from control_defaults import DEFAULT_CONTROL


def test_every_default_key_survives_sanitize():
    out = _sanitize_control({})
    missing = [k for k in DEFAULT_CONTROL if k not in out]
    assert not missing, f"sanitizer dropped keys the dashboard renders: {missing}"


def test_defaults_are_their_own_fixed_point():
    """Sanitizing the defaults must not rewrite them.

    A default outside its own clamp means the dashboard would show one value
    and the bot would run another.
    """
    out = _sanitize_control(dict(DEFAULT_CONTROL))
    drifted = {
        k: (DEFAULT_CONTROL[k], out[k])
        for k in DEFAULT_CONTROL
        if out[k] != DEFAULT_CONTROL[k]
    }
    assert not drifted, f"default is outside its own clamp: {drifted}"


@pytest.mark.parametrize(
    "key",
    [
        # control_defaults documents 0 as meaningful for each of these.
        "conversation_watch_seconds",  # 0 disables the follow-up watch
        "site_ttl_hours",  # 0 = never expire
        "autonomy_recent_reply_block_seconds",  # 0 = defer to the floor cooldown
        "autonomy_floor_cooldown_seconds",
        "autonomy_floor_mid_flow_seconds",
        "max_tool_iterations",
        "tool_history_messages",
        "memory_history_messages",
    ],
)
def test_zero_is_honored_not_treated_as_unset(key):
    """`out.get(k) or default` used to swap a real 0 for the default."""
    assert _sanitize_control({key: 0})[key] == 0


def test_zero_is_honored_for_float_keys():
    out = _sanitize_control(
        {"cross_context_extract_threshold": 0, "vc_preroll_seconds": 0}
    )
    assert out["cross_context_extract_threshold"] == 0.0
    assert out["vc_preroll_seconds"] == 0.0


def test_float_shaped_ints_are_accepted():
    """Dashboard number inputs hand back strings; "5.0" must not fall back."""
    out = _sanitize_control({"ai_concurrency": "5.0", "max_tool_iterations": "12"})
    assert out["ai_concurrency"] == 5
    assert out["max_tool_iterations"] == 12


def test_out_of_range_is_clamped_not_defaulted():
    out = _sanitize_control({"ai_concurrency": 999, "max_tool_iterations": -5})
    assert out["ai_concurrency"] == 10
    assert out["max_tool_iterations"] == 0


@pytest.mark.parametrize(
    "key,good,bad,fallback",
    [
        ("vc_tts_engine", "espeak", "bogus", "fish"),
        ("vc_reply_mode", "both", "telepathy", "voice"),
        ("vc_response_mode", "addressed", "sometimes", "always"),
    ],
)
def test_voice_enums_reject_unknown_values(key, good, bad, fallback):
    assert _sanitize_control({key: good})[key] == good
    assert _sanitize_control({key: bad})[key] == fallback
