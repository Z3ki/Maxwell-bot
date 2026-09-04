"""Tests for the sleep gate / SleepTool / ClearSleepTool.

The 2026-07-19 user request: add a sleep feature (max 1 hour) so
pings during the window get a 'max is sleeping, back in Xm' notice
in the triggering channel (never a DM).
These tests pin the contract:

  1. set_sleep() clamps duration to 1-60 minutes.
  2. set_sleep() sets a future deadline and _is_sleeping() reflects it.
  3. _is_sleeping() auto-clears when the deadline has passed.
  4. clear_sleep() is idempotent.
  5. _check_sleep_gate() returns False (block dispatch) when sleeping.
  6. _check_sleep_gate() returns True (allow dispatch) when not sleeping.
  7. _check_sleep_gate() returns True when control flag is off.
  8. _check_sleep_gate() channel-dedup: same user only gets one notice
     per 5 minutes; create_dm is never called.
  9. SleepTool.execute enforces the 1-60m server-side cap.
 10. Sleep start sets Discord presence to idle; sleep end restores
     _current_status. A change_presence during sleep stays idle on
     Discord but is kept for restore.
"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

# Make sure the repo root is on sys.path so the test can import
# `tool_progress`, `bot_tools`, and `bot` without an installed package.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import bot_tools  # noqa: E402


_BOT_USER = SimpleNamespace(id=777, display_name="Maxwell", name="maxwell")


class FakeMessage:
    """Minimal stand-in for a discord.Message used in the sleep-gate
    tests. Tracks channel sends and would-be DMs so we can assert
    per-user channel dedup and that create_dm is never used."""

    def __init__(self, uid="111", channel_id="222", platform="discord"):
        self.author = SimpleNamespace(
            id=int(uid),
            display_name=f"user{uid}",
            bot=False,
            dm_channel=SimpleNamespace(send=AsyncMock()),
            create_dm=AsyncMock(),
        )
        sent: list = []

        async def send(content, **kwargs):
            sent.append(content)

        self.channel = SimpleNamespace(
            id=int(channel_id),
            sent=sent,
            send=send,
        )
        self.id = int(uid) + 1000
        self.tool_platform = platform
        self.content = "hi"
        # A hard ping by default: the sleeping notice is only for people who
        # actually addressed him.
        self.mentions = [_BOT_USER]
        self.mention_everyone = False
        self.role_mentions = []
        self.reference = None
        self.guild = SimpleNamespace(me=None, get_member=lambda _uid: None)


class FakeBot:
    """Minimal bot-shaped object exposing the sleep-state API the
    tests need: _is_sleeping / set_sleep / clear_sleep /
    _check_sleep_gate / _format_sleep_remaining. We DON'T pull the
    full MaxwellBot class in (it requires a Discord client + config
    pipeline) — we test the *contract* by calling the methods that
    the SleepTool / clear_sleep tool will hit at runtime.

    For the gate-level tests (which need a real `asyncio.Lock`-free
    bot-shaped object) we instantiate MaxwellBot... no. The gate
    uses real asyncio + a real time source. We mirror the gate logic
    with a smaller helper to keep the tests fast and dependency-free.
    """

    def __init__(self):
        self._sleep_until = 0.0
        self._sleep_notified_at = {}

    def _is_admin(self, user_id) -> bool:
        return True

    def _now(self):
        # The real MaxwellBot uses asyncio.get_running_loop().time() so
        # the test mirror here uses time.monotonic() — same epoch,
        # no event-loop dependency.
        import time

        return time.monotonic()

    def _is_sleeping(self):
        if self._sleep_until <= 0:
            return False, 0
        if self._now() >= self._sleep_until:
            self._sleep_until = 0.0
            self._sleep_notified_at.clear()
            return False, 0
        return True, int(self._sleep_until - self._now())

    def set_sleep(self, minutes):
        if minutes < 1:
            minutes = 1
        if minutes > 60:
            minutes = 60
        self._sleep_until = self._now() + minutes * 60
        self._sleep_notified_at.clear()
        return f"sleeping for {minutes}m"

    def clear_sleep(self):
        if self._sleep_until <= 0:
            return "not sleeping"
        self._sleep_until = 0.0
        self._sleep_notified_at.clear()
        return "sleep cleared, awake now"


# ---- set_sleep / clear_sleep / _is_sleeping ----


def test_set_sleep_clamps_to_1_60():
    bot = FakeBot()
    assert bot.set_sleep(0) == "sleeping for 1m"
    assert bot.set_sleep(-5) == "sleeping for 1m"
    assert bot.set_sleep(120) == "sleeping for 60m"
    assert bot.set_sleep(45) == "sleeping for 45m"


def test_set_sleep_sets_deadline_and_is_sleeping_reflects_it():
    bot = FakeBot()
    bot.set_sleep(5)
    sleeping, secs = bot._is_sleeping()
    assert sleeping is True
    # 5 minutes in seconds with a 2-second scheduling tolerance.
    assert 290 <= secs <= 300


def test_clear_sleep_is_idempotent():
    bot = FakeBot()
    assert bot.clear_sleep() == "not sleeping"
    bot.set_sleep(10)
    assert bot.clear_sleep() == "sleep cleared, awake now"
    assert bot.clear_sleep() == "not sleeping"
    sleeping, _ = bot._is_sleeping()
    assert sleeping is False


def test_sleep_clears_dedup_dict():
    bot = FakeBot()
    bot._sleep_notified_at["123"] = 12345.0
    bot.set_sleep(10)
    assert bot._sleep_notified_at == {}


def test_is_sleeping_auto_clears_expired_state():
    bot = FakeBot()
    # Force a past deadline.
    bot._sleep_until = bot._now() - 1
    bot._sleep_notified_at["x"] = 1.0
    sleeping, secs = bot._is_sleeping()
    assert sleeping is False
    assert secs == 0
    assert bot._sleep_until == 0.0
    assert bot._sleep_notified_at == {}


# ---- SleepTool / ClearSleepTool ----


def test_sleep_tool_clamps_to_60_minutes():
    bot = FakeBot()
    tool = bot_tools.SleepTool(bot)
    # Out-of-range value: the tool clamps server-side.
    result = asyncio.run(
        tool.execute(
            SimpleNamespace(author=SimpleNamespace(id=1, bot=False)),
            duration_minutes=999,
        )
    )
    assert result == "sleeping for 60m"
    # String input is also accepted (the model might pass "30").
    result = asyncio.run(
        tool.execute(
            SimpleNamespace(author=SimpleNamespace(id=1, bot=False)),
            duration_minutes="45",
        )
    )
    assert result == "sleeping for 45m"
    # Garbage input falls back to 30.
    result = asyncio.run(
        tool.execute(
            SimpleNamespace(author=SimpleNamespace(id=1, bot=False)),
            duration_minutes="banana",
        )
    )
    assert result == "sleeping for 30m"


def _admin_msg():
    return SimpleNamespace(author=SimpleNamespace(id=1, bot=False))


def test_clear_sleep_tool_idempotent():
    bot = FakeBot()
    tool = bot_tools.ClearSleepTool(bot)
    result = asyncio.run(tool.execute(_admin_msg()))
    assert result == "not sleeping"
    bot.set_sleep(10)
    result = asyncio.run(tool.execute(_admin_msg()))
    assert result == "sleep cleared, awake now"
    result = asyncio.run(tool.execute(_admin_msg()))
    assert result == "not sleeping"


def test_sleep_tools_allow_non_admin():
    bot = FakeBot()
    bot._is_admin = lambda _uid: False
    msg = SimpleNamespace(author=SimpleNamespace(id=99, bot=False))
    assert asyncio.run(bot_tools.SleepTool(bot).execute(msg, duration_minutes=5)) == "sleeping for 5m"
    assert asyncio.run(bot_tools.ClearSleepTool(bot).execute(msg)) == "sleep cleared, awake now"


# ---- SleepTool integration with real bot's sleep helpers ----
# The real `MaxwellBot.set_sleep` clamps to 60 and the real
# `_is_sleeping` returns the right tuple. We verify by spinning up
# a barebones bot without Discord: a tiny shim that mimics the
# asyncio.get_running_loop().time() source. (The real bot uses
# asyncio.get_running_loop().time() in set_sleep/clear_sleep.)


def test_sleep_tool_with_real_bot_helpers():
    """End-to-end: SleepTool.set_sleep() interacts correctly with the
    FakeBot-shaped object that mirrors MaxwellBot's sleep API."""
    bot = FakeBot()
    tool = bot_tools.SleepTool(bot)
    asyncio.run(
        tool.execute(
            SimpleNamespace(author=SimpleNamespace(id=1, bot=False)),
            duration_minutes=30,
        )
    )
    sleeping, _ = bot._is_sleeping()
    assert sleeping is True


