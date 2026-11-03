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
