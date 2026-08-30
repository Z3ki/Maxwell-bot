"""Tests for the nightly fallback-model routing."""

import asyncio
from types import SimpleNamespace

import bot as bot_mod
from bot import MaxwellBot


class _FakeProvider:
    def __init__(self):
        self.kwargs = None

    async def generate_response(self, messages, **kwargs):
        self.kwargs = kwargs
        return "ok"


def _make_bot():
    bot = MaxwellBot.__new__(MaxwellBot)
    bot._control = {
        "enable_night_fallback": True,
        "night_fallback_start_hour": 22,
        "night_fallback_end_hour": 9,
    }
    bot.config = SimpleNamespace(
        OLLAMA_FALLBACK_BASE_URL="https://fallback.example/v1",
        OLLAMA_FALLBACK_MODEL="fallback-model",
    )
    bot.ai_provider = _FakeProvider()
    bot._sleep_until = 0.0
    return bot


def test_night_window_prefers_fallback(monkeypatch):
    bot = _make_bot()
    monkeypatch.setattr(
        bot_mod.time, "localtime", lambda: SimpleNamespace(tm_hour=23)
    )

    assert bot._is_in_night_fallback_window() is True
    asyncio.run(bot._generate_response([{"role": "user", "content": "hi"}]))
    assert bot.ai_provider.kwargs["prefer_fallback"] is True


def test_daytime_keeps_primary_provider(monkeypatch):
    bot = _make_bot()
    monkeypatch.setattr(
        bot_mod.time, "localtime", lambda: SimpleNamespace(tm_hour=12)
    )

    assert bot._is_in_night_fallback_window() is False
    asyncio.run(bot._generate_response([{"role": "user", "content": "hi"}]))
    assert "prefer_fallback" not in bot.ai_provider.kwargs


def test_night_window_does_not_put_bot_to_sleep(monkeypatch):
    bot = _make_bot()
    monkeypatch.setattr(
        bot_mod.time, "localtime", lambda: SimpleNamespace(tm_hour=23)
    )

    assert bot._is_sleeping() == (False, 0)


def test_night_fallback_is_disabled_without_a_configured_fallback(monkeypatch):
    bot = _make_bot()
    bot.config.OLLAMA_FALLBACK_MODEL = ""
    monkeypatch.setattr(
        bot_mod.time, "localtime", lambda: SimpleNamespace(tm_hour=23)
    )

    assert bot._night_fallback_active() is False
    asyncio.run(bot._generate_response([{"role": "user", "content": "hi"}]))
    assert "prefer_fallback" not in bot.ai_provider.kwargs
