"""Auth, rate-limiting, and response helpers for the Maxwell API server.

Configuration comes from api.config without circular imports.
"""

import base64
import contextlib
import hmac
import os
import re
import time
from collections import defaultdict

from aiohttp import web

from api.config import (
    AUTH_CLEANUP_INTERVAL,
    AUTH_RATE_MAX,
    AUTH_RATE_WINDOW,
    CORS_ORIGIN,
)

ADMIN_USER = os.getenv("MAXWELL_ADMIN_USER", "").strip()
ADMIN_PASSWORD = os.getenv("MAXWELL_ADMIN_PASSWORD", "").strip()

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


def _json_response(data, status=200):
    return web.json_response(
        data,
        status=status,
        headers={
            "Access-Control-Allow-Origin": CORS_ORIGIN,
            "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, Authorization",
        },
    )


# Public routes. These cannot carry admin credentials by definition.
#
#   /api/site/<slug>/...   the built-in datastore (site_backend.py), scoped to
#                          one slug, size-capped and rate-limited.
#   /bot/<slug>/api/...    the site's own server, proxied to a container that
#                          only that slug's registry row can name
#                          (site_server.py). Caddy routes this path here.
#   /api/github/webhook    signed GitHub wakeup; HMAC-verified, no admin auth.
#   /api/github/oauth/...  GitHub login callback; state is HMAC-signed.
#
# Neither site namespace can reach another slug's data, and nothing else on
# the API is reachable through them.
PUBLIC_PATH_PREFIXES = ("/api/site/", "/api/github/webhook", "/api/github/oauth")
PUBLIC_PATH_RE = re.compile(r"^/bot/[a-z0-9-]{2,30}/api(?:/|$)")


def _needs_auth(request) -> bool:
    """All requests need auth except OPTIONS preflight, /api/login, and the
    public per-site backends."""
    if request.path in ("/health", "/api/health"):
        return False
    if request.path.startswith(PUBLIC_PATH_PREFIXES):
        return False
    if PUBLIC_PATH_RE.match(request.path):
        return False
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
    if request.method == "POST" and request.path == "/api/login":
        if _check_rate_limit(request):
            return _json_response({"error": "too many attempts, try again later"}, 429)
        return await handler(request)
    if _needs_auth(request):
        # Authenticate first. Rate-limiting valid sessions (or every request
        # before credentials are checked) lets an unauthenticated client lock
        # the operator API, especially behind a proxy that shares one IP.
        _load_admin_creds()
        if _has_admin_auth(request):
            pass
        elif not ADMIN_USER or not ADMIN_PASSWORD:
            if _check_rate_limit(request):
                return _json_response(
                    {"error": "too many attempts, try again later"}, 429
                )
            _record_auth_failure(request)
            return _json_response({"error": "admin auth not configured"}, 503)
        else:
            if _check_rate_limit(request):
                return _json_response(
                    {"error": "too many attempts, try again later"}, 429
                )
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

