"""`,solo` — lock one server to one channel.

The promise is narrow and absolute: in a soloed server Maxwell answers in the
chosen channel and nowhere else, and he does not start anything on his own
there. Other servers must be untouched — which is the whole reason this is a
per-guild map instead of the global allowed_channels list.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from autonomy import AutonomyEngine
from bot import MaxwellBot
from control_defaults import DEFAULT_CONTROL


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def bot(tmp_path):
    control = {
        "guild_solo_channel": {},
        "autonomy_blocked_servers": [],
        "guild_solo_autonomy_added": [],
    }
    b = SimpleNamespace(
        _control=control,
        config=SimpleNamespace(DATA_DIR=str(tmp_path)),
        _is_admin=lambda _uid: True,
    )
    b._solo_channel_for = MaxwellBot._solo_channel_for.__get__(b)
    b._solo_blocks = MaxwellBot._solo_blocks.__get__(b)
    b._save_solo = MaxwellBot._save_solo.__get__(b)
    b._handle_solo_command = MaxwellBot._handle_solo_command.__get__(b)
    return b


def _msg(channel_id="100", guild_id="1", guild_name="Test", content=""):
    sent = []
    guild = (
        SimpleNamespace(id=guild_id, name=guild_name, get_channel=lambda cid: object())
        if guild_id
        else None
    )
    return SimpleNamespace(
        content=content,
        guild=guild,
        channel=SimpleNamespace(id=channel_id, send=AsyncMock(side_effect=lambda t: sent.append(t))),
        author=SimpleNamespace(id=7, display_name="admin"),
        _sent=sent,
    )


def _last(message):
    return message.channel.send.await_args[0][0]


# ── the gate ──────────────────────────────────────────────────────────────
def test_nothing_is_blocked_before_the_lock(bot):
    assert bot._solo_blocks(_msg(channel_id="100")) is False
    assert bot._solo_blocks(_msg(channel_id="999")) is False


def test_the_lock_silences_every_other_channel(bot):
    bot._control["guild_solo_channel"] = {"1": "100"}
    assert bot._solo_blocks(_msg(channel_id="100")) is False  # the chosen one
    assert bot._solo_blocks(_msg(channel_id="101")) is True
    assert bot._solo_blocks(_msg(channel_id="999")) is True


def test_other_servers_are_untouched(bot):
    """The bug this design avoids: allowed_channels is global."""
    bot._control["guild_solo_channel"] = {"1": "100"}
    assert bot._solo_blocks(_msg(channel_id="500", guild_id="2")) is False
    assert bot._solo_blocks(_msg(channel_id="600", guild_id="3")) is False


def test_dms_are_never_blocked(bot):
    bot._control["guild_solo_channel"] = {"1": "100"}
    assert bot._solo_blocks(_msg(channel_id="42", guild_id=None)) is False


def test_a_corrupt_map_does_not_block_anything(bot):
    for junk in ("nonsense", [], None, 5):
        bot._control["guild_solo_channel"] = junk
        assert bot._solo_blocks(_msg(channel_id="101")) is False


# ── the command ───────────────────────────────────────────────────────────
def test_bare_solo_locks_to_the_current_channel(bot):
    m = _msg(channel_id="100")
    run(bot._handle_solo_command(m, None))
    assert bot._control["guild_solo_channel"] == {"1": "100"}
    assert "Locked to this channel" in _last(m)
    assert bot._solo_blocks(_msg(channel_id="101")) is True


def test_solo_accepts_a_channel_mention(bot):
    m = _msg(channel_id="100")
    run(bot._handle_solo_command(m, "<#222333444555>"))
    assert bot._control["guild_solo_channel"] == {"1": "222333444555"}
    assert "<#222333444555>" in _last(m)


def test_solo_refuses_a_channel_from_another_server(bot):
    m = _msg(channel_id="100")
    m.guild.get_channel = lambda cid: None
    run(bot._handle_solo_command(m, "<#999888777666>"))
    assert bot._control["guild_solo_channel"] == {}
    assert "isn't in this server" in _last(m)


def test_solo_rejects_nonsense_with_usage(bot):
    m = _msg()
    run(bot._handle_solo_command(m, "the general one"))
    assert bot._control["guild_solo_channel"] == {}
    assert "Usage:" in _last(m)


def test_solo_off_restores_the_server(bot):
    m = _msg(channel_id="100")
    run(bot._handle_solo_command(m, None))
    run(bot._handle_solo_command(m, "off"))
    assert bot._control["guild_solo_channel"] == {}
    assert bot._solo_blocks(_msg(channel_id="101")) is False
    assert "Unlocked" in _last(m)


def test_solo_off_when_not_locked_says_so(bot):
    m = _msg()
    run(bot._handle_solo_command(m, "off"))
    assert "Wasn't locked" in _last(m)


def test_status_reports_both_states(bot):
    m = _msg(channel_id="100")
    run(bot._handle_solo_command(m, "status"))
    assert "Not locked" in _last(m)
    run(bot._handle_solo_command(m, None))
    run(bot._handle_solo_command(m, "status"))
    assert "<#100>" in _last(m)


def test_solo_outside_a_server_is_refused(bot):
    m = _msg(guild_id=None)
    run(bot._handle_solo_command(m, None))
    assert "only makes sense in a server" in _last(m)


def test_locking_one_server_leaves_another_lock_alone(bot):
    run(bot._handle_solo_command(_msg(channel_id="100", guild_id="1"), None))
    run(bot._handle_solo_command(_msg(channel_id="500", guild_id="2"), None))
    assert bot._control["guild_solo_channel"] == {"1": "100", "2": "500"}
    run(bot._handle_solo_command(_msg(channel_id="500", guild_id="2"), "off"))
    assert bot._control["guild_solo_channel"] == {"1": "100"}


# ── autonomy ──────────────────────────────────────────────────────────────
def test_locking_a_server_stops_autonomy_there(bot):
    run(bot._handle_solo_command(_msg(channel_id="100"), None))
    assert bot._control["autonomy_blocked_servers"] == ["1"]


def test_unlocking_gives_autonomy_back(bot):
    m = _msg(channel_id="100")
    run(bot._handle_solo_command(m, None))
    run(bot._handle_solo_command(m, "off"))
    assert bot._control["autonomy_blocked_servers"] == []


def test_unlocking_keeps_a_blacklist_the_admin_set_by_hand(bot):
    """`,solo off` must not hand autonomy back a server someone else silenced."""
    bot._control["autonomy_blocked_servers"] = ["2", "3"]
    m = _msg(channel_id="100", guild_id="2")
    run(bot._handle_solo_command(m, None))
    run(bot._handle_solo_command(m, "off"))
    # Solo did not take server 2's autonomy away, so it does not give it back.
    assert set(bot._control["autonomy_blocked_servers"]) == {"2", "3"}


def test_unlocking_gives_back_only_what_solo_took(bot):
    bot._control["autonomy_blocked_servers"] = ["3"]
    m = _msg(channel_id="100", guild_id="1")
    run(bot._handle_solo_command(m, None))
    assert set(bot._control["autonomy_blocked_servers"]) == {"3", "1"}
    run(bot._handle_solo_command(m, "off"))
    assert bot._control["autonomy_blocked_servers"] == ["3"]
    assert bot._control["guild_solo_autonomy_added"] == []


def _engine(control, channel_guild_id="1"):
    engine = object.__new__(AutonomyEngine)
    engine.bot = SimpleNamespace(
        _control=control,
        get_channel=lambda cid: SimpleNamespace(
            guild=SimpleNamespace(id=channel_guild_id)
        ),
    )
    return engine


def test_autonomy_obeys_the_lock_even_without_the_blacklist(bot):
    """Belt and braces: the lock holds if the two settings drift apart."""
    control = dict(DEFAULT_CONTROL)
    control["guild_solo_channel"] = {"1": "100"}
    control["autonomy_blocked_servers"] = []  # someone cleared it by hand
    engine = _engine(control)
    assert engine._channel_allowed("101") is False
    assert engine._channel_allowed("100") is True


def test_autonomy_is_unaffected_in_other_servers(bot):
    control = dict(DEFAULT_CONTROL)
    control["guild_solo_channel"] = {"1": "100"}
    engine = _engine(control, channel_guild_id="2")
    assert engine._channel_allowed("500") is True
