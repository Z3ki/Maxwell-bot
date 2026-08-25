"""Wiring for the X feature: controls, gates, and the switches that gate it.

x_client.py is tested on its own; this covers the parts that only exist once
the client is plugged into the bot — the dashboard knobs reaching the live
client, the autonomy denial, and the taint gate agreeing with .env.
"""

from types import SimpleNamespace

import pytest

from bot import MaxwellBot
from bot_tools import _taint_gate_blocks
from control_defaults import DEFAULT_CONTROL, KNOWN_TOOLS
from tool_schemas import RESULT_TOOL_NAMES, TOOL_PARAMETERS


def _bot(control=None, **cfg):
    return SimpleNamespace(config=SimpleNamespace(**cfg), _control=control or {})


# ── control plumbing ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        (300, 300.0),
        (5, 60.0),  # floored: mentions are a conversation, not an alarm
        (99999, 3600.0),
        ("banana", 300.0),
    ],
)
def test_mention_poll_interval_is_clamped(raw, expected):
    bot = _bot({"x_mention_poll_seconds": raw})
    assert MaxwellBot._x_poll_seconds(bot) == expected


def test_control_reaches_the_live_client_and_poller():
    """Turning posting off has to work now, not after a restart."""
    client = SimpleNamespace(
        post_enabled=True,
        cache_seconds=60.0,
        budget=SimpleNamespace(per_hour=8),
    )
    poller = SimpleNamespace(interval=300.0, max_backoff=3600.0)
    bot = _bot()
    bot.x_client = client
    bot.x_mention_poller = poller
    MaxwellBot._apply_x_control(
        bot,
        {
            "x_post_enabled": False,
            "x_posts_per_hour": 3,
            "x_cache_seconds": 0,
            "x_mention_poll_seconds": 600,
        },
    )
    assert client.post_enabled is False
    assert client.budget.per_hour == 3
    assert client.cache_seconds == 0
    assert poller.interval == 600.0


def test_control_plumbing_is_a_no_op_without_the_feature():
    bot = _bot()
    bot.x_client = None
    bot.x_mention_poller = None
    MaxwellBot._apply_x_control(bot, dict(DEFAULT_CONTROL))  # must not raise


def test_the_x_tools_are_declared_everywhere_they_have_to_be():
    """A tool missing from one of these lists fails in a different way each time."""
    for name in ("x_read", "x_post"):
        assert name in KNOWN_TOOLS, f"{name} missing from KNOWN_TOOLS (dashboard)"
        assert name in TOOL_PARAMETERS, f"{name} has no parameter schema"
        assert name in RESULT_TOOL_NAMES, f"{name} would never get a follow-up turn"


# ── autonomy ──────────────────────────────────────────────────────────────


def _engine(control):
    from autonomy import AutonomyEngine

    return SimpleNamespace(
        _autonomy_tool_allowed=lambda name: AutonomyEngine._autonomy_tool_allowed(
            SimpleNamespace(bot=SimpleNamespace(_control=control)), name
        )
    )


def test_unattended_ticks_cannot_post_by_default():
    assert _engine({})._autonomy_tool_allowed("x_post") is False
    assert _engine({})._autonomy_tool_allowed("x_read") is True


def test_autonomy_posting_can_be_switched_on():
    engine = _engine({"x_autonomy_post": True})
    assert engine._autonomy_tool_allowed("x_post") is True


def test_disabled_tools_still_win_over_the_autonomy_post_switch():
    engine = _engine({"x_autonomy_post": True, "disabled_tools": ["x_post"]})
    assert engine._autonomy_tool_allowed("x_post") is False


# ── the taint gate ────────────────────────────────────────────────────────


class _Tool:
    def __init__(self, bot):
        self.bot = bot


def _tainted_bot(**cfg):
    return SimpleNamespace(
        config=SimpleNamespace(**cfg),
        is_message_tainted=lambda m: True,
    )


def test_a_tainted_turn_is_blocked():
    assert _taint_gate_blocks(_Tool(_tainted_bot()), object(), {}) is True


def test_an_out_of_band_confirm_unblocks_it():
    tool = _Tool(_tainted_bot())
    assert _taint_gate_blocks(tool, object(), {"_confirmed": True}) is False


def test_disable_taint_gate_actually_disables_the_gate():
    """The .env switch used to be read only by the dispatcher.

    Every per-tool copy kept refusing, so setting it looked broken for
    email_send, shell and x_post alike.
    """
    tool = _Tool(_tainted_bot(DISABLE_TAINT_GATE=True))
    assert _taint_gate_blocks(tool, object(), {}) is False


def test_a_clean_turn_is_not_blocked():
    bot = SimpleNamespace(config=SimpleNamespace(), is_message_tainted=lambda m: False)
    assert _taint_gate_blocks(_Tool(bot), object(), {}) is False


# ── lean chat ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "what's on twitter today",
        "did you see that tweet",
        "check your mentions",
        "look at x.com/nasa/status/123",
    ],
)
def test_asking_about_x_leaves_the_lean_tool_set(text):
    assert MaxwellBot._ACTION_TOOL_HINT_RE.search(text)


def test_ordinary_chat_still_travels_light():
    assert not MaxwellBot._ACTION_TOOL_HINT_RE.search("lol yeah exactly")
