"""Tests for the aux background-agent provider/model resolution.

The aux group (REM, context-cleanup, context-watcher) is now separate from the
autonomy tick loop. _get_aux_provider / _get_aux_model resolve in the order
aux_* -> autonomy_* -> main provider. These tests pin that cascade without
booting the full bot: they monkeypatch the heavy methods and drive just the
resolution logic.
"""

import asyncio

import bot as bot_mod
from bot import MaxwellBot


class _FakeProvider:
    """Stand-in for OllamaProvider; records close() and init()."""

    def __init__(self, name="main"):
        self.name = name
        self.available = True
        self.closed = False
        self.inited = 0

    async def initialize(self):
        self.inited += 1

    async def close(self):
        self.closed = True


def _make_bot(monkeypatch, *, control=None, aux_env=None, auto_env=None):
    """Build a MaxwellBot-shaped object with only the resolution attrs.

    MaxwellBot.__init__ does a lot of discord wiring; we sidestep it by
    constructing via __new__ and setting the handful of attributes the
    resolution methods read.
    """
    cfg = {
        "AUX_BASE_URL": (aux_env or {}).get("base_url", ""),
        "AUX_API_KEY": (aux_env or {}).get("api_key", ""),
        "AUX_MODEL": (aux_env or {}).get("model", ""),
        "AUX_DISABLE_REASONING": (aux_env or {}).get("disable_reasoning", True),
        "AUTONOMY_BASE_URL": (auto_env or {}).get("base_url", ""),
        "AUTONOMY_API_KEY": (auto_env or {}).get("api_key", ""),
        "AUTONOMY_MODEL": (auto_env or {}).get("model", ""),
        "AUTONOMY_DISABLE_REASONING": (auto_env or {}).get("disable_reasoning", False),
        "OLLAMA_MODEL": "main-model",
        "OLLAMA_MAX_TOKENS": 8192,
        "OLLAMA_TEMPERATURE": 1.0,
        "OLLAMA_FALLBACK_BASE_URL": "",
        "OLLAMA_FALLBACK_MODEL": "",
        "OLLAMA_FALLBACK_API_KEY": "",
        "OLLAMA_FALLBACK_DISABLE_REASONING": True,
        "OLLAMA_RETRY_ATTEMPTS": 1,
        "ENABLE_AUDIO_INPUT": False,
    }

    class _Cfg:
        pass

    c = _Cfg()
    for k, v in cfg.items():
        setattr(c, k, v)

    inst = MaxwellBot.__new__(MaxwellBot)
    inst.config = c
    inst._control = control or {}
    inst.ai_provider = _FakeProvider("main")
    inst.autonomy_provider = None
    inst._autonomy_provider_sig = ""
    inst.aux_provider = None
    inst._aux_provider_sig = ""
    inst._tracked = []

    def _track(task):
        inst._tracked.append(task)

    inst._track_task = _track

    # Stub OllamaProvider so we don't touch the network: return a labeled fake
    # and remember what it was built with.
    built = []

    class _FakeOllama:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.available = True
            built.append(self)

        async def initialize(self):
            self.inited = True

        async def close(self):
            pass

    monkeypatch.setattr(bot_mod, "OllamaProvider", _FakeOllama)
    inst._built = built
    return inst


def test_aux_model_falls_back_to_autonomy_then_main(monkeypatch):
    bot = _make_bot(monkeypatch)
    # No aux_model, no autonomy_model -> None (provider default = main).
    assert bot._get_aux_model() is None

    bot._control = {"autonomy_model": "auto-m"}
    assert bot._get_aux_model() == "auto-m"

    bot._control = {"aux_model": "aux-m", "autonomy_model": "auto-m"}
    assert bot._get_aux_model() == "aux-m"


def test_aux_model_env_fallback(monkeypatch):
    bot = _make_bot(monkeypatch, auto_env={"model": "auto-env"})
    assert bot._get_aux_model() == "auto-env"

    bot = _make_bot(monkeypatch, aux_env={"model": "aux-env"}, auto_env={"model": "auto-env"})
    assert bot._get_aux_model() == "aux-env"


def test_get_aux_provider_without_aux_config_defers_to_autonomy(monkeypatch):
    """No AUX base_url -> _get_aux_provider delegates to _get_autonomy_provider."""
    bot = _make_bot(monkeypatch)

    async def _auto():
        return bot.ai_provider  # main

    bot._get_autonomy_provider = _auto
    prov = asyncio.run(bot._get_aux_provider())
    assert prov is bot.ai_provider
    # No dedicated aux provider should have been cached.
    assert bot.aux_provider is None
    assert bot._aux_provider_sig == ""


def test_get_aux_provider_builds_dedicated_when_aux_base_url_set(monkeypatch):
    bot = _make_bot(
        monkeypatch,
        control={"aux_base_url": "https://aux.example", "aux_model": "aux-m"},
    )
    # autonomy provider should NOT be consulted when aux has its own base_url.
    called = {"auto": False}

    async def _auto():
        called["auto"] = True
        return bot.ai_provider

    bot._get_autonomy_provider = _auto
    prov = asyncio.run(bot._get_aux_provider())
    assert called["auto"] is False
    assert len(bot._built) == 1
    assert bot._built[0].kwargs["base_url"] == "https://aux.example"
    assert bot._built[0].kwargs["model"] == "aux-m"
    assert prov is bot._built[0]
    assert bot.aux_provider is prov
    assert "https://aux.example" in bot._aux_provider_sig


def test_get_aux_provider_caches(monkeypatch):
    bot = _make_bot(
        monkeypatch,
        control={"aux_base_url": "https://aux.example", "aux_model": "aux-m"},
    )
    asyncio.run(bot._get_aux_provider())
    first = bot.aux_provider
    assert first is not None
    # Second call reuses the cached provider; no new build.
    prov = asyncio.run(bot._get_aux_provider())
    assert prov is first
    assert len(bot._built) == 1


def test_get_aux_provider_closes_prior_on_config_churn(monkeypatch):
    bot = _make_bot(
        monkeypatch,
        control={"aux_base_url": "https://aux.example", "aux_model": "aux-m"},
    )
    asyncio.run(bot._get_aux_provider())
    first = bot.aux_provider
    assert first is not None
    # Change the model -> signature changes -> old provider scheduled for close.
    bot._control = {"aux_base_url": "https://aux.example", "aux_model": "aux-m2"}
    asyncio.run(bot._get_aux_provider())
    assert len(bot._built) == 2
    # The tracked close tasks should include the first provider's close.
    assert any(
        getattr(t, "_coro", None) is not None for t in bot._tracked
    )


def test_get_aux_provider_falls_back_to_main_when_unavailable(monkeypatch):
    bot = _make_bot(
        monkeypatch,
        control={"aux_base_url": "https://aux.example", "aux_model": "aux-m"},
    )

    # Make the built provider fail availability.
    class _DeadOllama:
        def __init__(self, **kwargs):
            self.available = False
            self.kwargs = kwargs

        async def initialize(self):
            self.available = False

        async def close(self):
            pass

    monkeypatch.setattr(bot_mod, "OllamaProvider", _DeadOllama)
    prov = asyncio.run(bot._get_aux_provider())
    assert prov is bot.ai_provider