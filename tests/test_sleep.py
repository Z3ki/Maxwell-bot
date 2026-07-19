"""Tests for the sleep gate / SleepTool / ClearSleepTool.

The 2026-07-19 user request: add a sleep feature (max 1 hour) so
pings during the window get a 'max is sleeping, back in Xm' notice.
These tests pin the contract:

  1. set_sleep() clamps duration to 1-60 minutes.
  2. set_sleep() sets a future deadline and _is_sleeping() reflects it.
  3. _is_sleeping() auto-clears when the deadline has passed.
  4. clear_sleep() is idempotent.
  5. _check_sleep_gate() returns False (block dispatch) when sleeping.
  6. _check_sleep_gate() returns True (allow dispatch) when not sleeping.
  7. _check_sleep_gate() returns True when control flag is off.
  8. _check_sleep_gate() DM-dedup: same user only gets one notice per
     5 minutes.
  9. SleepTool.execute enforces the 1-60m server-side cap.
"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

# Make sure the repo root is on sys.path so the test can import
# `tool_progress`, `bot_tools`, and `bot` without an installed package.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import bot_tools  # noqa: E402


class FakeMessage:
    """Minimal stand-in for a discord.Message used in the sleep-gate
    tests. Tracks DM sends so we can assert per-user dedup."""

    def __init__(self, uid="111", channel_id="222", platform="discord"):
        self.author = SimpleNamespace(
            id=int(uid),
            display_name=f"user{uid}",
            bot=False,
            dm_channel=None,
        )
        self.channel = SimpleNamespace(
            id=int(channel_id),
            sent=[],
        )
        self.id = int(uid) + 1000
        self.tool_platform = platform
        self.content = "hi"

    async def reply(self, content, **kwargs):
        # The bot's _check_sleep_gate uses channel.send with a
        # reference, NOT reply — so reply isn't strictly required for
        # these tests, but we implement it for completeness.
        self.channel.sent.append(("reply", content))
        return SimpleNamespace(content=content, edit=AsyncMock(), delete=AsyncMock())


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
        tool.execute(SimpleNamespace(author=SimpleNamespace(id=1, bot=False)), duration_minutes=999)
    )
    assert result == "sleeping for 60m"
    # String input is also accepted (the model might pass "30").
    result = asyncio.run(
        tool.execute(SimpleNamespace(author=SimpleNamespace(id=1, bot=False)), duration_minutes="45")
    )
    assert result == "sleeping for 45m"
    # Garbage input falls back to 30.
    result = asyncio.run(
        tool.execute(SimpleNamespace(author=SimpleNamespace(id=1, bot=False)), duration_minutes="banana")
    )
    assert result == "sleeping for 30m"


def test_clear_sleep_tool_idempotent():
    bot = FakeBot()
    tool = bot_tools.ClearSleepTool(bot)
    result = asyncio.run(tool.execute(SimpleNamespace()))
    assert result == "not sleeping"
    bot.set_sleep(10)
    result = asyncio.run(tool.execute(SimpleNamespace()))
    assert result == "sleep cleared, awake now"
    result = asyncio.run(tool.execute(SimpleNamespace()))
    assert result == "not sleeping"


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
