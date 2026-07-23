"""Configuration management for Maxwell Bot"""

import os
from pathlib import Path

from dotenv.main import load_dotenv

APP_ROOT = Path(__file__).resolve().parent
ENV_FILE = Path(os.getenv("MAXWELL_ENV_FILE", APP_ROOT / ".env"))
# Don't override real environment - .env is fallback only
load_dotenv(ENV_FILE, override=False)


def _int_env(
    name: str, default: int, min_value: int | None = None, max_value: int | None = None
) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    if min_value is not None:
        value = max(min_value, value)
    if max_value is not None:
        value = min(max_value, value)
    return value


def _float_env(
    name: str,
    default: float,
    min_value: float | None = None,
    max_value: float | None = None,
) -> float:
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
    TELEGRAM_WEBHOOK_PORT = _int_env(
        "TELEGRAM_WEBHOOK_PORT", 8443, min_value=1024, max_value=65535
    )

    OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    OLLAMA_API_KEY = os.getenv("OLLAMA_API_KEY", os.getenv("OPENAI_COMPAT_API_KEY", ""))
    OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "gemma4:31b-cloud")
    OLLAMA_REM_MODEL = os.getenv("OLLAMA_REM_MODEL") or OLLAMA_MODEL
    # max_tokens = max *output* tokens per completion (not context window).
    # minimax-m3 allows huge context but caps output ~131072; 8192 is a sane default.
    OLLAMA_MAX_TOKENS = _int_env(
        "OLLAMA_MAX_TOKENS", 8192, min_value=1, max_value=131072
    )
    OLLAMA_TEMPERATURE = _float_env("OLLAMA_TEMPERATURE", 1.0, min_value=0.0)
    OLLAMA_DISABLE_REASONING = _bool_env("OLLAMA_DISABLE_REASONING", True)
    OLLAMA_FALLBACK_BASE_URL = os.getenv("OLLAMA_FALLBACK_BASE_URL", "").strip()
    OLLAMA_FALLBACK_API_KEY = os.getenv("OLLAMA_FALLBACK_API_KEY", "").strip()
    OLLAMA_FALLBACK_MODEL = os.getenv("OLLAMA_FALLBACK_MODEL", "").strip()
    OLLAMA_FALLBACK_DISABLE_REASONING = _bool_env(
        "OLLAMA_FALLBACK_DISABLE_REASONING", True
    )
    OLLAMA_RETRY_ATTEMPTS = _int_env(
        "OLLAMA_RETRY_ATTEMPTS", 3, min_value=1, max_value=10
    )

    # Toggle for "omni" (audio+vision capable) model input.
    # Default is now OFF. Set to true in .env to allow audio input for models that support it.
    ENABLE_AUDIO_INPUT = _bool_env("ENABLE_AUDIO_INPUT", False)

    # -------------------------------------------------------------------------
    # Feature kill switches (default true unless noted — matches legacy
    # behaviour). All read once at import time; restart the bot to change.
    # -------------------------------------------------------------------------
    ENABLE_IMAGE_INPUT = _bool_env("ENABLE_IMAGE_INPUT", True)
    ENABLE_VIDEO_INPUT = _bool_env("ENABLE_VIDEO_INPUT", True)
    ENABLE_IMAGE_GEN = _bool_env("ENABLE_IMAGE_GEN", True)
    ENABLE_TTS = _bool_env("ENABLE_TTS", True)
    ENABLE_TTS_VC = _bool_env("ENABLE_TTS_VC", True)
    ENABLE_EMAIL_TOOLS = _bool_env("ENABLE_EMAIL_TOOLS", True)
    ENABLE_VC = _bool_env("ENABLE_VC", True)
    ENABLE_YOUTUBE = _bool_env("ENABLE_YOUTUBE", True)
    ENABLE_WEB_SEARCH = _bool_env("ENABLE_WEB_SEARCH", True)
    ENABLE_FETCH_URL = _bool_env("ENABLE_FETCH_URL", True)
    ENABLE_CREATE_SITE = _bool_env("ENABLE_CREATE_SITE", True)
    ENABLE_AVATAR = _bool_env("ENABLE_AVATAR", True)
    ENABLE_SHELL = _bool_env("ENABLE_SHELL", True)
    ENABLE_TELEGRAM = _bool_env("ENABLE_TELEGRAM", True)
    ENABLE_AUTONOMY = _bool_env("ENABLE_AUTONOMY", True)

    # When false (default), shell refuses to run on a turn
    # that read untrusted fetched content (URLs, web search) without an
    # out-of-band `,confirm` from an admin. This blocks indirect prompt
    # injection from turning a fetched page into a shell command.
    # Set to true to skip the gate entirely — the model can call shell
    # after fetch_url/web_search without confirmation. Only do this if
    # you trust the model fully (single-user homelab install).
    DISABLE_TAINT_GATE = _bool_env("DISABLE_TAINT_GATE", False)

    # TTS engine selection. local / riva / gtts / auto. Undocumented before
    # 2026-07-21 — used to fall through a chain in bot._synthesize_tts_wav.
    TTS_ENGINE = os.getenv("TTS_ENGINE", "auto").strip().lower()

    # Optional secondary auth fallback for the primary LLM endpoint.
    OPENAI_COMPAT_API_KEY = os.getenv("OPENAI_COMPAT_API_KEY", "").strip()

    AUTONOMY_BASE_URL = os.getenv("AUTONOMY_BASE_URL", "").strip()
    AUTONOMY_API_KEY = os.getenv(
        "AUTONOMY_API_KEY", os.getenv("OPENAI_COMPAT_API_KEY", "")
    ).strip()
    AUTONOMY_MODEL = os.getenv("AUTONOMY_MODEL", "").strip()
    AUTONOMY_DISABLE_REASONING = _bool_env("AUTONOMY_DISABLE_REASONING", False)

    # Auxiliary background agents (REM, context-cleanup, context-watcher).
    # These are the "context manager" brains — separate from the autonomy
    # tick loop so they can run on a different (e.g. cheaper/faster) model
    # than autonomy. Defaults fall back to the autonomy config, which in
    # turn falls back to the main OLLAMA_* provider, so a fresh install
    # with no AUX_* vars behaves exactly as before (all background agents
    # shared one endpoint).
    AUX_BASE_URL = os.getenv("AUX_BASE_URL", "").strip()
    AUX_API_KEY = os.getenv(
        "AUX_API_KEY", os.getenv("OPENAI_COMPAT_API_KEY", "")
    ).strip()
    AUX_MODEL = os.getenv("AUX_MODEL", "").strip()
    AUX_DISABLE_REASONING = _bool_env("AUX_DISABLE_REASONING", True)

    # Live tool progress messages. OFF by default; set MAXWELL_PROGRESS_MESSAGES=true
    # in .env to enable for every server. The feature is also per-server: an
    # admin can turn it on for one server with `,progress on` (stored in
    # data/progress_servers.json) without affecting other servers. When
    # enabled, the bot posts one short status message ("shell: checking disk")
    # per non-terminal tool batch, edits it in place as tools run, and deletes
    # it when the batch ends. See tool_progress.py for design.
    PROGRESS_MESSAGES = _bool_env("MAXWELL_PROGRESS_MESSAGES", False)

    # Custom streaming tool-call protocol. Native OpenAI-style tools= doesn't
    # stream incrementally on some providers (notably Ollama cloud's
    # minimax-m3): the entire {name, arguments} block arrives in one final
    # delta at ~88% of stream time, so the bot's progress message stays
    # silent for the full 10-30s of generation. When this flag is on, the
    # bot asks the model to emit the tool call as a bare JSON object on its
    # own line ({"name": "...", "arguments": {...}}) and parses it from the
    # text stream AS IT STREAMS. Tool name lands in the progress UI at
    # ~12% of stream time vs ~88% for native. OFF by default to keep native
    # behavior; turn on with MAXWELL_CUSTOM_TOOL_CALLS=true in .env.
    CUSTOM_TOOL_CALLS = _bool_env("MAXWELL_CUSTOM_TOOL_CALLS", False)

    POLLINATIONS_MODEL = os.getenv("POLLINATIONS_MODEL", "flux")

    NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY", "")
    NVIDIA_IMAGE_URL = os.getenv(
        "NVIDIA_IMAGE_URL",
        "https://ai.api.nvidia.com/v1/genai/black-forest-labs/flux.1-dev",
    )

    GPT_IMAGE_URL = os.getenv("GPT_IMAGE_URL", "")
    GPT_IMAGE_API_KEY = os.getenv("GPT_IMAGE_API_KEY", "")

    MEMORY_MESSAGE_LIMIT = _int_env(
        "MEMORY_MESSAGE_LIMIT", 2000, min_value=1, max_value=10000
    )
    REM_ENABLED = _bool_env("REM_ENABLED", True)
    REM_INTERVAL_SECONDS = _int_env("REM_INTERVAL_SECONDS", 600, min_value=10)
    REM_MAX_TURNS = _int_env("REM_MAX_TURNS", 3, min_value=0, max_value=10)
    REM_EVENT_BUFFER_MAX = _int_env(
        "REM_EVENT_BUFFER_MAX", 500, min_value=1, max_value=10000
    )
    REM_RUN_HISTORY = _int_env("REM_RUN_HISTORY", 50, min_value=1, max_value=1000)

    DATA_DIR = os.getenv("DATA_DIR", "data")
    LOGS_DIR = os.getenv("LOGS_DIR", os.getenv("LOGS", "logs"))
    LOG_LEVEL = os.getenv("LOG_LEVEL", "info")

    MAXWELL_SITE_DIR = os.getenv("MAXWELL_SITE_DIR", "public/bot")
    MAXWELL_PUBLIC_BASE_URL = os.getenv(
        "MAXWELL_PUBLIC_BASE_URL", "https://maxwell.example.com"
    )
    MAXWELL_API_HOST = os.getenv("MAXWELL_API_HOST", "127.0.0.1")
    MAXWELL_API_PORT = _int_env("MAXWELL_API_PORT", 8765, min_value=1, max_value=65535)
    MAXWELL_CORS_ORIGIN = os.getenv(
        "MAXWELL_CORS_ORIGIN", MAXWELL_PUBLIC_BASE_URL.rstrip("/")
    )

    # Local mail (maxwell@z3ki.dev). Bot talks to local Postfix for
    # outbound and local Dovecot for inbound; no third-party relay. The
    # default host/port values match the Postfix+Dovecot setup documented
    # in email_integration/README.md. Override the env vars only if you
    # intentionally point the bot at a different mail server (debugging,
    # testing against a sandbox, etc.).
    MAXWELL_SMTP_HOST = os.getenv("MAXWELL_SMTP_HOST", "127.0.0.1").strip()
    MAXWELL_SMTP_PORT = _int_env("MAXWELL_SMTP_PORT", 25, min_value=1, max_value=65535)
    MAXWELL_IMAP_HOST = os.getenv("MAXWELL_IMAP_HOST", "127.0.0.1").strip()
    MAXWELL_IMAP_PORT = _int_env("MAXWELL_IMAP_PORT", 993, min_value=1, max_value=65535)
    MAXWELL_EMAIL_USER = os.getenv("MAXWELL_EMAIL_USER", "maxwell@z3ki.dev").strip()
    MAXWELL_EMAIL_PASSWORD = os.getenv("MAXWELL_EMAIL_PASSWORD", "").strip()
    MAXWELL_EMAIL_FROM = os.getenv("MAXWELL_EMAIL_FROM", "maxwell@z3ki.dev").strip()
    MAXWELL_EMAIL_FROM_NAME = os.getenv("MAXWELL_EMAIL_FROM_NAME", "Maxwell").strip()

    # Admin / owner allowlists. Re-exported here so Config is the single
    # source of truth; bot_tools.refresh_owner_ids() still does a runtime
    # reload but the initial parse lives here.
    MAXWELL_ADMIN_USER = os.getenv("MAXWELL_ADMIN_USER", "admin").strip()
    MAXWELL_ADMIN_PASSWORD = os.getenv("MAXWELL_ADMIN_PASSWORD", "").strip()
    MAXWELL_OWNER_IDS = {
        item.strip()
        for item in os.getenv("MAXWELL_OWNER_IDS", "").split(",")
        if item.strip()
    }

    @classmethod
    def validate(cls):
        if not cls.DISCORD_TOKEN:
            raise ValueError(
                "DISCORD_TOKEN is required. Set it in .env before starting the bot."
            )
        if not cls.OLLAMA_BASE_URL:
            raise ValueError("OLLAMA_BASE_URL is required")
        if cls.OLLAMA_MAX_TOKENS < 1:
            raise ValueError("OLLAMA_MAX_TOKENS must be >= 1")

        # Soft warnings — these don't block startup but they WILL cause
        # runtime errors the first time someone hits the feature, which is
        # confusing without a hint. Log via the standard logging facility
        # so pm2 captures it.
        import logging
        _log = logging.getLogger("maxwell.config")

        if not cls.MAXWELL_ADMIN_PASSWORD:
            _log.warning(
                "MAXWELL_ADMIN_PASSWORD is empty — the admin API will return "
                "503 on every request. Set a real password in .env."
            )
        if not cls.MAXWELL_OWNER_IDS:
            _log.warning(
                "MAXWELL_OWNER_IDS is empty — admin commands (`,prompt`, "
                "`,clearmem`, `,autonomy`, `,rem`, etc.) will be denied to "
                "everyone. Set your Discord user ID in .env."
            )
        if cls.ENABLE_EMAIL_TOOLS and not cls.MAXWELL_EMAIL_PASSWORD:
            _log.warning(
                "ENABLE_EMAIL_TOOLS=true but MAXWELL_EMAIL_PASSWORD is empty — "
                "the email tools will return a 'not configured' error on every "
                "call. Either set MAXWELL_EMAIL_PASSWORD or set "
                "ENABLE_EMAIL_TOOLS=false."
            )
        if cls.ENABLE_TELEGRAM and cls.TELEGRAM_TOKEN:
            _log.info(
                "TELEGRAM_TOKEN is set — Telegram polling will auto-start. "
                "Set ENABLE_TELEGRAM=false to suppress without removing the token."
            )
        # TTS engine sanity check
        if cls.TTS_ENGINE not in {"auto", "local", "riva", "gtts"}:
            _log.warning(
                "TTS_ENGINE=%r is not one of auto/local/riva/gtts — falling "
                "back to 'auto' behaviour.",
                cls.TTS_ENGINE,
            )