def _gate_bot():
    from bot import MaxwellBot

    bot = SimpleNamespace(
        _control={"enable_sleep": True},
        _sleep_until=0.0,
        _sleep_notified_at={},
        _conversation_watch={},
        _watch_debounce={},
        user=_BOT_USER,
    )
    bot._directly_addressed = MaxwellBot._directly_addressed.__get__(bot)
    bot._cancel_watch_debounce = MaxwellBot._cancel_watch_debounce.__get__(bot)
    bot._drop_watches_for_sleep = MaxwellBot._drop_watches_for_sleep.__get__(bot)
    bot._is_sleeping = MaxwellBot._is_sleeping.__get__(bot)
    bot.set_sleep = MaxwellBot.set_sleep.__get__(bot)
    bot._format_sleep_remaining = MaxwellBot._format_sleep_remaining.__get__(bot)
    bot._check_sleep_gate = MaxwellBot._check_sleep_gate.__get__(bot)
    return bot


def test_sleep_gate_posts_in_channel_not_dm():
    async def scenario():
        bot = _gate_bot()
        await bot.set_sleep(10)
        msg = FakeMessage()
        proceed = await bot._check_sleep_gate(msg)
        assert proceed is False
        assert len(msg.channel.sent) == 1
        assert "sleeping" in msg.channel.sent[0]
        assert "back in" in msg.channel.sent[0]
        msg.author.create_dm.assert_not_called()
        msg.author.dm_channel.send.assert_not_called()
        proceed2 = await bot._check_sleep_gate(msg)
        assert proceed2 is False
        assert len(msg.channel.sent) == 1

    asyncio.run(scenario())


