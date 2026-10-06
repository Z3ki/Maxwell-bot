"""Configuration management for Maxwell Bot.

Design note — optional features
-------------------------------
Maxwell only *requires* two things: a Discord token and an OpenAI-compatible
model endpoint. Everything else (YouTube, web search, email, video frames,
RAG embeddings) is optional and gated behind an ``ENABLE_*`` switch.

Those switches are tri-state:

    true   -> force on  (you promise the dependency is installed)
    false  -> force off (never register the tool / import the dep)
    auto   -> DEFAULT. Turn the feature on only if its dependency is
              actually present on this machine.

"auto" is what makes a bare ``git clone`` + ``pip install -r
requirements.txt`` work: features whose system package, Python package or
API key is missing quietly stay off instead of erroring on first use, and
``python3 doctor.py`` explains every decision.
"""

import math
import os
import shutil
import sys
from importlib.util import find_spec
from pathlib import Path
from typing import ClassVar

from dotenv.main import load_dotenv

APP_ROOT = Path(__file__).resolve().parent
ENV_FILE = Path(os.getenv("MAXWELL_ENV_FILE") or APP_ROOT / ".env")
# .env is the SOURCE OF TRUTH — always override whatever PM2/the shell
# injected. PM2 caches the env from first start and `--update-env` does
# NOT re-read the .env file, so without override=True every restart kept
# stale values (e.g. the old AI_FALLBACK_MODEL) forever.
load_dotenv(ENV_FILE, override=True)


# Deprecated environment aliases. Presence (including blank) wins for AI_*
# so credentials and optional routes can explicitly be cleared.
_AI_LEGACY_ENV = {
    "AI_BASE_URL": "OLLAMA_BASE_URL",
    "AI_API_KEY": "OLLAMA_API_KEY",
    "AI_MODEL": "OLLAMA_MODEL",
    "AI_TEMPERATURE": "OLLAMA_TEMPERATURE",
    "AI_DISABLE_REASONING": "OLLAMA_DISABLE_REASONING",
    "AI_REASONING_EFFORT": "OLLAMA_REASONING_EFFORT",
    "AI_FALLBACK_BASE_URL": "OLLAMA_FALLBACK_BASE_URL",
    "AI_FALLBACK_API_KEY": "OLLAMA_FALLBACK_API_KEY",
    "AI_FALLBACK_MODEL": "OLLAMA_FALLBACK_MODEL",
    "AI_FALLBACK_DISABLE_REASONING": "OLLAMA_FALLBACK_DISABLE_REASONING",
    "AI_FALLBACK_REASONING_EFFORT": "OLLAMA_FALLBACK_REASONING_EFFORT",
    "AI_VISION_BASE_URL": "OLLAMA_VISION_BASE_URL",
    "AI_VISION_API_KEY": "OLLAMA_VISION_API_KEY",
    "AI_VISION_MODEL": "OLLAMA_VISION_MODEL",
    "AI_VISION_DISABLE_REASONING": "OLLAMA_VISION_DISABLE_REASONING",
    "AI_RETRY_ATTEMPTS": "OLLAMA_RETRY_ATTEMPTS",
    "AI_EMPTY_RESPONSE_RETRIES": "OLLAMA_EMPTY_RESPONSE_RETRIES",
    "AI_ENDPOINT_COOLDOWN_SECONDS": "OLLAMA_ENDPOINT_COOLDOWN_SECONDS",
    "AI_MAX_OUTPUT_TOKENS": "OLLAMA_MAX_TOKENS",
}


def _env_name(name: str) -> str:
    if name in os.environ:
        return name
    return _AI_LEGACY_ENV.get(name, name)


