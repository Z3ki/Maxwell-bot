"""Install ergonomics: feature auto-detection and endpoint normalization.

These cover the "a bare clone should just work" contract — a missing
dependency turns one feature off, it does not break the bot.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

import config as config_module
from providers import normalize_base_url
from rag_memory import _embed_endpoint, _extract_embeddings


@pytest.mark.parametrize(
    "value,expected",
    [
        ("true", True),
        ("TRUE", True),
        ("1", True),
        ("on", True),
        ("false", False),
        ("0", False),
        ("off", False),
    ],
)
def test_explicit_switch_overrides_detection(monkeypatch, value, expected):
    monkeypatch.setenv("ENABLE_THING", value)
    # Detection says the opposite of whatever was asked for; the env wins.
    assert config_module._feature_env("ENABLE_THING", lambda: not expected) is expected


def test_auto_follows_detection(monkeypatch):
    monkeypatch.delenv("ENABLE_THING", raising=False)
    assert config_module._feature_env("ENABLE_THING", lambda: True) is True
    assert config_module._feature_env("ENABLE_THING", lambda: False) is False


def test_auto_records_a_reason(monkeypatch):
    monkeypatch.delenv("ENABLE_THING", raising=False)
    config_module._feature_env("ENABLE_THING", lambda: False, needs="ffmpeg")
    assert "ffmpeg" in config_module.FEATURE_REASONS["ENABLE_THING"]


def test_garbage_value_falls_back_to_detection(monkeypatch):
    monkeypatch.setenv("ENABLE_THING", "sometimes")
    assert config_module._feature_env("ENABLE_THING", lambda: True) is True


def test_no_dependency_features_default_on(monkeypatch):
    monkeypatch.delenv("ENABLE_THING", raising=False)
    assert config_module._feature_env("ENABLE_THING") is True
    assert config_module._feature_env("ENABLE_THING", default=False) is False


def test_feature_report_covers_every_switch():
    from config import Config

    report = Config.feature_report()
    assert len(report) == len(Config.FEATURE_SWITCHES)
    for name, label, enabled, reason in report:
        assert isinstance(enabled, bool)
        assert reason, f"{name} has no explanation"
        assert label


def _run(code, env=None):
    """Run a snippet in a fresh interpreter with a clean environment.

    Reloading config in-process would hand every already-imported module a
    stale Config class, so the shipped defaults are checked out-of-process.
    """
    child_env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(
            ("AI_", "REM_", "ENABLE_", "OLLAMA_", "OPENAI_COMPAT_", "MAXWELL_", "DISCORD_")
        )
    }
    child_env["MAXWELL_ENV_FILE"] = os.devnull
    child_env.update(env or {})
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parent.parent,
        env=child_env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _config_value(attr, env=None):
    return _run(f"from config import Config; print(Config.{attr})", env)


def test_rem_is_opt_in_by_default():
    """REM spends tokens on a timer — it must never default to on."""
    assert _config_value("REM_ENABLED") == "False"


@pytest.mark.parametrize("flag", ["ENABLE_CREATE_SITE", "ENABLE_HD_IMAGE", "ENABLE_RAG", "ENABLE_EMAIL_TOOLS", "ENABLE_SHELL", "ENABLE_AUTONOMY", "MAXWELL_RESTRICT_PUBLIC_RUNTIME"])
def test_unconfigured_integrations_are_off(flag):
    assert _config_value(flag) == "False"


@pytest.mark.parametrize("flag,settings", [
    ("ENABLE_CREATE_SITE", {"MAXWELL_PUBLIC_BASE_URL": "https://bot.example.test"}),
    ("ENABLE_HD_IMAGE", {"GEMINI_IMAGE_MODEL": "my-image-model"}),
    ("ENABLE_RAG", {"EMBED_MODEL": "my-embed-model"}),
    ("ENABLE_EMAIL_TOOLS", {"MAXWELL_EMAIL_USER": "bot@example.test", "MAXWELL_EMAIL_PASSWORD": "test"}),
])
def test_configured_integrations_enable_and_respect_explicit_off(flag, settings):
    assert _config_value(flag, settings) == "True"
    assert _config_value(flag, {**settings, flag: "false"}) == "False"


def test_mailbox_password_alone_does_not_enable_email():
    assert _config_value("ENABLE_EMAIL_TOOLS", {"MAXWELL_EMAIL_PASSWORD": "test"}) == "False"


@pytest.mark.parametrize("template", [".env.example", ".env.simple.example"])
def test_shipped_templates_do_not_enable_unconfigured_integrations(template):
    code = "from config import Config; print(any(getattr(Config, name) for name in " \
           "['ENABLE_CREATE_SITE','ENABLE_HD_IMAGE','ENABLE_RAG','ENABLE_EMAIL_TOOLS','ENABLE_SHELL']))"
    assert _run(code, {"MAXWELL_ENV_FILE": str(Path(__file__).resolve().parents[1] / template)}) == "False"


def test_enable_rem_alias_turns_rem_on():
    # The docs have always called it ENABLE_REM; honour that spelling.
    assert _config_value("REM_ENABLED", {"ENABLE_REM": "true"}) == "True"
    # An explicit REM_ENABLED still wins over the alias.
    assert (
        _config_value("REM_ENABLED", {"ENABLE_REM": "true", "REM_ENABLED": "false"})
        == "False"
    )


MINIMUM_ENV = {
    "DISCORD_BOT_TOKEN": "test-token",
    "OLLAMA_BASE_URL": "http://localhost:11434",
    "OLLAMA_MODEL": "test-model",
}


def test_minimum_install_only_needs_token_and_model():
    """A token plus a model endpoint is the whole hard requirement."""
    assert (
        _run(
            "from config import Config; Config.validate(); print('valid')", MINIMUM_ENV
        )
        == "valid"
    )


def test_friendly_provider_config_works_without_legacy_aliases():
    env = {
        "DISCORD_BOT_TOKEN": "test-token",
        "AI_API_URL": "http://127.0.0.1:9999/v1",
        "AI_MODEL": "friendly-model",
        "AI_API_KEY": "friendly-key",
    }
    assert _run(
        "from config import Config; Config.validate(); "
        "print(Config.OLLAMA_BASE_URL, Config.OLLAMA_MODEL, Config.OLLAMA_API_KEY)",
        env,
    ) == "http://127.0.0.1:9999/v1 friendly-model friendly-key"


def test_friendly_provider_config_overrides_stale_legacy_settings():
    env = {
        **MINIMUM_ENV,
        "OLLAMA_API_KEY": "stale-key",
        "AI_API_URL": "http://127.0.0.1:9999/v1",
        "AI_MODEL": "friendly-model",
        "AI_API_KEY": "",
    }
    assert _run(
        "from config import Config; print(repr(("
        "Config.OLLAMA_BASE_URL, Config.OLLAMA_MODEL, Config.OLLAMA_API_KEY)))",
        env,
    ) == "('http://127.0.0.1:9999/v1', 'friendly-model', '')"


def test_validate_accepts_legacy_discord_token_alias():
    env = {k: v for k, v in MINIMUM_ENV.items() if k != "DISCORD_BOT_TOKEN"}
    env["DISCORD_TOKEN"] = "legacy-bot-token"
    assert (
        _run(
            "from config import Config; Config.validate(); print('valid')", env
        )
        == "valid"
    )


@pytest.mark.parametrize(
    "missing,expected",
    [
        ("DISCORD_BOT_TOKEN", "DISCORD_BOT_TOKEN"),
        ("OLLAMA_MODEL", "AI_MODEL"),
    ],
)
def test_validate_names_the_missing_requirement(missing, expected):
    env = {k: v for k, v in MINIMUM_ENV.items() if k != missing}
    out = _run(
        "from config import Config\n"
        "try:\n"
        "    Config.validate()\n"
        "except ValueError as exc:\n"
        "    print(exc)\n",
        env,
    )
    assert expected in out


@pytest.mark.parametrize(
    "given,expected",
    [
        # A bare host gets the conventional API path — the #1 setup mistake.
        ("http://localhost:11434", "http://localhost:11434/v1"),
        ("https://api.openai.com", "https://api.openai.com/v1"),
        ("http://localhost:11434/", "http://localhost:11434/v1"),
        # Anything with a path is the operator's business, left alone.
        ("https://openrouter.ai/api/v1", "https://openrouter.ai/api/v1"),
        ("https://example.com/v2", "https://example.com/v2"),
        ("", ""),
    ],
)
def test_base_url_normalization(given, expected):
    assert normalize_base_url(given) == expected


@pytest.mark.parametrize(
    "given,expected",
    [
        ("http://localhost:11434", "http://localhost:11434/api/embed"),
        ("https://api.openai.com/v1", "https://api.openai.com/v1/embeddings"),
        ("https://api.openai.com/v1/embeddings", "https://api.openai.com/v1/embeddings"),
        ("http://box:11434/api/embed", "http://box:11434/api/embed"),
        ("", "http://localhost:11434/api/embed"),
    ],
)
def test_embed_endpoint_derivation(given, expected):
    assert _embed_endpoint(given) == expected


def test_embedding_response_shapes():
    """Ollama and OpenAI disagree on the wrapper; both must parse."""
    assert _extract_embeddings({"embeddings": [[1.0, 2.0]]}) == [[1.0, 2.0]]
    assert _extract_embeddings({"embedding": [1.0]}) == [[1.0]]
    assert _extract_embeddings({"data": [{"embedding": [3.0]}, {"embedding": [4.0]}]}) == [
        [3.0],
        [4.0],
    ]
    assert _extract_embeddings({"data": []}) == []
    assert _extract_embeddings({}) == []
    assert _extract_embeddings(None) == []
