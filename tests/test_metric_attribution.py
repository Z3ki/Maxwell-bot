"""Model metrics identify routes without exposing personal provider details."""

import asyncio
import sqlite3
from types import SimpleNamespace

import pytest

import bot_parts.ai as ai
import usage_metrics
from maxwell_core.providers.models import ProviderPolicy, ProviderResult


@pytest.mark.parametrize(
    ("name", "sensitive", "model", "expected"),
    [
        ("byok", True, "private-model-name", ("byok", "byok", "byok")),
        ("worker", False, "deepseek/deepseek-v4.1-flash", ("deepseek/deepseek-v4.1-flash", "api.deepseek.com", "worker")),
        ("primary", False, "unconfigured-private-model", ("other", "api.deepseek.com", "primary")),
    ],
)
def test_model_attribution(tmp_path, monkeypatch, name, sensitive, model, expected):
    monkeypatch.setenv("AI_WORKER_MODEL", "deepseek/deepseek-v4.1-flash")

    async def resume(*args):
        pass

    monkeypatch.setattr(ai, "typing_resume", resume)

    class Provider:
        policy = ProviderPolicy(sensitive_credentials=sensitive)
        base_url = "https://user:password@API.DeepSeek.Com:8443/private?key=private"

        async def generate_response(self, messages, **kwargs):
            return ProviderResult("ok", model=model, provider="primary")

    provider = Provider()
    provider.name = name
    bot = SimpleNamespace(
        ai_provider=provider,
        config=SimpleNamespace(DATA_DIR=tmp_path, AI_WORKER_MODEL="deepseek/deepseek-v4.1-flash"),
        _control={},
    )
    assert asyncio.run(ai.AIMixin._generate_response(bot, [], provider=provider)) == "ok"
    usage_metrics._queue.join()
    report = usage_metrics.stats(tmp_path)
    row = report["models"][0]
    assert (row["model"], row["provider"], row["route"]) == expected
    with sqlite3.connect(tmp_path / "usage_metrics.sqlite3") as db:
        stored = "\n".join(db.iterdump())
    assert "private-model-name" not in stored
    assert "unconfigured-private-model" not in stored
    assert "password" not in stored
    assert "/private" not in stored
    if sensitive:
        assert "deepseek.com" not in stored


def test_provider_cache_usage_reaches_aggregates(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_MODEL", "example")

    async def resume(*args):
        pass

    monkeypatch.setattr(ai, "typing_resume", resume)

    class Provider:
        name = "primary"
        model = "example"
        base_url = "https://api.example.com"
        policy = ProviderPolicy()

        def __init__(self):
            self.usages = iter([
                {"prompt_tokens": 100, "cached_tokens": 80, "cache_write_tokens": 20},
                {"prompt_tokens": 100, "cached_tokens": 0, "cache_write_tokens": 0},
                {"prompt_tokens": 500},
            ])

        async def generate_response(self, messages, **kwargs):
            return ProviderResult("ok", usage=next(self.usages), model="example", provider="primary")

    provider = Provider()
    bot = SimpleNamespace(
        ai_provider=provider, config=SimpleNamespace(DATA_DIR=tmp_path, AI_MODEL="example"), _control={},
    )

    async def generate():
        for _ in range(3):
            await ai.AIMixin._generate_response(bot, [], provider=provider)

    asyncio.run(generate())
    usage_metrics._queue.join()
    row = usage_metrics.stats(tmp_path)["models"][0]
    assert row["cached_tokens"] == 80
    assert row["cache_write_tokens"] == 20
    assert row["cached_tokens_samples"] == 2
    assert row["cache_write_tokens_samples"] == 2
    assert row["cache_hit_ratio"] == 0.4