def _int_env(
    name: str, default: int, min_value: int | None = None, max_value: int | None = None
) -> int:
    try:
        value = int(os.getenv(_env_name(name), str(default)))
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
        value = float(os.getenv(_env_name(name), str(default)))
    except (TypeError, ValueError):
        value = default
    if not math.isfinite(value):
        value = default
    if min_value is not None:
        value = max(min_value, value)
    if max_value is not None:
        value = min(max_value, value)
    return value


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(_env_name(name))
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _json_env(name: str) -> dict:
    """A JSON object out of an env var, or {} — a typo never stops startup.

    Used for the small override maps (X path templates). A malformed value
    is worth a log line at first use, not a crash on import, so it degrades
    to "no overrides".
    """
    raw = os.getenv(name, "").strip()
    if not raw:
        return {}
    try:
        import json

        value = json.loads(raw)
    except (TypeError, ValueError):
        print(f"warning: {name} is not valid JSON — ignoring it", file=sys.stderr)
        return {}
    return value if isinstance(value, dict) else {}


def _first_env(*names: str, default: str = "") -> str:
    """First non-empty value among ``names`` (aliases), else ``default``."""
    for name in names:
        value = os.getenv(name)
        if value is not None and value.strip():
            return value.strip()
    return default


# --- optional-feature detection -------------------------------------------
# Every check below is cheap and runs once, at import: find_spec() does NOT
# execute the module, and shutil.which() is a PATH scan. Restart to re-detect
# after installing something.

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _has_module(name: str) -> bool:
    try:
        return find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _has_binary(name: str) -> bool:
    """True if ``name`` is runnable: on PATH, or beside this interpreter.

    The second case matters for venv installs started by absolute
    interpreter path (PM2 does exactly that): the venv's bin/ holds the
    console scripts but is not on PATH.
    """
    if not name:
        return False
    if shutil.which(name):
        return True
    sibling = Path(sys.executable).parent / name
    return sibling.is_file() and os.access(sibling, os.X_OK)


# Human-readable reason for each feature decision, filled in by
# _feature_env(). Consumed by Config.feature_report() / doctor.py.
FEATURE_REASONS: dict[str, str] = {}


def _feature_env(
    name: str,
    detect=None,
    *,
    needs: str = "",
    default: bool = True,
    on_text: str = "",
    off_text: str = "",
) -> bool:
    """Resolve a tri-state ENABLE_* switch (true / false / auto).

    ``detect`` is a zero-arg callable returning True when the feature's
    dependency is available. With no ``detect`` the feature has no external
    dependency and ``auto`` means ``default``. Accepts the legacy plain
    booleans, so an existing .env keeps behaving exactly as before.
    """
    raw = (os.getenv(name) or "").strip().lower()
    if raw in _TRUE:
        FEATURE_REASONS[name] = f"forced on ({name}=true)"
        return True
    if raw in _FALSE:
        FEATURE_REASONS[name] = f"disabled ({name}=false)"
        return False
    # auto / unset / garbage
    if detect is None:
        FEATURE_REASONS[name] = "on by default" if default else "off by default"
        return default
    if detect():
        FEATURE_REASONS[name] = on_text or (
            f"auto: {needs} found" if needs else "auto: available"
        )
        return True
    FEATURE_REASONS[name] = off_text or (
        f"auto: off, {needs} not installed"
        if needs
        else "auto: off, dependency missing"
    )
    return False


