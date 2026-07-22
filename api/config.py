"""Environment-derived configuration for the Maxwell API server.

All constants here are read once at import time from environment variables /
the .env file (loaded by api.storage). No route handlers, no I/O side effects
beyond os.getenv. Imported by api_server, auth, and state without circularity.
"""

import os
from pathlib import Path

from api.storage import APP_ROOT, _int_env_safe  # noqa: E402

try:
    from control_defaults import parse_bool as _parse_bool  # noqa: E402
except ImportError:
    def _parse_bool(value, default=False):
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        s = str(value).strip().lower()
        if s in {"1", "true", "yes", "on"}:
            return True
        if s in {"0", "false", "no", "off"}:
            return False
        return default


CORS_ORIGIN = os.getenv(
    "MAXWELL_CORS_ORIGIN",
    os.getenv("MAXWELL_PUBLIC_BASE_URL", "https://maxwell.example.com"),
).rstrip("/")
API_HOST = os.getenv("MAXWELL_API_HOST", "127.0.0.1")
API_PORT = _int_env_safe("MAXWELL_API_PORT", 8765)
BASE_SITE_DIR = Path(
    os.getenv("MAXWELL_SITE_DIR", APP_ROOT / "public" / "bot")
).resolve()

ADMIN_USER = os.getenv("MAXWELL_ADMIN_USER", "").strip()
ADMIN_PASSWORD = os.getenv("MAXWELL_ADMIN_PASSWORD", "").strip()

DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID", "").strip()
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "").strip()
DISCORD_REDIRECT_URI = os.getenv(
    "DISCORD_REDIRECT_URI",
    "https://maxwell.z3ki.dev/api/auth/discord/callback",
).strip()
DISCORD_ALLOWED_USER_IDS = {
    uid.strip()
    for uid in os.getenv("DISCORD_ALLOWED_USER_IDS", "").split(",")
    if uid.strip()
}

REM_ENABLED_DEFAULT = _parse_bool(os.getenv("REM_ENABLED"), False)
REM_INTERVAL_DEFAULT = _int_env_safe("REM_INTERVAL_SECONDS", 600)
REM_RUN_HISTORY_DEFAULT = _int_env_safe("REM_RUN_HISTORY", 50)

MAX_LTM_LINES = 999
MAX_LTM_CHARS = 1000
MAX_PROMPT_CHARS = 12000
MAX_COMMANDS = 200
MAX_AUTONOMY_GOALS = 50

AUTH_RATE_WINDOW = 300
AUTH_RATE_MAX = 10
AUTH_CLEANUP_INTERVAL = 600
DISCORD_TOKEN_TTL = 7 * 24 * 3600