def test_sleep_gate_is_silent_for_lines_nobody_sent_him():
    """Typing in a room he was in is not a ping — he stays asleep, and quiet.

    The watch keeps a whole room live after he speaks, so an ordinary line in
    that room used to reach the gate and get answered with "max is sleeping":
    a bot talking in his sleep to someone who never asked him anything.
    """

    async def scenario():
        bot = _gate_bot()
        await bot.set_sleep(10)
        ambient = FakeMessage()
        ambient.mentions = []
        assert await bot._check_sleep_gate(ambient) is False
        assert ambient.channel.sent == []
        # And a real ping still gets the one notice.
        ping = FakeMessage()
        assert await bot._check_sleep_gate(ping) is False
        assert len(ping.channel.sent) == 1

    asyncio.run(scenario())


def test_sleep_drops_armed_watches():
    """Going to sleep forgets every room it was watching."""

    async def scenario():
        bot = _gate_bot()
        bot._conversation_watch["222"] = 1e9
        bot._watch_debounce["222"] = {"task": None}
        await bot.set_sleep(10)
        assert bot._conversation_watch == {}
        assert bot._watch_debounce == {}

    asyncio.run(scenario())


def test_sleep_gate_allows_when_awake():
    async def scenario():
        bot = _gate_bot()
        msg = FakeMessage()
        assert await bot._check_sleep_gate(msg) is True
        assert msg.channel.sent == []

    asyncio.run(scenario())


def test_sleep_gate_disabled_by_control_flag():
    async def scenario():
        bot = _gate_bot()
        await bot.set_sleep(10)
        bot._control["enable_sleep"] = False
        msg = FakeMessage()
        assert await bot._check_sleep_gate(msg) is True
        assert msg.channel.sent == []

    asyncio.run(scenario())