class Config:
    # Official bot token from the Discord Developer Portal. Required.
    # DISCORD_TOKEN is a deprecated alias so older .env files that already
    # stored a bot token under that name keep working. User (self-bot)
    # tokens are not supported.
    DISCORD_BOT_TOKEN = (os.getenv("DISCORD_BOT_TOKEN") or "").strip()
    DISCORD_TOKEN = (os.getenv("DISCORD_TOKEN") or "").strip()
    MAXWELL_DEV_MODE = _bool_env("MAXWELL_DEV_MODE", False)
    # In-memory directed turn bound; overflow stays in the durable journal.
    MAX_PENDING_REPLY_REQUESTS = _int_env(
        "MAX_PENDING_REPLY_REQUESTS", 256, min_value=1, max_value=10000
    )
    MAXWELL_DEV_GUILD_IDS: ClassVar[frozenset[str]] = frozenset(
        item.strip()
        for item in os.getenv("MAXWELL_DEV_GUILD_IDS", "").split(",")
        if item.strip().isdigit()
    )

    AI_BASE_URL = _first_env(
        "AI_BASE_URL", "AI_API_URL", "OLLAMA_BASE_URL", default="http://localhost:11434"
    )
    # An explicitly blank friendly key clears a stale legacy credential.
    AI_API_KEY = os.getenv(
        "AI_API_KEY",
        os.getenv("OLLAMA_API_KEY", os.getenv("OPENAI_COMPAT_API_KEY", "")),
    ).strip()
    # No default model on purpose: a hardcoded one that your endpoint does
    # not serve fails later, as an opaque 404 from the provider. Empty fails
    # at startup with a sentence that says what to do.
    AI_MODEL = _first_env("AI_MODEL", "OLLAMA_MODEL")
    # max_tokens = max *output* tokens per completion (not context window).
    # minimax-m3 allows huge context but caps output ~131072; 8192 is a sane default.
    AI_MAX_OUTPUT_TOKENS = _int_env(
        "AI_MAX_OUTPUT_TOKENS", 16384, min_value=1, max_value=131072
    )
    # 0.7 rather than 1.0: at 1.0 the tail of the distribution is wide enough
    # that a chat model wanders — it picks a tic and rides it, which is the
    # generation-side half of the repetition the scrubber cleans up after.
    # 0.7 keeps him improvising without letting the sampler pick the odd token
    # for its own sake. Raise it back per-install with AI_TEMPERATURE.
    AI_TEMPERATURE = _float_env("AI_TEMPERATURE", 0.7, min_value=0.0)
    AI_DISABLE_REASONING = _bool_env("AI_DISABLE_REASONING", True)
    # Optional chat-completions reasoning_effort (e.g. "low"). Ignored when
    # AI_DISABLE_REASONING is on. Blank leaves the field off the payload.
    AI_REASONING_EFFORT = os.getenv(_env_name("AI_REASONING_EFFORT"), "").strip()
    AI_FALLBACK_BASE_URL = os.getenv(_env_name("AI_FALLBACK_BASE_URL"), "").strip()
    AI_FALLBACK_API_KEY = os.getenv(_env_name("AI_FALLBACK_API_KEY"), "").strip()
    AI_FALLBACK_MODEL = os.getenv(_env_name("AI_FALLBACK_MODEL"), "").strip()
    AI_FALLBACK_DISABLE_REASONING = _bool_env("AI_FALLBACK_DISABLE_REASONING", True)
    AI_FALLBACK_REASONING_EFFORT = os.getenv(
        _env_name("AI_FALLBACK_REASONING_EFFORT"), ""
    ).strip()
    # Optional vision/omni model for image/video (and audio, if enabled) turns.
    # Text-only primaries like deepseek-v4-flash 400 on image_url; when this is
    # set, media requests go here first. Blank base/key inherit the primary.
    AI_VISION_BASE_URL = os.getenv(_env_name("AI_VISION_BASE_URL"), "").strip()
    AI_VISION_API_KEY = os.getenv(_env_name("AI_VISION_API_KEY"), "").strip()
    AI_VISION_MODEL = os.getenv(_env_name("AI_VISION_MODEL"), "").strip()
    AI_VISION_DISABLE_REASONING = _bool_env("AI_VISION_DISABLE_REASONING", True)
    AI_RETRY_ATTEMPTS = _int_env("AI_RETRY_ATTEMPTS", 3, min_value=1, max_value=10)
    # Extra bounded recovery attempts for HTTP 200 responses that contain
    # neither assistant text nor tool calls. These use a different endpoint
    # when available and a non-streaming request to bypass flaky SSE gateways.
    AI_EMPTY_RESPONSE_RETRIES = _int_env(
        "AI_EMPTY_RESPONSE_RETRIES", 2, min_value=0, max_value=5
    )

    # Deprecated Python configuration aliases for third-party integrations.
    OLLAMA_BASE_URL = AI_BASE_URL
    OLLAMA_API_KEY = AI_API_KEY
    OLLAMA_MODEL = AI_MODEL
    OLLAMA_TEMPERATURE = AI_TEMPERATURE
    OLLAMA_DISABLE_REASONING = AI_DISABLE_REASONING
    OLLAMA_REASONING_EFFORT = AI_REASONING_EFFORT
    OLLAMA_FALLBACK_BASE_URL = AI_FALLBACK_BASE_URL
    OLLAMA_FALLBACK_API_KEY = AI_FALLBACK_API_KEY
    OLLAMA_FALLBACK_MODEL = AI_FALLBACK_MODEL
    OLLAMA_FALLBACK_DISABLE_REASONING = AI_FALLBACK_DISABLE_REASONING
    OLLAMA_FALLBACK_REASONING_EFFORT = AI_FALLBACK_REASONING_EFFORT
    OLLAMA_VISION_BASE_URL = AI_VISION_BASE_URL
    OLLAMA_VISION_API_KEY = AI_VISION_API_KEY
    OLLAMA_VISION_MODEL = AI_VISION_MODEL
    OLLAMA_VISION_DISABLE_REASONING = AI_VISION_DISABLE_REASONING
    OLLAMA_RETRY_ATTEMPTS = AI_RETRY_ATTEMPTS
    OLLAMA_EMPTY_RESPONSE_RETRIES = AI_EMPTY_RESPONSE_RETRIES
    OLLAMA_MAX_TOKENS = AI_MAX_OUTPUT_TOKENS

    # Toggle for "omni" (audio+vision capable) model input. On by default:
    # Gemini behind the current proxy transcribes wav/mp3; endpoints that
    # 400 on input_audio fall back to text-only via the media-incapable path.
    # Dashboard process_audio can still turn it off at runtime.
    ENABLE_AUDIO_INPUT = _feature_env("ENABLE_AUDIO_INPUT", default=True)

    # -------------------------------------------------------------------------
    # Optional features (true / false / auto — see the module docstring).
    #
    # Unset means "auto": the feature turns itself on only when whatever it
    # needs is actually installed. A bare clone with nothing but ffmpeg
    # missing loses video frames, not the whole bot. Read once at import
    # time; restart to re-detect.
    # -------------------------------------------------------------------------

    # No external dependency — pure code paths, on by default.
    ENABLE_IMAGE_INPUT = _feature_env("ENABLE_IMAGE_INPUT")
    ENABLE_FETCH_URL = _feature_env("ENABLE_FETCH_URL")
    ENABLE_CREATE_SITE = _feature_env(
        "ENABLE_CREATE_SITE",
        lambda: bool(os.getenv("MAXWELL_PUBLIC_BASE_URL", "").strip()),
        needs="a configured MAXWELL_PUBLIC_BASE_URL",
    )
    ENABLE_AVATAR = _feature_env("ENABLE_AVATAR")

    ENABLE_AUTONOMY = _feature_env("ENABLE_AUTONOMY", default=False)
    # Keyless image generation is available without setup. HD generation
    # needs a model explicitly configured on the operator's provider.
    ENABLE_IMAGE_GEN = _feature_env("ENABLE_IMAGE_GEN")
    ENABLE_HD_IMAGE = _feature_env(
        "ENABLE_HD_IMAGE",
        lambda: bool(os.getenv("GEMINI_IMAGE_MODEL", "").strip()),
        needs="a configured GEMINI_IMAGE_MODEL",
    )

    # Needs a system binary or Python package.
    ENABLE_VIDEO_INPUT = _feature_env(
        "ENABLE_VIDEO_INPUT", lambda: _has_binary("ffmpeg"), needs="ffmpeg"
    )
    SEARXNG_URL = os.getenv("SEARXNG_URL", "").strip()
    TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "").strip()
    WEB_SEARCH_TIMEOUT = _float_env("WEB_SEARCH_TIMEOUT", 12.0, 2.0, 30.0)
    WEB_SEARCH_CONCURRENCY = _int_env("WEB_SEARCH_CONCURRENCY", 4, 1, 16)
    WEB_SEARCH_MAX_PENDING = _int_env("WEB_SEARCH_MAX_PENDING", 32, 1, 256)
    WEB_SEARCH_CACHE_SIZE = _int_env("WEB_SEARCH_CACHE_SIZE", 256, 0, 2048)
    WEB_FETCH_TIMEOUT = _float_env("WEB_FETCH_TIMEOUT", 12.0, 2.0, 30.0)
    ENABLE_WEB_SEARCH = _feature_env(
        "ENABLE_WEB_SEARCH",
        lambda: _has_module("ddgs") or bool(os.getenv("SEARXNG_URL", "").strip() or os.getenv("TAVILY_API_KEY", "").strip()),
        needs="ddgs or a configured SearXNG/Tavily provider",
    )
    # Optional Discord ID of this bot's user account. Empty on a fresh clone
    # so no one else's snowflake is inherited.
    MAXWELL_USER_ID = os.getenv("MAXWELL_USER_ID", "").strip()
    # Owner / display identity. Names default to the project labels; IDs and
    # the human creator name stay blank until the operator sets them.
    CREATOR_NAME = os.getenv("CREATOR_NAME", "").strip()
    CREATOR_ID = os.getenv("CREATOR_ID", "").strip()
    BOT_NAME = os.getenv("BOT_NAME", "Maxwell").strip() or "Maxwell"
    COMMAND_PREFIX = os.getenv("COMMAND_PREFIX", ",").strip() or ","
    BOT_BIRTHDAY = os.getenv("BOT_BIRTHDAY", "2026-05-21").strip() or "2026-05-21"
    BOT_INVITE_URL = os.getenv(
        "BOT_INVITE_URL", os.getenv("OFFICIAL_INVITE", "")
    ).strip()
    MAXWELL_USAGE_URL = os.getenv("MAXWELL_USAGE_URL", "").strip()
    # Public hosted operators may retain the retired-tool restrictions.
    MAXWELL_RESTRICT_PUBLIC_RUNTIME = _bool_env("MAXWELL_RESTRICT_PUBLIC_RUNTIME", False)

    # Email needs a real mailbox. Without a password the four tools could
    # only ever answer "not configured", so auto keeps them unregistered.
    ENABLE_EMAIL_TOOLS = _feature_env(
        "ENABLE_EMAIL_TOOLS",
        lambda: bool(os.getenv("MAXWELL_EMAIL_USER", "").strip()
                     and os.getenv("MAXWELL_EMAIL_PASSWORD", "").strip()),
        needs="MAXWELL_EMAIL_USER and MAXWELL_EMAIL_PASSWORD",
    )

    # The installer enables this when --with-shell prepares the backend.
    ENABLE_SHELL = _feature_env("ENABLE_SHELL", default=False)

    # RAG vector memory. Needs a reachable embedding endpoint (see
    # EMBED_* below); without one the bot still works, it just loses
    # semantic recall and falls back to recent-history context.
    ENABLE_RAG = _feature_env(
        "ENABLE_RAG",
        lambda: bool(_first_env("MAXWELL_EMBED_MODEL", "EMBED_MODEL")),
        needs="a configured embedding model",
    )
    RAG_WEB_STORE_ENABLED = _bool_env("RAG_WEB_STORE_ENABLED", True)

    # -------------------------------------------------------------------------
    # Embeddings for RAG memory. Defaults target a local Ollama, but any
    # OpenAI-compatible /v1/embeddings endpoint works — set EMBED_BASE_URL
    # to e.g. https://api.openai.com/v1 with EMBED_MODEL/EMBED_DIM to match.
    # -------------------------------------------------------------------------
    EMBED_BASE_URL = _first_env(
        "MAXWELL_EMBED_BASE_URL", "EMBED_BASE_URL", default="http://localhost:11434"
    ).rstrip("/")
    EMBED_MODEL = _first_env(
        "MAXWELL_EMBED_MODEL", "EMBED_MODEL", default="qwen3-embedding:0.6b"
    )
    EMBED_API_KEY = _first_env("MAXWELL_EMBED_API_KEY", "EMBED_API_KEY")
    EMBED_DIM = _int_env("MAXWELL_EMBED_DIM", 1024, min_value=8, max_value=16384)

    # When false (default), shell refuses to run on a turn
    # that read untrusted fetched content (URLs, web search). A new clean user
    # request is required after tainted content. This blocks indirect prompt
    # injection from turning a fetched page into a shell command.
    # Set to true to skip the gate entirely — the model can call shell
    # after fetch_url/web_search without confirmation. Only do this if
    # you trust the model fully (single-user homelab install).
    DISABLE_TAINT_GATE = _bool_env("DISABLE_TAINT_GATE", False)

    # Optional secondary auth fallback for the primary LLM endpoint.
    OPENAI_COMPAT_API_KEY = os.getenv("OPENAI_COMPAT_API_KEY", "").strip()

    AUTONOMY_BASE_URL = os.getenv("AUTONOMY_BASE_URL", "").strip()
    AUTONOMY_API_KEY = os.getenv(
        "AUTONOMY_API_KEY", os.getenv("OPENAI_COMPAT_API_KEY", "")
    ).strip()
    AUTONOMY_MODEL = os.getenv("AUTONOMY_MODEL", "").strip()
    AUTONOMY_DISABLE_REASONING = _bool_env("AUTONOMY_DISABLE_REASONING", False)


    # Live tool progress messages are on by default. A per-server
    # `/progress arguments:off` silences a noisy server; MAXWELL_PROGRESS_MESSAGES=false
    # disables the baseline globally. DMs never get them. See tool_progress.py.
    PROGRESS_MESSAGES = _bool_env("MAXWELL_PROGRESS_MESSAGES", True)

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

    # image_generator runs on Pollinations. SDXL-Lightning is fast (~1-2s) and
    # high quality; the old default (flux) was both slower and less consistent.
    POLLINATIONS_MODEL = os.getenv("POLLINATIONS_MODEL", "MarcosFRG/sdxl-lightning")

    NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY", "")
    NVIDIA_IMAGE_URL = os.getenv(
        "NVIDIA_IMAGE_URL",
        "https://ai.api.nvidia.com/v1/genai/black-forest-labs/flux.1-dev",
    )

    # Legacy ChatGPT2API image endpoint. Kept only so an existing .env does
    # not error on load — hd_image no longer uses it (that host dropped every
    # image model and now 404s on /v1/images/generations).
    GPT_IMAGE_URL = os.getenv("GPT_IMAGE_URL", "")
    GPT_IMAGE_API_KEY = os.getenv("GPT_IMAGE_API_KEY", "")

    # hd_image: Gemini image model on the OpenAI-compatible endpoint. Blank
    # base/key inherit the primary chat endpoint (AI_*), which is where
    # the image model lives anyway — one key, one host.
    #
    # Generation goes through /chat/completions rather than
    # /images/generations because only the chat route accepts an input image
    # (the /images/edits route on this gateway ignores `model` and pins
    # gemini-3-pro-image, which has no quota). The chat route returns images
    # as markdown data-URIs in message.content.
    GEMINI_IMAGE_BASE_URL = os.getenv("GEMINI_IMAGE_BASE_URL", "").strip()
    GEMINI_IMAGE_API_KEY = os.getenv("GEMINI_IMAGE_API_KEY", "").strip()
    GEMINI_IMAGE_MODEL = (
        os.getenv("GEMINI_IMAGE_MODEL", "").strip() or "gemini-3.1-flash-image"
    )
    # Input images are downscaled to this longest edge before upload. Payload
    # size dominates latency on this endpoint: a 629KB input took 89s where
    # the same edit with a 64KB input took 20s.
    GEMINI_IMAGE_MAX_INPUT_EDGE = _int_env(
        "GEMINI_IMAGE_MAX_INPUT_EDGE", 1024, min_value=256, max_value=4096
    )
    GEMINI_IMAGE_TIMEOUT = _int_env(
        "GEMINI_IMAGE_TIMEOUT", 300, min_value=30, max_value=900
    )

    MEMORY_MESSAGE_LIMIT = _int_env(
        "MEMORY_MESSAGE_LIMIT", 2000, min_value=1, max_value=10000
    )


    DATA_DIR = os.getenv("DATA_DIR", "data")
    LOGS_DIR = os.getenv("LOGS_DIR", os.getenv("LOGS", "logs"))
    LOG_LEVEL = os.getenv("LOG_LEVEL", "info")
    # Host path of this checkout when Maxwell runs in Docker. Sibling
    # containers (shell sandbox, site backends) bind-mount through the
    # host daemon, so in-container paths like /app must be rewritten.
    MAXWELL_HOST_BIND = os.getenv("MAXWELL_HOST_BIND", "").strip()

    MAXWELL_SITE_DIR = os.getenv("MAXWELL_SITE_DIR") or "public/bot"
    MAXWELL_PUBLIC_BASE_URL = os.getenv(
        "MAXWELL_PUBLIC_BASE_URL", ""
    ).strip()
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
    MAXWELL_EMAIL_USER = os.getenv("MAXWELL_EMAIL_USER", "").strip()
    MAXWELL_EMAIL_PASSWORD = os.getenv("MAXWELL_EMAIL_PASSWORD", "").strip()
    # Blank From: falls back to the mailbox itself — one less thing to fill in.
    MAXWELL_EMAIL_FROM = (
        os.getenv("MAXWELL_EMAIL_FROM", "").strip() or MAXWELL_EMAIL_USER
    )
    MAXWELL_EMAIL_FROM_NAME = (
        os.getenv("MAXWELL_EMAIL_FROM_NAME", BOT_NAME).strip() or BOT_NAME
    )
    # Senders whose mail is never filed as an inbox notice. Comma-separated;
    # a full address, or a leading-dot domain (".google.com") for it and its
    # subdomains. Empty by default: which machine mail matters is the
    # operator's call. A DMARC aggregate report is pure telemetry, but a
    # MAILER-DAEMON bounce means something he sent did not arrive, and a
    # heuristic cannot tell those apart. The mail itself is untouched — it
    # stays on the server and the email_* tools still read it.
    MAXWELL_EMAIL_IGNORE_SENDERS = os.getenv("MAXWELL_EMAIL_IGNORE_SENDERS", "").strip()

    # Admin / owner allowlists. Re-exported here so Config is the single
    # source of truth; bot_tools.refresh_owner_ids() still does a runtime
    # reload but the initial parse lives here.
    MAXWELL_ADMIN_USER = os.getenv("MAXWELL_ADMIN_USER", "admin").strip()
    MAXWELL_ADMIN_PASSWORD = os.getenv("MAXWELL_ADMIN_PASSWORD", "").strip()
    MAXWELL_OWNER_IDS: ClassVar[set[str]] = {
        item.strip()
        for item in os.getenv("MAXWELL_OWNER_IDS", "").split(",")
        if item.strip()
    }

    # Every optional feature, in the order doctor.py and the startup log
    # print them. (attribute, human label).
    FEATURE_SWITCHES = (
        ("ENABLE_IMAGE_INPUT", "image input (vision)"),
        ("ENABLE_VIDEO_INPUT", "video input (frame extraction)"),
        ("ENABLE_AUDIO_INPUT", "audio input (omni models)"),
        ("ENABLE_IMAGE_GEN", "image generation"),
        ("ENABLE_HD_IMAGE", "HD image generation"),
        ("ENABLE_WEB_SEARCH", "web search"),
        ("ENABLE_FETCH_URL", "fetch_url"),
        ("ENABLE_CREATE_SITE", "site generation"),
        ("ENABLE_AVATAR", "avatar changes"),
        ("ENABLE_EMAIL_TOOLS", "email tools"),
        ("ENABLE_SHELL", "shell (docker sandbox)"),
        ("ENABLE_RAG", "RAG vector memory"),
        ("ENABLE_AUTONOMY", "autonomy engine"),
    )

    @classmethod
    def feature_report(cls) -> list[tuple[str, str, bool, str]]:
        """(env name, label, enabled, reason) for every optional feature."""
        report = []
        for name, label in cls.FEATURE_SWITCHES:
            enabled = bool(getattr(cls, name, False))
            reason = FEATURE_REASONS.get(name, "")
            if not reason:
                reason = "set in .env" if os.getenv(name) else "default"
            report.append((name, label, enabled, reason))
        return report

    @classmethod
    def validate(cls):
        # The only two hard requirements. Anything else has a default or
        # degrades to "feature off", which is the whole point of the
        # ENABLE_*=auto design.
        if not cls.DISCORD_BOT_TOKEN and not cls.DISCORD_TOKEN:
            raise ValueError(
                "DISCORD_BOT_TOKEN is required (official Discord bot token from "
                "the Developer Portal). Run ./setup.sh, or set it in .env, then "
                "start the bot again. Self-bot user tokens are not supported."
            )
        if cls.MAXWELL_DEV_MODE and not cls.MAXWELL_DEV_GUILD_IDS:
            raise ValueError(
                "MAXWELL_DEV_GUILD_IDS must contain at least one Discord guild ID "
                "when MAXWELL_DEV_MODE=true."
            )
        if not cls.AI_BASE_URL:
            raise ValueError(
                "AI_BASE_URL is required — point it at any OpenAI-compatible "
                "endpoint (local Ollama, OpenRouter, LM Studio, ...)."
            )
        if not cls.AI_MODEL:
            raise ValueError(
                "AI_MODEL is required — set the model name your endpoint serves."
            )
        if cls.AI_MAX_OUTPUT_TOKENS < 1:
            raise ValueError("AI_MAX_OUTPUT_TOKENS must be >= 1")

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
                "MAXWELL_OWNER_IDS is empty — restricted /diagnostics and "
                "/maintenance commands will be denied to everyone. Set your "
                "Discord user ID in .env."
            )
        if cls.ENABLE_EMAIL_TOOLS and not cls.MAXWELL_EMAIL_PASSWORD:
            _log.warning(
                "ENABLE_EMAIL_TOOLS=true but MAXWELL_EMAIL_PASSWORD is empty — "
                "the email tools will return a 'not configured' error on every "
                "call. Either set MAXWELL_EMAIL_PASSWORD or set "
                "ENABLE_EMAIL_TOOLS=false."
            )

        if cls.ENABLE_SHELL:
            _log.warning(
                "ENABLE_SHELL is on — the model can run commands on this host "
                "as the bot user. Set ENABLE_SHELL=false in .env if you did "
                "not mean to grant that."
            )

        # One line per optional feature, so "why isn't X working" is answered
        # by the top of the log instead of by reading the source.
        on = [label for _, label, enabled, _ in cls.feature_report() if enabled]
        off = [
            f"{label} ({reason})"
            for _, label, enabled, reason in cls.feature_report()
            if not enabled
        ]
        _log.info("Features on: %s", ", ".join(on) or "none")
        if off:
            _log.info("Features off: %s", "; ".join(off))
