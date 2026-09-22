"""Persistent per-user quota behavior, including concurrent reservations."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from daily_tokens import DailyTokens, DailyTokenLimitExceeded


def test_usage_persists_and_resets_by_utc_day(tmp_path, monkeypatch):
    monkeypatch.setattr(DailyTokens, "today", staticmethod(lambda: "2026-09-22"))
    path = tmp_path / "quota.sqlite3"
    ledger = DailyTokens(path)
    reservation, output = ledger.reserve("12", 1000, 100, 200)
    assert output == 200
    ledger.settle(reservation, 150)
    assert DailyTokens(path).status("12", 1000)["spent"] == 150
    monkeypatch.setattr(DailyTokens, "today", staticmethod(lambda: "2026-09-23"))
    assert DailyTokens(path).status("12", 1000)["spent"] == 0


def test_concurrent_calls_cannot_oversubscribe(tmp_path):
    ledger = DailyTokens(tmp_path / "quota.sqlite3")

    def reserve(_):
        try:
            return ledger.reserve("12", 400, 100, 100)
        except DailyTokenLimitExceeded:
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(reserve, range(4)))
    assert len([item for item in results if item]) == 2
    assert ledger.status("12", 400)["reserved"] == 400
    for item in results:
        if item:
            ledger.settle(item[0], 120)
    assert ledger.status("12", 400)["spent"] == 240
    assert ledger.status("12", 400)["reserved"] == 0


def test_override_exemption_and_reset_are_persistent(tmp_path):
    path = tmp_path / "quota.sqlite3"
    ledger = DailyTokens(path)
    ledger.configure("12", limit=500)
    reservation, _ = ledger.reserve("12", 100, 100, 100)
    ledger.settle(reservation, 190)
    assert DailyTokens(path).status("12", 100)["limit"] == 500
    ledger.configure("12", exempt=True)
    assert ledger.reserve("12", 100, 10000, 1000) == (None, 1000)
    ledger.configure("12", reset=True, exempt=False)
    assert ledger.status("12", 100)["spent"] == 0
    ledger.configure("12", clear=True)
    assert ledger.status("12", 100)["limit"] == 100


def test_live_provider_calls_charge_actual_usage_and_stop_at_limit(tmp_path):
    import asyncio
    from types import SimpleNamespace

    from bot import MaxwellBot, _current_inbound
    from providers import ProviderResult

    ledger = DailyTokens(tmp_path / "quota.sqlite3")
    calls = []

    async def generate(_messages, **kwargs):
        calls.append(kwargs)
        return ProviderResult("ok", usage={"total_tokens": 60})

    bot = object.__new__(MaxwellBot)
    bot._control = {"daily_user_token_limit_enabled": True, "daily_user_token_limit": 120, "live_max_output_tokens": 64}
    bot._daily_tokens = ledger
    bot.ai_provider = SimpleNamespace(generate_response=generate, model="test")
    bot._night_fallback_kwargs = dict
    message = SimpleNamespace(id=1, author=SimpleNamespace(id=42, bot=False), channel=SimpleNamespace(id=2))
    token = _current_inbound.set(message)
    try:
        async def run():
            await MaxwellBot._generate_response(bot, [{"role": "user", "content": "hi"}], max_tokens=50)
            await MaxwellBot._generate_response(bot, [{"role": "user", "content": "hi"}], max_tokens=50)
            with pytest.raises(DailyTokenLimitExceeded):
                await MaxwellBot._generate_response(bot, [{"role": "user", "content": "hi"}], max_tokens=50)

        asyncio.run(run())
    finally:
        _current_inbound.reset(token)
    assert len(calls) == 2
    assert ledger.status("42", 120)["spent"] == 120
