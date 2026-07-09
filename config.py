"""Configuration management for Maxwell Bot"""

import os
from pathlib import Path

from dotenv.main import load_dotenv

APP_ROOT = Path(__file__).resolve().parent
ENV_FILE = Path(os.getenv("MAXWELL_ENV_FILE", APP_ROOT / ".env"))
# Don't override real environment - .env is fallback only
load_dotenv(ENV_FILE, override=False)


def _int_env(name: str, default: int, min_value: int | None = None, max_value: int | None = None) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    if min_value is not None:
        value = max(min_value, value)
    if max_value is not None:
        value = min(max_value, value)
    return value


def _float_env(name: str, default: float, min_value: float | None = None, max_value: float | None = None) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    if min_value is not None:
        value = max(min_value, value)
    if max_value is not None:
        value = min(max_value, value)
    return value


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


class Config:
    DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
    TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
    TELEGRAM_WEBHOOK_URL = os.getenv("TELEGRAM_WEBHOOK_URL", "").strip()
    TELEGRAM_WEBHOOK_PORT = _int_env("TELEGRAM_WEBHOOK_PORT", 8443, min_value=1024, max_value=65535)

    OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    OLLAMA_API_KEY = os.getenv("OLLAMA_API_KEY", os.getenv("OPENAI_COMPAT_API_KEY", ""))
    OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "gemma4:31b-cloud")
    OLLAMA_REM_MODEL = os.getenv("OLLAMA_REM_MODEL") or OLLAMA_MODEL
    OLLAMA_MAX_TOKENS = _int_env("OLLAMA_MAX_TOKENS", 200000, min_value=1)
    OLLAMA_TEMPERATURE = _float_env("OLLAMA_TEMPERATURE", 1.0, min_value=0.0)
    OLLAMA_DISABLE_REASONING = _bool_env("OLLAMA_DISABLE_REASONING", True)
    OLLAMA_FALLBACK_BASE_URL = os.getenv("OLLAMA_FALLBACK_BASE_URL", "").strip()
    OLLAMA_FALLBACK_API_KEY = os.getenv("OLLAMA_FALLBACK_API_KEY", "").strip()
    OLLAMA_FALLBACK_MODEL = os.getenv("OLLAMA_FALLBACK_MODEL", "").strip()
    OLLAMA_FALLBACK_DISABLE_REASONING = _bool_env("OLLAMA_FALLBACK_DISABLE_REASONING", True)
    OLLAMA_RETRY_ATTEMPTS = _int_env("OLLAMA_RETRY_ATTEMPTS", 3, min_value=1, max_value=10)

    AUTONOMY_BASE_URL = os.getenv("AUTONOMY_BASE_URL", "").strip()
    AUTONOMY_API_KEY = os.getenv("AUTONOMY_API_KEY", os.getenv("OPENAI_COMPAT_API_KEY", "")).strip()
    AUTONOMY_MODEL = os.getenv("AUTONOMY_MODEL", "").strip()
    AUTONOMY_DISABLE_REASONING = _bool_env("AUTONOMY_DISABLE_REASONING", False)

    POLLINATIONS_MODEL = os.getenv("POLLINATIONS_MODEL", "flux")

    NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY", "")
    NVIDIA_IMAGE_URL = os.getenv(
        "NVIDIA_IMAGE_URL",
        "https://ai.api.nvidia.com/v1/genai/black-forest-labs/flux.1-dev",
    )

    GPT_IMAGE_URL = os.getenv("GPT_IMAGE_URL", "")
    GPT_IMAGE_API_KEY = os.getenv("GPT_IMAGE_API_KEY", "")

    MEMORY_MESSAGE_LIMIT = _int_env("MEMORY_MESSAGE_LIMIT", 100, min_value=1, max_value=500)
    REM_ENABLED = _bool_env("REM_ENABLED", False)
    REM_INTERVAL_SECONDS = _int_env("REM_INTERVAL_SECONDS", 600, min_value=10)
    REM_MAX_TURNS = _int_env("REM_MAX_TURNS", 3, min_value=0, max_value=10)
    REM_EVENT_BUFFER_MAX = _int_env("REM_EVENT_BUFFER_MAX", 500, min_value=1, max_value=10000)
    REM_RUN_HISTORY = _int_env("REM_RUN_HISTORY", 50, min_value=1, max_value=1000)

    DATA_DIR = os.getenv("DATA_DIR", "data")
    LOGS_DIR = os.getenv("LOGS_DIR", os.getenv("LOGS", "logs"))
    LOG_LEVEL = os.getenv("LOG_LEVEL", "info")

    MAXWELL_SITE_DIR = os.getenv("MAXWELL_SITE_DIR", "public/bot")
    MAXWELL_PUBLIC_BASE_URL = os.getenv("MAXWELL_PUBLIC_BASE_URL", "https://maxwell.example.com")
    MAXWELL_API_HOST = os.getenv("MAXWELL_API_HOST", "127.0.0.1")
    MAXWELL_API_PORT = _int_env("MAXWELL_API_PORT", 8765, min_value=1, max_value=65535)
    MAXWELL_CORS_ORIGIN = os.getenv("MAXWELL_CORS_ORIGIN", MAXWELL_PUBLIC_BASE_URL.rstrip("/"))

    @classmethod
    def validate(cls):
        if not cls.DISCORD_TOKEN:
            raise ValueError("DISCORD_TOKEN is required")
        if not cls.OLLAMA_BASE_URL:
            raise ValueError("OLLAMA_BASE_URL is required")
        if cls.OLLAMA_MAX_TOKENS < 1:
            raise ValueError("OLLAMA_MAX_TOKENS must be >= 1")