def _presence_bot():
    from bot import MaxwellBot

    bot = SimpleNamespace(
        _control={"enable_sleep": True},
        _sleep_until=0.0,
        _sleep_notified_at={},
        _current_status=discord.Status.online,
        _custom_status=None,
        _current_game=None,
        _sleep_wake_task=None,
        _sleep_presence_overlay=False,
    )
    bot._push_presence = AsyncMock()
    bot._build_activities = MaxwellBot._build_activities.__get__(bot)
    bot._sleep_window_active = MaxwellBot._sleep_window_active.__get__(bot)
    bot.change_presence = MaxwellBot.change_presence.__get__(bot)
    bot._apply_sleep_presence = MaxwellBot._apply_sleep_presence.__get__(bot)
    bot._schedule_sleep_presence = MaxwellBot._schedule_sleep_presence.__get__(bot)
    bot._arm_sleep_wake = MaxwellBot._arm_sleep_wake.__get__(bot)
    bot._cancel_sleep_wake = MaxwellBot._cancel_sleep_wake.__get__(bot)
    bot._sleep_wake_when_due = MaxwellBot._sleep_wake_when_due.__get__(bot)
    bot.set_sleep = MaxwellBot.set_sleep.__get__(bot)
    bot.clear_sleep = MaxwellBot.clear_sleep.__get__(bot)
    bot._is_sleeping = MaxwellBot._is_sleeping.__get__(bot)
    return bot


def _last_status(bot):
    assert bot._push_presence.await_args_list, "expected a presence push"
    return bot._push_presence.await_args.kwargs["status"]


def test_sleep_sets_idle_and_clear_restores_status():
    async def scenario():
        bot = _presence_bot()
        try:
            assert await bot.set_sleep(10) == "sleeping for 10m"
            assert bot._current_status is discord.Status.online
            assert _last_status(bot) is discord.Status.idle
            assert await bot.clear_sleep() == "sleep cleared, awake now"
            assert _last_status(bot) is discord.Status.online
        finally:
            bot._cancel_sleep_wake()

    asyncio.run(scenario())


def test_sleep_restores_pre_sleep_idle():
    async def scenario():
        bot = _presence_bot()
        bot._current_status = discord.Status.idle
        try:
            await bot.set_sleep(5)
            assert _last_status(bot) is discord.Status.idle
            await bot.clear_sleep()
            assert _last_status(bot) is discord.Status.idle
        finally:
            bot._cancel_sleep_wake()

    asyncio.run(scenario())


def test_sleep_idle_wins_but_keeps_presence_change_for_restore():
    async def scenario():
        bot = _presence_bot()
        try:
            await bot.set_sleep(10)
            bot._push_presence.reset_mock()
            await bot.change_presence(status=discord.Status.dnd)
            assert bot._current_status is discord.Status.dnd
            assert _last_status(bot) is discord.Status.idle
            await bot.clear_sleep()
            assert _last_status(bot) is discord.Status.dnd
        finally:
            bot._cancel_sleep_wake()

    asyncio.run(scenario())


def test_sleep_expiry_restores_presence():
    async def scenario():
        bot = _presence_bot()
        try:
            await bot.set_sleep(10)
            bot._sleep_until = asyncio.get_running_loop().time() - 1
            sleeping, _ = bot._is_sleeping()
            assert sleeping is False
            await asyncio.sleep(0)
            assert _last_status(bot) is discord.Status.online
        finally:
            bot._cancel_sleep_wake()

    asyncio.run(scenario())


def test_sleep_gate_notifies_right_after_boot():
    """``loop.time()`` is monotonic and starts near zero at boot.

    The dedup map used ``0.0`` as its "never notified" default, so on a host
    that had been up for less than five minutes ``now - 0.0 < 300`` held for
    every first ping and the notice was silently swallowed. A bot started by
    systemd/Docker at boot hit this on every restart.
    """

    async def scenario():
        bot = _gate_bot()
        loop = asyncio.get_running_loop()
        real_time = loop.time
        base = real_time()
        # Pretend the process is 12 seconds past boot.
        loop.time = lambda: real_time() - base + 12.0
        try:
            await bot.set_sleep(10)
            msg = FakeMessage()
            proceed = await bot._check_sleep_gate(msg)
            assert proceed is False
            assert len(msg.channel.sent) == 1
            assert "sleeping" in msg.channel.sent[0]
            # Second ping inside the window stays deduped.
            proceed2 = await bot._check_sleep_gate(msg)
            assert proceed2 is False
            assert len(msg.channel.sent) == 1
        finally:
            loop.time = real_time

    asyncio.run(scenario())
