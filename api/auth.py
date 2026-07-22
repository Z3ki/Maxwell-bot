"""Auth, rate-limiting, and response helpers for the Maxwell API server.

Imports CORS_ORIGIN and DISCORD_ALLOWED_USER_IDS from api.config (no circular
dep), and resolves DATA_DIR lazily via api.storage._data_dir() so tests that
monkeypatch api.api_server.DATA_DIR keep working.
"""

import base64
import contextlib
import hmac
import json
import os
import time
from collections import defaultdict

from aiohttp import web

from api.config import (
    AUTH_CLEANUP_INTERVAL,
    AUTH_RATE_MAX,
    AUTH_RATE_WINDOW,
    CORS_ORIGIN,
    DISCORD_ALLOWED_USER_IDS,
    DISCORD_TOKEN_TTL,
)
from api.storage import _data_dir

ADMIN_USER = os.getenv("MAXWELL_ADMIN_USER", "").strip()
ADMIN_PASSWORD = os.getenv("MAXWELL_ADMIN_PASSWORD", "").strip()

_DISCORD_TOKENS: dict[str, dict] = {}

_auth_failures: dict[str, list[float]] = defaultdict(list)
_last_auth_cleanup = 0.0


def _load_admin_creds():
    """Load admin credentials from environment only.

    Persisting plaintext admin credentials in the data directory is unsafe for
    open-source deployments and easy to publish accidentally.
    """
    global ADMIN_USER, ADMIN_PASSWORD
    ADMIN_USER = os.getenv("MAXWELL_ADMIN_USER", "").strip()
    ADMIN_PASSWORD = os.getenv("MAXWELL_ADMIN_PASSWORD", "").strip()
    return ADMIN_USER, ADMIN_PASSWORD


def _load_bot_admins():
    """Read the bot's live admin allowlist from admins.json.

    The bot writes this file every time `,admin @user` / `,admin clear` runs,
    so a user promoted via chat can immediately OAuth into the dashboard
    without a restart. We read it on every call (it's tiny) so promotions
    take effect without bouncing the API process.

    Falls back to DISCORD_ALLOWED_USER_IDS env if no file is present, so an
    operator who hasn't run the bot yet can still seed the allowlist.

    Returns a set of user-id strings. Empty set = nobody allowed.
    """
    path = _data_dir() / "admins.json"
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, ValueError):
            return set(DISCORD_ALLOWED_USER_IDS)
        if isinstance(data, list):
            return {str(x).strip() for x in data if str(x).strip()}
        if isinstance(data, dict):
            ids = set()
            for key in ("admins", "owners", "user_ids"):
                values = data.get(key)
                if isinstance(values, list):
                    ids.update(str(x).strip() for x in values if str(x).strip())
            if ids:
                return ids
    return set(DISCORD_ALLOWED_USER_IDS)


def _discord_token_authed(request) -> bool:
    token = request.headers.get("X-Discord-Token", "")
    info = _DISCORD_TOKENS.get(token)
    return bool(info and info.get("expires", 0) >= time.time())


def _json_response(data, status=200):
    return web.json_response(
        data,
        status=status,
        headers={
            "Access-Control-Allow-Origin": CORS_ORIGIN,
            "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, Authorization, X-Discord-Token",
        },
    )


def _needs_auth(request) -> bool:
    """All requests need auth except OPTIONS preflight and /api/login."""
    return request.method != "OPTIONS"


def _get_client_ip(request) -> str:
    """Extract client IP. Only trust X-Forwarded-For when MAXWELL_TRUST_PROXY=1."""
    trust_proxy = os.getenv("MAXWELL_TRUST_PROXY", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if trust_proxy:
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return getattr(request, "remote", None) or "unknown"


def _safe_compare(a: str, b: str) -> bool:
    """Length-safe constant-time compare (avoid 500 length oracle)."""
    if a is None or b is None:
        return False
    a = str(a)
    b = str(b)
    if len(a) != len(b):
        dummy = a if len(a) >= len(b) else b
        with contextlib.suppress(Exception):
            hmac.compare_digest(dummy, dummy)
        return False
    try:
        return hmac.compare_digest(a, b)
    except Exception:
        return False


def _cleanup_auth_failures():
    """Prune stale entries from _auth_failures to prevent unbounded growth."""
    global _last_auth_cleanup
    now = time.time()
    if now - _last_auth_cleanup < AUTH_CLEANUP_INTERVAL:
        return
    _last_auth_cleanup = now
    stale_ips = [
        ip
        for ip, times in _auth_failures.items()
        if all(now - t >= AUTH_RATE_WINDOW for t in times)
    ]
    for ip in stale_ips:
        del _auth_failures[ip]


def _check_rate_limit(request) -> bool:
    """Return True if request is rate-limited (should be rejected)."""
    ip = _get_client_ip(request)
    now = time.time()
    _auth_failures[ip] = [t for t in _auth_failures[ip] if now - t < AUTH_RATE_WINDOW]
    _cleanup_auth_failures()
    return len(_auth_failures[ip]) >= AUTH_RATE_MAX


def _record_auth_failure(request):
    ip = _get_client_ip(request)
    _auth_failures[ip].append(time.time())


def _basic_credentials(request):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Basic "):
        return None, None
    try:
        decoded = base64.b64decode(auth[6:].strip(), validate=True).decode("utf-8")
    except Exception:
        return None, None
    if ":" not in decoded:
        return None, None
    username, password = decoded.split(":", 1)
    return username, password


def _has_admin_auth(request) -> bool:
    if _discord_token_authed(request):
        return True
    _load_admin_creds()
    if not ADMIN_USER or not ADMIN_PASSWORD:
        return False
    username, password = _basic_credentials(request)
    return bool(
        _safe_compare(username or "", ADMIN_USER)
        and _safe_compare(password or "", ADMIN_PASSWORD)
    )


@web.middleware
async def _auth_middleware_unless_login(request, handler):
    """Middleware that requires auth for all requests, except OPTIONS and /api/login."""
    if request.path.startswith("/api/auth/discord"):
        return await handler(request)
    if request.method == "POST" and request.path == "/api/login":
        if _check_rate_limit(request):
            return _json_response({"error": "too many attempts, try again later"}, 429)
        return await handler(request)
    if _needs_auth(request):
        if _check_rate_limit(request):
            return _json_response({"error": "too many attempts, try again later"}, 429)
        _load_admin_creds()
        if not ADMIN_USER or not ADMIN_PASSWORD:
            if not _discord_token_authed(request):
                return _json_response({"error": "admin auth not configured"}, 503)
        else:
            username, password = _basic_credentials(request)
            if not (
                _safe_compare(username or "", ADMIN_USER)
                and _safe_compare(password or "", ADMIN_PASSWORD)
            ) and not _discord_token_authed(request):
                _record_auth_failure(request)
                return _json_response({"error": "unauthorized"}, 401)
    resp = await handler(request)
    if isinstance(resp, web.Response):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        resp.headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
        )
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; connect-src 'self'",
        )
    return resp


def _set_discord_token(token: str, user_info: dict) -> None:
    info = dict(user_info)
    info["expires"] = time.time() + DISCORD_TOKEN_TTL
    _DISCORD_TOKENS[token] = info