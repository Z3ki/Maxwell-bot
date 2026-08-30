"""Configuration management for Maxwell Bot.

Design note — optional features
-------------------------------
Maxwell only *requires* two things: a Discord token and an OpenAI-compatible
model endpoint. Everything else (voice, YouTube, web search, TTS, email,
video frames, RAG embeddings) is optional and gated behind an ``ENABLE_*``
switch.

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

from dotenv.main import load_dotenv

APP_ROOT = Path(__file__).resolve().parent
ENV_FILE = Path(os.getenv("MAXWELL_ENV_FILE", APP_ROOT / ".env"))
# .env is the SOURCE OF TRUTH — always override whatever PM2/the shell
# injected. PM2 caches the env from first start and `--update-env` does
# NOT re-read the .env file, so without override=True every restart kept
# stale values (e.g. the old OLLAMA_FALLBACK_MODEL) forever.
load_dotenv(ENV_FILE, override=True)


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
    if not math.isfinite(value):
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
        FEATURE_REASONS[name] = (
            on_text or (f"auto: {needs} found" if needs else "auto: available")
        )
        return True
    FEATURE_REASONS[name] = off_text or (
        f"auto: off, {needs} not installed" if needs else "auto: off, dependency missing"
    )
    return False


class Config:
    DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
    TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
    TELEGRAM_WEBHOOK_URL = os.getenv("TELEGRAM_WEBHOOK_URL", "").strip()
    TELEGRAM_WEBHOOK_PORT = _int_env(
        "TELEGRAM_WEBHOOK_PORT", 8443, min_value=1024, max_value=65535
    )

    OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    OLLAMA_API_KEY = os.getenv("OLLAMA_API_KEY", os.getenv("OPENAI_COMPAT_API_KEY", ""))
    # No default model on purpose: a hardcoded one that your endpoint does
    # not serve fails later, as an opaque 404 from the provider. Empty fails
    # at startup with a sentence that says what to do.
    OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "").strip()
    OLLAMA_REM_MODEL = os.getenv("OLLAMA_REM_MODEL") or OLLAMA_MODEL
    # max_tokens = max *output* tokens per completion (not context window).
    # minimax-m3 allows huge context but caps output ~131072; 8192 is a sane default.
    OLLAMA_MAX_TOKENS = _int_env(
        "OLLAMA_MAX_TOKENS", 16384, min_value=1, max_value=131072
    )
    # 0.7 rather than 1.0: at 1.0 the tail of the distribution is wide enough
    # that a chat model wanders — it picks a tic and rides it, which is the
    # generation-side half of the repetition the scrubber cleans up after.
    # 0.7 keeps him improvising without letting the sampler pick the odd token
    # for its own sake. Raise it back per-install with OLLAMA_TEMPERATURE.
    OLLAMA_TEMPERATURE = _float_env("OLLAMA_TEMPERATURE", 0.7, min_value=0.0)
    OLLAMA_DISABLE_REASONING = _bool_env("OLLAMA_DISABLE_REASONING", True)
    OLLAMA_FALLBACK_BASE_URL = os.getenv("OLLAMA_FALLBACK_BASE_URL", "").strip()
    OLLAMA_FALLBACK_API_KEY = os.getenv("OLLAMA_FALLBACK_API_KEY", "").strip()
    OLLAMA_FALLBACK_MODEL = os.getenv("OLLAMA_FALLBACK_MODEL", "").strip()
    OLLAMA_FALLBACK_DISABLE_REASONING = _bool_env(
        "OLLAMA_FALLBACK_DISABLE_REASONING", True
    )
    # Optional vision/omni model for image/video (and audio, if enabled) turns.
    # Text-only primaries like deepseek-v4-flash 400 on image_url; when this is
    # set, media requests go here first. Blank base/key inherit the primary.
    OLLAMA_VISION_BASE_URL = os.getenv("OLLAMA_VISION_BASE_URL", "").strip()
    OLLAMA_VISION_API_KEY = os.getenv("OLLAMA_VISION_API_KEY", "").strip()
    OLLAMA_VISION_MODEL = os.getenv("OLLAMA_VISION_MODEL", "").strip()
    OLLAMA_VISION_DISABLE_REASONING = _bool_env("OLLAMA_VISION_DISABLE_REASONING", True)
    OLLAMA_RETRY_ATTEMPTS = _int_env(
        "OLLAMA_RETRY_ATTEMPTS", 3, min_value=1, max_value=10
    )

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
    ENABLE_CREATE_SITE = _feature_env("ENABLE_CREATE_SITE")
    ENABLE_AVATAR = _feature_env("ENABLE_AVATAR")
    ENABLE_TELEGRAM = _feature_env("ENABLE_TELEGRAM")
    ENABLE_AUTONOMY = _feature_env("ENABLE_AUTONOMY")
    # image_generator uses Pollinations (free, keyless); hd_image needs an
    # NVIDIA key but degrades to a clear error instead of breaking the tool.
    ENABLE_IMAGE_GEN = _feature_env("ENABLE_IMAGE_GEN")

    # Needs a system binary or Python package.
    ENABLE_VIDEO_INPUT = _feature_env(
        "ENABLE_VIDEO_INPUT", lambda: _has_binary("ffmpeg"), needs="ffmpeg"
    )
    # The tool shells out to the yt-dlp binary, so the binary is what counts.
    ENABLE_YOUTUBE = _feature_env(
        "ENABLE_YOUTUBE", lambda: _has_binary("yt-dlp"), needs="the yt-dlp binary"
    )
    ENABLE_WEB_SEARCH = _feature_env(
        "ENABLE_WEB_SEARCH", lambda: _has_module("ddgs"), needs="the ddgs package"
    )
    ENABLE_VC = _feature_env(
        "ENABLE_VC",
        lambda: _has_module("discord.ext.voice_recv") and _has_module("nacl"),
        needs="discord-ext-voice-recv + PyNaCl",
    )
    # TTS works through any one of: Fish (key), NVIDIA Riva (key), gTTS
    # (package), espeak (binary). Off only when none of them exist.
    ENABLE_TTS = _feature_env(
        "ENABLE_TTS",
        lambda: bool(
            os.getenv("FISH_API_KEY", "").strip()
            or os.getenv("NVIDIA_API_KEY", "").strip()
            or _has_module("gtts")
            or _has_binary("espeak-ng")
            or _has_binary("espeak")
        ),
        needs="a TTS engine (espeak-ng, gTTS, or a Fish/NVIDIA key)",
    )
    # Playing TTS into a voice channel additionally needs ffmpeg.
    ENABLE_TTS_VC = _feature_env(
        "ENABLE_TTS_VC",
        lambda _tts=ENABLE_TTS: _tts and _has_binary("ffmpeg"),
        needs="ffmpeg + a TTS engine",
    )
    # Maxwell Companion / Partner
    GF_DISCORD_TOKEN = os.getenv("GF_DISCORD_TOKEN", "").strip()
    GF_USER_ID = os.getenv("GF_USER_ID", "1496154562715848763").strip()
    MAXWELL_USER_ID = os.getenv("MAXWELL_USER_ID", "1382894657624866889").strip()
    PARTNER_USER_ID = os.getenv("PARTNER_USER_ID", "").strip()
    # Partner-to-partner replies are intentionally finite.  A human message
    # resets the budget; silence resets it after the configured window.
    PARTNER_MAX_AUTO_TURNS = _int_env(
        "PARTNER_MAX_AUTO_TURNS", 2, min_value=1, max_value=20
    )
    PARTNER_TURN_WINDOW_SECONDS = _float_env(
        "PARTNER_TURN_WINDOW_SECONDS", 60.0, min_value=5.0, max_value=3600.0
    )
    BOT_PERSONA_TYPE = os.getenv("BOT_PERSONA_TYPE", "maxwell").strip().lower()
    CREATOR_NAME = os.getenv("CREATOR_NAME", "Z3ki").strip() or "Z3ki"
    CREATOR_ID = os.getenv("CREATOR_ID", "1471821513824014480").strip() or "1471821513824014480"
    BOT_NAME = os.getenv("BOT_NAME", "Maxwell").strip() or "Maxwell"
    PARTNER_NAME = os.getenv("PARTNER_NAME", "Uni").strip() or "Uni"

    # Email needs a real mailbox. Without a password the four tools could
    # only ever answer "not configured", so auto keeps them unregistered.
    ENABLE_EMAIL_TOOLS = _feature_env(
        "ENABLE_EMAIL_TOOLS",
        lambda: bool(os.getenv("MAXWELL_EMAIL_PASSWORD", "").strip()),
        on_text="auto: MAXWELL_EMAIL_PASSWORD is set",
        off_text="auto: off, no MAXWELL_EMAIL_PASSWORD",
    )

    # X (Twitter). Reading is free and needs no account at all — X's own
    # embed backend and any Nitter/RSSHub instance serve public profiles,
    # posts and searches — so `auto` is on. Posting needs the session
    # cookies below; without them the read half still works and x_post says
    # what is missing.
    # No detector: there is no dependency to find. Public reads need no
    # credentials at all, so `auto` means on and posting simply stays
    # unavailable until X_AUTH_TOKEN/X_CT0 are set.
    ENABLE_X = _feature_env("ENABLE_X")

    # Host access. Kept on by default for parity with older installs, but
    # this is THE security-relevant switch: `shell` runs commands as the bot
    # user. validate() warns loudly at startup so it is never a surprise.
    ENABLE_SHELL = _feature_env("ENABLE_SHELL")
    # Native sub-agent: Maxwell spawns a nested copy of itself (same
    # provider, restricted toolset, its own workdir) to work a coding task
    # to completion. It writes and runs code, so it inherits ENABLE_SHELL's
    # trust decision unless set explicitly.
    ENABLE_SUBAGENT = _feature_env(
        "ENABLE_SUBAGENT",
        lambda _sh=ENABLE_SHELL: _sh,
        on_text="auto: follows ENABLE_SHELL",
        off_text="auto: off, follows ENABLE_SHELL=false",
    )

    # RAG vector memory. Needs a reachable embedding endpoint (see
    # EMBED_* below); without one the bot still works, it just loses
    # semantic recall and falls back to recent-history context.
    ENABLE_RAG = _feature_env("ENABLE_RAG")
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

    # Live tool progress messages. OFF by default: a per-server `,progress on`
    # opts a server in, and MAXWELL_PROGRESS_MESSAGES=true enables it for every
    # server as a baseline. The sub-agent does NOT post a per-step "working on
    # it" heartbeat anymore — Maxwell watches the run on the event bus via
    # sub_agent_status and reports/steers it itself; only the final report lands
    # in the channel. `,progress off` silences a noisy server even under the env
    # baseline; DMs never get them. See tool_progress.py.
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

    # Discord join-captcha handling. Discord sometimes challenges an invite
    # accept (or other API action) with an hCaptcha — surfaced by the library
    # as discord.CaptchaRequired. When CAPTCHA_SOLVER_SERVICE+API_KEY are set,
    # the bot solves the challenge via the external service and auto-retries.
    # When unset, the challenge details are surfaced in the tool result so the
    # user sees exactly why the join failed. Supported services: capsolver,
    # 2captcha.
    CAPTCHA_SOLVER_SERVICE = os.getenv("CAPTCHA_SOLVER_SERVICE", "").strip().lower()
    CAPTCHA_SOLVER_API_KEY = os.getenv("CAPTCHA_SOLVER_API_KEY", "").strip()
    CAPTCHA_SOLVER_TIMEOUT = _int_env(
        "CAPTCHA_SOLVER_TIMEOUT", 180, min_value=10, max_value=600
    )

    # Human-in-the-loop captcha solving. When CAPTCHA_SOLVER_SERVICE is unset
    # (or fails), the bot hosts a one-shot hCaptcha solve page and DMs the
    # link to the owner (any CAPTCHA hit: joins, DM gates, phone checks).
    # The token is bound to Discord's sitekey+rqdata, not the solver, so
    # anyone who opens the link can complete it. CAPTCHA_FALLBACK_USER_ID is
    # DM'd when no admin is resolvable.
    CAPTCHA_HUMAN_SOLVE = _bool_env("CAPTCHA_HUMAN_SOLVE", True)
    CAPTCHA_HUMAN_HOST = os.getenv("CAPTCHA_HUMAN_HOST", "127.0.0.1").strip()
    CAPTCHA_HUMAN_PORT = _int_env(
        "CAPTCHA_HUMAN_PORT", 8790, min_value=1, max_value=65535
    )
    CAPTCHA_FALLBACK_USER_ID = os.getenv("CAPTCHA_FALLBACK_USER_ID", "").strip()

    # image_generator runs on Pollinations. SDXL-Lightning is fast (~1-2s) and
    # high quality; the old default (flux) was both slower and less consistent.
    POLLINATIONS_MODEL = os.getenv("POLLINATIONS_MODEL", "MarcosFRG/sdxl-lightning")

    NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY", "")
    NVIDIA_IMAGE_URL = os.getenv(
        "NVIDIA_IMAGE_URL",
        "https://ai.api.nvidia.com/v1/genai/black-forest-labs/flux.1-dev",
    )
    # NVIDIA Riva ASR (Parakeet) for live VC transcription. Whisper is too
    # slow for this path; VC utterances go through Riva then the text model.
    ASR_RIVA_FUNCTION_ID = os.getenv(
        "ASR_RIVA_FUNCTION_ID", "1598d209-5e27-4d3c-8079-4751568b1081"
    ).strip()
    ASR_RIVA_LANGUAGE = os.getenv("ASR_RIVA_LANGUAGE", "en-US").strip() or "en-US"

    # Legacy ChatGPT2API image endpoint. Kept only so an existing .env does
    # not error on load — hd_image no longer uses it (that host dropped every
    # image model and now 404s on /v1/images/generations).
    GPT_IMAGE_URL = os.getenv("GPT_IMAGE_URL", "")
    GPT_IMAGE_API_KEY = os.getenv("GPT_IMAGE_API_KEY", "")

    # hd_image: Gemini image model on the OpenAI-compatible endpoint. Blank
    # base/key inherit the primary chat endpoint (OLLAMA_*), which is where
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
    # REM is a background LLM loop: it spends tokens on its own schedule.
    # Opt-in, so a fresh install never quietly bills you. `ENABLE_REM` is
    # accepted as an alias because that is the name the docs always used.
    REM_ENABLED = _bool_env("REM_ENABLED", _bool_env("ENABLE_REM", False))
    FEATURE_REASONS["REM_ENABLED"] = (
        "enabled in .env" if REM_ENABLED else "off by default (opt in with ENABLE_REM=true)"
    )
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
    MAXWELL_EMAIL_USER = os.getenv("MAXWELL_EMAIL_USER", "").strip()
    MAXWELL_EMAIL_PASSWORD = os.getenv("MAXWELL_EMAIL_PASSWORD", "").strip()
    # Blank From: falls back to the mailbox itself — one less thing to fill in.
    MAXWELL_EMAIL_FROM = (
        os.getenv("MAXWELL_EMAIL_FROM", "").strip() or MAXWELL_EMAIL_USER
    )
    MAXWELL_EMAIL_FROM_NAME = os.getenv("MAXWELL_EMAIL_FROM_NAME", "Maxwell").strip()
    # Senders whose mail is never filed as an inbox notice. Comma-separated;
    # a full address, or a leading-dot domain (".google.com") for it and its
    # subdomains. Empty by default: which machine mail matters is the
    # operator's call. A DMARC aggregate report is pure telemetry, but a
    # MAILER-DAEMON bounce means something he sent did not arrive, and a
    # heuristic cannot tell those apart. The mail itself is untouched — it
    # stays on the server and the email_* tools still read it.
    # -------------------------------------------------------------------------
    # X (Twitter). Two cookies out of a logged-in browser tab are the whole
    # of the write credential; everything else has a working default.
    # X_BACKEND pins the backend order ("cookies", "api", "rss",
    # "syndication", or a comma-separated subset); auto tries them in that
    # order and takes the first that answers.
    # -------------------------------------------------------------------------
    X_BACKEND = os.getenv("X_BACKEND", "auto").strip() or "auto"
    X_AUTH_TOKEN = os.getenv("X_AUTH_TOKEN", "").strip()
    X_CT0 = os.getenv("X_CT0", "").strip()
    X_HANDLE = os.getenv("X_HANDLE", "").strip().lstrip("@")
    X_API_BASE_URL = os.getenv("X_API_BASE_URL", "").strip().rstrip("/")
    X_API_KEY = os.getenv("X_API_KEY", "").strip()
    X_API_KEY_HEADER = os.getenv("X_API_KEY_HEADER", "Authorization").strip()
    X_API_PATHS = _json_env("X_API_PATHS")
    X_RSS_BASE_URL = os.getenv("X_RSS_BASE_URL", "").strip().rstrip("/")
    X_RSS_PATHS = _json_env("X_RSS_PATHS")
    X_SYNDICATION = _bool_env("X_SYNDICATION", True)
    X_MAX_CHARS = _int_env("X_MAX_CHARS", 280, min_value=1, max_value=25000)
    X_TIMEOUT_SECONDS = _int_env("X_TIMEOUT_SECONDS", 20, min_value=5, max_value=120)
    X_GRAPHQL_FILE = os.getenv("X_GRAPHQL_FILE", "").strip()

    MAXWELL_EMAIL_IGNORE_SENDERS = os.getenv(
        "MAXWELL_EMAIL_IGNORE_SENDERS", ""
    ).strip()

    # -------------------------------------------------------------------------
    # Native sub-agent (only used if ENABLE_SUBAGENT resolves true).
    # Maxwell runs the task itself on its own provider inside a scratch
    # workdir — no external coding-agent binary, no container image.
    # -------------------------------------------------------------------------
    SUBAGENT_BASE_DIR = os.getenv("SUBAGENT_BASE_DIR", "data/subagents").strip()
    # MODEL for sub-agent work. Blank means the main OLLAMA_MODEL — the sub-
    # agent runs on the SAME model the chat uses by default. Only set this to
    # run sub-agent jobs on a different (typically cheaper/faster) model.
    SUBAGENT_MODEL = os.getenv("SUBAGENT_MODEL", "").strip()
    SUBAGENT_MAX_STEPS = _int_env("SUBAGENT_MAX_STEPS", 100, min_value=1, max_value=200)
    SUBAGENT_MAX_TOKENS = _int_env(
        "SUBAGENT_MAX_TOKENS", 16384, min_value=256, max_value=128000
    )
    SUBAGENT_TIMEOUT_SECONDS = _int_env(
        "SUBAGENT_TIMEOUT_SECONDS", 900, min_value=30, max_value=7200
    )
    SUBAGENT_COMMAND_TIMEOUT_SECONDS = _int_env(
        "SUBAGENT_COMMAND_TIMEOUT_SECONDS", 120, min_value=5, max_value=3600
    )
    # How many background (fire-and-forget) sub-agents may run at once. Each
    # one holds a provider loop and a sandbox container, so this caps both the
    # provider load and how many throwaway containers are alive. Foreground
    # (blocking) sub-agent calls are not limited by this — the model only ever
    # runs one of those per turn.
    SUBAGENT_MAX_CONCURRENT = _int_env(
        "SUBAGENT_MAX_CONCURRENT", 5, min_value=1, max_value=16
    )
    # Hard ceiling on background sub-agents submitted but not yet finished
    # (running + waiting on a concurrency slot). Past this, new background
    # requests are refused with a clear "too busy" so a channel flood can't
    # grow the in-memory queue without bound. Foreground calls are unaffected.
    SUBAGENT_MAX_QUEUED = _int_env("SUBAGENT_MAX_QUEUED", 16, min_value=1, max_value=128)
    # How long a finished sub-agent's message channel is kept so the main agent
    # can still reply to it (sub_agent_message) and read its pending notes.
    SUBAGENT_CHAN_GRACE_SECONDS = _int_env(
        "SUBAGENT_CHAN_GRACE_SECONDS", 600, min_value=30, max_value=7200
    )
    # Where the sub-agent's `run_command` executes. "docker" (default) runs
    # each run in its own throwaway container with only the run's scratch
    # workspace mounted — the bot's .env and source are not visible to it.
    # "host" runs `bash -lc` in the bot's own environment: no isolation, and
    # only sensible if the whole machine is already disposable.
    SUBAGENT_SANDBOX = os.getenv("SUBAGENT_SANDBOX", "docker").strip().lower()
    SUBAGENT_MAX_FILE_BYTES = _int_env(
        "SUBAGENT_MAX_FILE_BYTES", 200_000, min_value=1000, max_value=5_000_000
    )
    # How many times a sub-agent retries a provider call that fails before it
    # gives up the whole run. A self-hosted or proxied model drops a call now
    # and then (network blip, 5xx, transient timeout) — that used to end a
    # minutes-long run with "the model call failed". Retries with backoff make
    # a hiccup cost a step, not the run. 0 disables and keeps the old behaviour.
    SUBAGENT_PROVIDER_RETRIES = _int_env(
        "SUBAGENT_PROVIDER_RETRIES", 2, min_value=0, max_value=6
    )
    # How many consecutive "dud" replies (no tool call AND no content) a
    # sub-agent tolerates before it ends the run. A model that returns nothing
    # at all is stalled or glitched; instead of ending the run we nudge it once
    # or twice to take an action, then give up. 0 keeps the old behaviour.
    SUBAGENT_DUD_TOLERANCE = _int_env(
        "SUBAGENT_DUD_TOLERANCE", 2, min_value=0, max_value=6
    )

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

    # Every optional feature, in the order doctor.py and the startup log
    # print them. (attribute, human label).
    FEATURE_SWITCHES = (
        ("ENABLE_IMAGE_INPUT", "image input (vision)"),
        ("ENABLE_VIDEO_INPUT", "video input (frame extraction)"),
        ("ENABLE_AUDIO_INPUT", "audio input (omni models)"),
        ("ENABLE_IMAGE_GEN", "image generation"),
        ("ENABLE_TTS", "text-to-speech"),
        ("ENABLE_TTS_VC", "TTS playback in voice channels"),
        ("ENABLE_VC", "voice channels (live listening)"),
        ("ENABLE_WEB_SEARCH", "web search"),
        ("ENABLE_FETCH_URL", "fetch_url"),
        ("ENABLE_YOUTUBE", "YouTube"),
        ("ENABLE_CREATE_SITE", "site generation"),
        ("ENABLE_AVATAR", "avatar changes"),
        ("ENABLE_EMAIL_TOOLS", "email tools"),
        ("ENABLE_X", "X (Twitter)"),
        ("ENABLE_SHELL", "shell (docker sandbox)"),
        ("ENABLE_SUBAGENT", "native sub-agent"),
        ("ENABLE_RAG", "RAG vector memory"),
        ("ENABLE_TELEGRAM", "Telegram transport"),
        ("ENABLE_AUTONOMY", "autonomy engine"),
        ("REM_ENABLED", "REM dreaming pass"),
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
        if not cls.DISCORD_TOKEN:
            raise ValueError(
                "DISCORD_TOKEN is required. Run ./setup.sh, or set it in .env, "
                "then start the bot again."
            )
        if not cls.OLLAMA_BASE_URL:
            raise ValueError(
                "OLLAMA_BASE_URL is required — point it at any OpenAI-compatible "
                "endpoint (local Ollama, OpenRouter, LM Studio, ...)."
            )
        if not cls.OLLAMA_MODEL:
            raise ValueError(
                "OLLAMA_MODEL is required — set the model name your endpoint serves."
            )
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
        if cls.ENABLE_SHELL:
            _log.warning(
                "ENABLE_SHELL is on — the model can run commands on this host "
                "as the bot user. Set ENABLE_SHELL=false in .env if you did "
                "not mean to grant that."
            )
        # TTS engine sanity check
        if cls.TTS_ENGINE not in {"auto", "local", "riva", "gtts", "fish"}:
            _log.warning(
                "TTS_ENGINE=%r is not one of auto/local/riva/gtts/fish — falling "
                "back to 'auto' behaviour.",
                cls.TTS_ENGINE,
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
