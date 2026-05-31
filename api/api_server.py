#!/usr/bin/env python3
"""Backend server for the Maxwell dashboard/admin API.

All API and data routes require Basic username/password auth by default.
"""
import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import shutil
import tempfile
import time
import uuid as _uuid
from collections import defaultdict
from pathlib import Path

from aiohttp import web

APP_ROOT = Path(os.getenv("MAXWELL_APP_ROOT", Path(__file__).resolve().parents[1]))
ENV_FILE = Path(os.getenv("MAXWELL_ENV_FILE", APP_ROOT / ".env"))


def _load_env_file(path: Path):
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _int_env_safe(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default





_load_env_file(ENV_FILE)

# Add parent dir to path so we can import shared modules
import sys as _sys
_sys.path.insert(0, str(APP_ROOT))
from control_defaults import DEFAULT_CONTROL, KNOWN_TOOLS, parse_bool as _parse_bool  # noqa: E402

DATA_DIR = Path(os.getenv("DATA_DIR", APP_ROOT / "data"))
CORS_ORIGIN = os.getenv("MAXWELL_CORS_ORIGIN", os.getenv("MAXWELL_PUBLIC_BASE_URL", "https://maxwell.example.com")).rstrip("/")
API_HOST = os.getenv("MAXWELL_API_HOST", "127.0.0.1")
API_PORT = _int_env_safe("MAXWELL_API_PORT", 8765)
BASE_SITE_DIR = Path(os.getenv("MAXWELL_SITE_DIR", APP_ROOT / "public" / "bot")).resolve()
ADMIN_USER = os.getenv("MAXWELL_ADMIN_USER", "").strip()
ADMIN_PASSWORD = os.getenv("MAXWELL_ADMIN_PASSWORD", "").strip()
REM_ENABLED_DEFAULT = _parse_bool(os.getenv("REM_ENABLED"), False)
REM_INTERVAL_DEFAULT = _int_env_safe("REM_INTERVAL_SECONDS", 600)
REM_RUN_HISTORY_DEFAULT = _int_env_safe("REM_RUN_HISTORY", 50)


def _load_admin_creds():
    """Load admin credentials from environment only.

    Persisting plaintext admin credentials in the data directory is unsafe for
    open-source deployments and easy to publish accidentally.
    """
    global ADMIN_USER, ADMIN_PASSWORD
    ADMIN_USER = os.getenv("MAXWELL_ADMIN_USER", "").strip()
    ADMIN_PASSWORD = os.getenv("MAXWELL_ADMIN_PASSWORD", "").strip()
    return ADMIN_USER, ADMIN_PASSWORD


_load_admin_creds()
MAX_LTM_LINES = 999
MAX_LTM_CHARS = 1000
MAX_PROMPT_CHARS = 12000
MAX_ID_CHARS = 64
_file_lock = asyncio.Lock()
# DEFAULT_CONTROL, KNOWN_TOOLS, _parse_bool imported from control_defaults.py above
MAX_COMMANDS = 200
MAX_AUTONOMY_GOALS = 50


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

def _needs_auth(request) -> bool:
    """All requests need auth except OPTIONS preflight and /api/login."""
    if request.method == "OPTIONS":
        return False
    return True


# --- Rate limiting for login/auth ---
_auth_failures: dict[str, list[float]] = defaultdict(list)
_AUTH_RATE_WINDOW = 300  # 5 minutes
_AUTH_RATE_MAX = 10  # max failures per window

def _check_rate_limit(request) -> bool:
    """Return True if request is rate-limited (should be rejected)."""
    ip = getattr(request, 'remote', None) or "unknown"
    now = time.time()
    # Prune old entries
    _auth_failures[ip] = [t for t in _auth_failures[ip] if now - t < _AUTH_RATE_WINDOW]
    return len(_auth_failures[ip]) >= _AUTH_RATE_MAX

def _record_auth_failure(request):
    ip = getattr(request, 'remote', None) or "unknown"
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
        hmac.compare_digest(username or "", ADMIN_USER)
        and hmac.compare_digest(password or "", ADMIN_PASSWORD)
    )


async def _auth_middleware_unless_login(app, handler):
    """Middleware that requires auth for all requests, except OPTIONS and /api/login."""
    async def middleware(request):
        if request.method == "POST" and request.path == "/api/login":
            # Rate limit login attempts
            if _check_rate_limit(request):
                return _json_response({"error": "too many attempts, try again later"}, 429)
            return await handler(request)
        if _needs_auth(request):
            _load_admin_creds()
            if not ADMIN_USER or not ADMIN_PASSWORD:
                return _json_response({"error": "admin auth not configured"}, 503)
            username, password = _basic_credentials(request)
            if not (
                hmac.compare_digest(username or "", ADMIN_USER)
                and hmac.compare_digest(password or "", ADMIN_PASSWORD)
            ):
                _record_auth_failure(request)
                return _json_response({"error": "unauthorized"}, 401)
        # Add security headers to all responses
        resp = await handler(request)
        if isinstance(resp, web.Response):
            resp.headers.setdefault("X-Content-Type-Options", "nosniff")
            resp.headers.setdefault("X-Frame-Options", "DENY")
            resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
            resp.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
            resp.headers.setdefault("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; connect-src 'self'")
        return resp
    return middleware


def _load(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, ValueError):
        return None


def _safe_list(value):
    return value if isinstance(value, list) else []


def _safe_object(value):
    return value if isinstance(value, dict) else {}


def _load_for_write(path, expected_type, default):
    if not path.exists():
        return default
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        # Mutating a corrupt file as []/{} silently deletes data. Nope.
        raise ValueError(f"refusing to overwrite corrupt {path.name}: {exc}") from exc
    if not isinstance(data, expected_type):
        raise ValueError(f"refusing to overwrite malformed {path.name}")
    return data


def _clean_id(value: str) -> str:
    return str(value or "").strip()[:MAX_ID_CHARS]


def _control_path():
    return DATA_DIR / "bot_control.json"


def _rem_state_path():
    return DATA_DIR / "rem_state.json"


def _rem_runs_path():
    return DATA_DIR / "rem_runs.json"


def _rem_events_path():
    return DATA_DIR / "rem_events.json"


def _rem_control_path():
    return DATA_DIR / "rem_control.json"


def _autonomy_state_path():
    return DATA_DIR / "autonomy_state.json"


def _autonomy_goals_path():
    return DATA_DIR / "autonomy_goals.json"


def _autonomy_log_path():
    return DATA_DIR / "autonomy_log.json"


def _sanitize_rem_control(control):
    control = _safe_object(control)
    interval = REM_INTERVAL_DEFAULT
    try:
        if control.get("interval_seconds") is not None:
            interval = max(10, int(control.get("interval_seconds")))
    except (TypeError, ValueError):
        pass
    max_turns = 3
    try:
        env_val = os.getenv("REM_MAX_TURNS", "3")
        max_turns = int(env_val)
    except (TypeError, ValueError):
        pass
    try:
        if control.get("max_turns") is not None:
            max_turns = max(0, min(int(control.get("max_turns")), 10))
    except (TypeError, ValueError):
        pass
    return {
        "enabled": _parse_bool(control.get("enabled"), REM_ENABLED_DEFAULT),
        "interval_seconds": interval,
        "max_turns": max_turns,
        "prompt": str(control.get("prompt") or ""),
    }


def _load_rem_control():
    return _sanitize_rem_control(_load(_rem_control_path()))


def _load_rem_control_for_write():
    return _sanitize_rem_control(_load_for_write(_rem_control_path(), dict, {}))


async def _save_rem_control(control):
    await atomic_json_write(_rem_control_path(), control)


def _load_rem_status():
    control = _load_rem_control()
    state = _safe_object(_load(_rem_state_path()))
    runs = _safe_list(_load(_rem_runs_path()))
    events = _safe_list(_load(_rem_events_path()))
    last = runs[-1] if runs and isinstance(runs[-1], dict) else {}
    return {
        "enabled": control["enabled"],
        "interval_s": control["interval_seconds"],
        "last_run": state.get("last_rem_run_ts") or last.get("ts") or "",
        "events_buffered": len(events),
        "last_audit_preview": str(state.get("last_audit") or last.get("audit") or "")[:500],
        "running": bool(state.get("running")),
    }


def _load_control():
    control = dict(DEFAULT_CONTROL)
    loaded = _safe_object(_load(_control_path()))
    control.update(loaded)
    return _sanitize_control(control)


def _sanitize_control(control):
    control = _safe_object(control)
    # Preserve newer bot-side settings this API build doesn't understand yet.
    # Dashboard saves should not casually delete config just because the UI lags.
    out = {k: v for k, v in control.items() if k not in DEFAULT_CONTROL}
    out.update(DEFAULT_CONTROL)
    for key, default in DEFAULT_CONTROL.items():
        value = control.get(key, default)
        if isinstance(default, bool):
            out[key] = _parse_bool(value, default)
        elif isinstance(default, int):
            try:
                out[key] = int(value)
            except (TypeError, ValueError):
                out[key] = default
        elif isinstance(default, float):
            try:
                out[key] = float(value)
            except (TypeError, ValueError):
                out[key] = default
        elif isinstance(default, list):
            if isinstance(value, list):
                items = [str(x).strip()[:64] for x in value if str(x).strip()]
                out[key] = [x for x in items if x in KNOWN_TOOLS] if key == "disabled_tools" else items[:500]
            else:
                out[key] = []
        else:
            out[key] = value
    out["auto_eval_every"] = max(1, min(out["auto_eval_every"], 100))
    out["auto_max_recent_replies"] = max(0, min(out["auto_max_recent_replies"], 100))
    out["auto_recent_window_minutes"] = max(1, min(out["auto_recent_window_minutes"], 1440))
    out["auto_inactivity_minutes"] = max(0, min(out["auto_inactivity_minutes"], 10080))
    out["per_user_cooldown_seconds"] = max(0, min(out["per_user_cooldown_seconds"], 3600))
    out["max_image_size_mb"] = max(1, min(out["max_image_size_mb"], 25))
    out["ai_timeout_seconds"] = max(10, min(out["ai_timeout_seconds"], 600))
    out["ai_concurrency"] = max(1, min(out["ai_concurrency"], 10))
    out["autonomy_interval_seconds"] = max(30, int(out.get("autonomy_interval_seconds", 300) or 300))
    out["memory_history_messages"] = max(0, min(out["memory_history_messages"], 100))
    out["memory_context_budget"] = max(1000, min(out["memory_context_budget"], 100000))
    out["tool_history_messages"] = max(0, min(int(out.get("tool_history_messages", 3) or 3), 20))
    out["prompt_context_budget"] = max(10000, min(int(out.get("prompt_context_budget", 60000) or 60000), 200000))
    out["cross_context_max_items"] = max(1, min(int(out.get("cross_context_max_items", 10)), 50))
    out["cross_context_budget"] = max(1000, min(int(out.get("cross_context_budget", 5000)), 20000))
    out["cross_context_min_importance"] = max(1, min(int(out.get("cross_context_min_importance", 5)), 10))
    out["max_tool_iterations"] = max(0, min(out["max_tool_iterations"], 25))
    out["max_response_chars"] = max(80, min(out["max_response_chars"], 4000))
    out["vc_rms_threshold"] = max(100, min(int(out.get("vc_rms_threshold", 1200) or 1200), 10000))
    out["vc_pause_seconds"] = max(0.1, min(float(out.get("vc_pause_seconds", 0.8) or 0.8), 5.0))
    out["vc_min_seconds"] = max(0.1, min(float(out.get("vc_min_seconds", 0.55) or 0.55), 10.0))
    out["vc_max_seconds"] = max(1.0, min(float(out.get("vc_max_seconds", 18) or 18), 120.0))
    out["vc_preroll_seconds"] = max(0.0, min(float(out.get("vc_preroll_seconds", 0.25) or 0.25), 3.0))
    out["vc_ai_timeout_seconds"] = max(5, min(int(out.get("vc_ai_timeout_seconds", 25) or 25), 180))
    out["vc_ai_max_tokens"] = max(16, min(int(out.get("vc_ai_max_tokens", 90) or 90), 1000))
    out["vc_memory_history_messages"] = max(0, min(int(out.get("vc_memory_history_messages", 2) or 2), 20))
    out["vc_max_response_chars"] = max(40, min(int(out.get("vc_max_response_chars", 260) or 260), 2000))
    out["vc_wake_words"] = [str(x).strip()[:32] for x in out.get("vc_wake_words", []) if str(x).strip()][:20]
    out["base_personality"] = str(out.get("base_personality", DEFAULT_CONTROL["base_personality"]))[:12000]
    out["auto_decider_prompt"] = str(out.get("auto_decider_prompt", DEFAULT_CONTROL["auto_decider_prompt"]))[:8000]
    return out


def _normalize_memory_line(content: str) -> str:
    return " ".join(str(content).split())[:MAX_LTM_CHARS]


def _memory_text_path():
    return DATA_DIR / "long_term_memory.txt"


def _memory_lines():
    path = _memory_text_path()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    return [_normalize_memory_line(line) for line in lines if line.strip()][:MAX_LTM_LINES]


def _memory_json():
    return [{"id": i + 1, "content": line} for i, line in enumerate(_memory_lines())]


def _context_path():
    return DATA_DIR / "shared_context.json"


def _normalize_context_content(content: str) -> str:
    return " ".join(str(content or "").split())[:1200]


def _context_entry_id(raw: dict) -> str:
    existing = str(raw.get("id") or "").strip()
    if existing:
        return existing[:32]
    stable = json.dumps({
        "scope": raw.get("scope") or "global",
        "content": _normalize_context_content(raw.get("content", "")),
        "created_at": raw.get("created_at") or "",
        "source_user_id": raw.get("source_user_id") or "",
        "source_channel_id": raw.get("source_channel_id") or "",
    }, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(stable.encode("utf-8")).hexdigest()[:8]


def _sanitize_context_entries(data):
    if not isinstance(data, list):
        return []
    now = time.time()
    out = []
    for raw in data:
        if not isinstance(raw, dict):
            continue
        content = _normalize_context_content(raw.get("content", ""))
        if not content:
            continue
        expires_at = str(raw.get("expires_at") or "")
        if expires_at:
            try:
                if time.mktime(time.strptime(expires_at[:19], "%Y-%m-%dT%H:%M:%S")) <= now:
                    continue
            except Exception:
                pass
        try:
            importance = int(raw.get("importance", 5))
        except (TypeError, ValueError):
            importance = 5
        visibility = str(raw.get("visibility") or "shared")[:32]
        if visibility not in {"private", "shared", "admin_only", "public_hint"}:
            visibility = "shared"
        tags = raw.get("tags", [])
        if isinstance(tags, str):
            tags = [tags]
        if not isinstance(tags, list):
            tags = []
        out.append({
            "id": _context_entry_id(raw),
            "scope": str(raw.get("scope") or "global")[:80],
            "visibility": visibility,
            "importance": max(1, min(importance, 10)),
            "content": content,
            "source_user_id": str(raw.get("source_user_id") or "")[:64],
            "source_channel_id": str(raw.get("source_channel_id") or "")[:64],
            "source_guild_id": str(raw.get("source_guild_id") or "")[:64],
            "source_kind": str(raw.get("source_kind") or "unknown")[:32],
            "tags": [str(t).strip()[:32] for t in tags if str(t).strip()][:12],
            "created_at": str(raw.get("created_at") or "")[:64],
            "last_seen_at": str(raw.get("last_seen_at") or raw.get("created_at") or "")[:64],
            "expires_at": expires_at[:64],
        })
    out.sort(key=lambda e: (e.get("last_seen_at", ""), e.get("created_at", "")), reverse=True)
    return out[:1000]


def _load_context_entries():
    return _sanitize_context_entries(_load(_context_path()))


def _load_context_entries_for_write():
    return _sanitize_context_entries(_load_for_write(_context_path(), list, []))


async def _save_context_entries(entries):
    await atomic_json_write(_context_path(), entries[:1000])


async def atomic_json_write(path: Path, data):
    """Atomic write: temp file + fsync + rename."""

    def _sync_write():
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False, default=str)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    await asyncio.to_thread(_sync_write)


async def atomic_text_write(path: Path, text: str):
    """Atomic write: temp file + fsync + rename."""

    def _sync_write():
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    await asyncio.to_thread(_sync_write)


# ---------- Data (all authenticated) ----------
async def data_file(request):
    file = request.match_info.get("file", "")
    if ".." in file or "/" in file or not file.endswith(".json"):
        return _json_response({"error": "bad file"}, 403)
    # All data files require auth
    ALLOWED_FILES = {
        "sites.json", "prompts.json", "memory.json",
        "long_term_memory.json", "blacklist.json", "auto_channels.json",
        "bot_control.json",
    }
    if file not in ALLOWED_FILES:
        return _json_response({"error": "forbidden"}, 403)
    if file == "long_term_memory.json":
        return _json_response(_memory_json())
    if file == "bot_control.json":
        return _json_response({"control": _load_control(), "tools": KNOWN_TOOLS})
    path = DATA_DIR / file
    if not path.exists():
        return _json_response({"error": "not found"}, 404)
    text = await asyncio.to_thread(path.read_text, encoding="utf-8")
    return web.Response(
        text=text,
        content_type="application/json",
        headers={"Access-Control-Allow-Origin": CORS_ORIGIN},
    )


# ---------- Memory ----------
async def _handle_memory():
    return _memory_text_path(), _memory_lines()


async def memory_add(request):
    try:
        body = await request.json()
    except Exception:
        return _json_response({"error": "invalid json"}, 400)
    content = body.get("content", "").strip()
    if not content:
        return _json_response({"error": "empty"}, 400)
    content = _normalize_memory_line(content)
    async with _file_lock:
        path, mem = await _handle_memory()
        mem.append(content)
        mem = mem[-MAX_LTM_LINES:]
        await atomic_text_write(path, "\n".join(mem) + ("\n" if mem else ""))
        nxt = len(mem)
    return _json_response({"ok": True, "id": nxt})


async def memory_update(request):
    try:
        body = await request.json()
    except Exception:
        return _json_response({"error": "invalid json"}, 400)
    mid = body.get("id", "")
    content = _normalize_memory_line(body.get("content", ""))
    if not content:
        return _json_response({"error": "empty"}, 400)
    try:
        idx = int(mid) - 1
    except (TypeError, ValueError):
        return _json_response({"error": "not found"}, 404)
    async with _file_lock:
        path, mem = await _handle_memory()
        if idx < 0 or idx >= len(mem):
            return _json_response({"error": "not found"}, 404)
        mem[idx] = content
        await atomic_text_write(path, "\n".join(mem) + ("\n" if mem else ""))
    return _json_response({"ok": True})


async def memory_delete(request):
    mid = request.query.get("id", "")
    try:
        idx = int(mid) - 1
    except ValueError:
        return _json_response({"error": "not found"}, 404)
    async with _file_lock:
        path, mem = await _handle_memory()
        if idx < 0 or idx >= len(mem):
            return _json_response({"error": "not found"}, 404)
        del mem[idx]
        await atomic_text_write(path, "\n".join(mem) + ("\n" if mem else ""))
    return _json_response({"ok": True})


# ---------- Shared context ----------
async def context_get(request):
    if not _has_admin_auth(request):
        return _json_response({"error": "unauthorized"}, 401)
    entries = _load_context_entries()
    query = str(request.query.get("q", "")).strip().lower()
    if query:
        entries = [e for e in entries if query in (e.get("content", "") + " " + e.get("scope", "") + " " + " ".join(e.get("tags", []))).lower()]
    return _json_response(entries[:500])


async def context_post(request):
    try:
        body = await request.json()
    except Exception:
        return _json_response({"error": "invalid json"}, 400)
    content = _normalize_context_content(body.get("content", ""))
    if not content:
        return _json_response({"error": "empty"}, 400)
    tags = body.get("tags", [])
    if isinstance(tags, str):
        tags = [x.strip() for x in tags.split(",")]
    if not isinstance(tags, list):
        tags = []
    try:
        importance = int(body.get("importance", 8))
    except (TypeError, ValueError):
        importance = 8
    now = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    entry = {
        "id": str(_uuid.uuid4())[:8],
        "scope": str(body.get("scope") or "global")[:80],
        "visibility": str(body.get("visibility") or "shared")[:32],
        "importance": max(1, min(importance, 10)),
        "content": content,
        "source_user_id": str(body.get("source_user_id") or "admin")[:64],
        "source_channel_id": str(body.get("source_channel_id") or "dashboard")[:64],
        "source_guild_id": str(body.get("source_guild_id") or "")[:64],
        "source_kind": "admin",
        "tags": [str(t).strip()[:32] for t in tags if str(t).strip()][:12],
        "created_at": now,
        "last_seen_at": now,
        "expires_at": str(body.get("expires_at") or "")[:64],
    }
    async with _file_lock:
        try:
            entries = _load_context_entries_for_write()
        except ValueError as exc:
            return _json_response({"error": str(exc)}, 409)
        entries.insert(0, entry)
        await _save_context_entries(entries)
    return _json_response({"ok": True, "id": entry["id"], "entry": entry})


async def context_put(request):
    try:
        body = await request.json()
    except Exception:
        return _json_response({"error": "invalid json"}, 400)
    context_id = str(body.get("id") or "").strip()
    if not context_id:
        return _json_response({"error": "id required"}, 400)
    allowed = {"scope", "visibility", "importance", "content", "tags", "expires_at"}
    async with _file_lock:
        try:
            entries = _load_context_entries_for_write()
        except ValueError as exc:
            return _json_response({"error": str(exc)}, 409)
        for entry in entries:
            if str(entry.get("id")) == context_id:
                for key in allowed:
                    if key in body:
                        entry[key] = _normalize_context_content(body[key]) if key == "content" else body[key]
                entry["last_seen_at"] = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
                await _save_context_entries(entries)
                return _json_response({"ok": True, "entry": entry})
    return _json_response({"error": "not found"}, 404)


async def context_delete(request):
    context_id = str(request.query.get("id", "")).strip()
    if not context_id:
        return _json_response({"error": "id required"}, 400)
    async with _file_lock:
        try:
            entries = _load_context_entries_for_write()
        except ValueError as exc:
            return _json_response({"error": str(exc)}, 409)
        kept = [e for e in entries if str(e.get("id")) != context_id]
        if len(kept) == len(entries):
            return _json_response({"error": "not found"}, 404)
        await _save_context_entries(kept)
    return _json_response({"ok": True})


# ---------- Prompts ----------
async def prompt_save(request):
    try:
        body = await request.json()
    except Exception:
        return _json_response({"error": "invalid json"}, 400)
    pid = _clean_id(body.get("id", ""))
    text = str(body.get("text", "")).strip()[:MAX_PROMPT_CHARS]
    if not pid:
        return _json_response({"error": "no id"}, 400)
    path = DATA_DIR / "prompts.json"
    async with _file_lock:
        try:
            p = _load_for_write(path, dict, {})
        except ValueError as exc:
            return _json_response({"error": str(exc)}, 409)
        if not text:
            p.pop(pid, None)
        else:
            p[pid] = text
        await atomic_json_write(path, p)
    return _json_response({"ok": True})


async def prompt_delete(request):
    pid = _clean_id(request.query.get("id", ""))
    if not pid:
        return _json_response({"error": "no id"}, 400)
    path = DATA_DIR / "prompts.json"
    async with _file_lock:
        try:
            p = _load_for_write(path, dict, {})
        except ValueError as exc:
            return _json_response({"error": str(exc)}, 409)
        if pid not in p:
            return _json_response({"error": "not found"}, 404)
        p.pop(pid, None)
        await atomic_json_write(path, p)
    return _json_response({"ok": True})


# ---------- Blacklist ----------
async def blacklist_post(request):
    try:
        body = await request.json()
    except Exception:
        return _json_response({"error": "invalid json"}, 400)
    uid = _clean_id(body.get("id", ""))
    if not uid:
        return _json_response({"error": "empty"}, 400)
    path = DATA_DIR / "blacklist.json"
    async with _file_lock:
        try:
            bl = _load_for_write(path, list, [])
        except ValueError as exc:
            return _json_response({"error": str(exc)}, 409)
        if uid not in bl:
            bl.append(uid)
            await atomic_json_write(path, bl)
    return _json_response({"ok": True})


async def blacklist_del(request):
    uid = _clean_id(request.query.get("id", ""))
    path = DATA_DIR / "blacklist.json"
    async with _file_lock:
        try:
            bl = _load_for_write(path, list, [])
        except ValueError as exc:
            return _json_response({"error": str(exc)}, 409)
        if uid not in bl:
            return _json_response({"error": "not found"}, 404)
        bl.remove(uid)
        await atomic_json_write(path, bl)
    return _json_response({"ok": True})


# ---------- Auto channels ----------
async def auto_channel_post(request):
    try:
        body = await request.json()
    except Exception:
        return _json_response({"error": "invalid json"}, 400)
    cid = _clean_id(body.get("id", ""))
    if not cid:
        return _json_response({"error": "empty"}, 400)
    path = DATA_DIR / "auto_channels.json"
    async with _file_lock:
        try:
            channels = [str(x) for x in _load_for_write(path, list, [])]
        except ValueError as exc:
            return _json_response({"error": str(exc)}, 409)
        if cid not in channels:
            channels.append(cid)
            await atomic_json_write(path, channels)
    return _json_response({"ok": True})


async def auto_channel_del(request):
    cid = _clean_id(request.query.get("id", ""))
    path = DATA_DIR / "auto_channels.json"
    async with _file_lock:
        try:
            channels = [str(x) for x in _load_for_write(path, list, [])]
        except ValueError as exc:
            return _json_response({"error": str(exc)}, 409)
        if cid not in channels:
            return _json_response({"error": "not found"}, 404)
        channels.remove(cid)
        await atomic_json_write(path, channels)
    return _json_response({"ok": True})


def _safe_site_slug(value: str) -> str:
    slug = str(value or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9-]{2,30}", slug):
        return ""
    return slug


async def site_update(request):
    try:
        body = await request.json()
    except Exception:
        return _json_response({"error": "invalid json"}, 400)
    slug = _safe_site_slug(body.get("slug", ""))
    if not slug:
        return _json_response({"error": "bad slug"}, 400)
    path = DATA_DIR / "sites.json"
    async with _file_lock:
        try:
            sites = _load_for_write(path, dict, {})
        except ValueError as exc:
            return _json_response({"error": str(exc)}, 409)
        if slug not in sites or not isinstance(sites.get(slug), dict):
            return _json_response({"error": "not found"}, 404)
        site = dict(sites[slug])
        if "title" in body:
            site["title"] = str(body.get("title") or "untitled")[:200]
        if body.get("extend_24h"):
            site["created_at"] = time.time()
        sites[slug] = site
        await atomic_json_write(path, sites)
    return _json_response({"ok": True, "site": site})


async def site_delete(request):
    slug = _safe_site_slug(request.query.get("slug", ""))
    if not slug:
        return _json_response({"error": "bad slug"}, 400)
    site_dir = (BASE_SITE_DIR / slug).resolve()
    if BASE_SITE_DIR not in site_dir.parents and site_dir != BASE_SITE_DIR:
        return _json_response({"error": "bad path"}, 400)
    path = DATA_DIR / "sites.json"
    async with _file_lock:
        try:
            sites = _load_for_write(path, dict, {})
        except ValueError as exc:
            return _json_response({"error": str(exc)}, 409)
        if slug not in sites:
            return _json_response({"error": "not found"}, 404)
        sites.pop(slug, None)
        await atomic_json_write(path, sites)
    if site_dir.exists():
        await asyncio.to_thread(shutil.rmtree, site_dir)
    return _json_response({"ok": True})


# ---------- Runtime controls ----------
async def control_put(request):
    try:
        body = await request.json()
    except Exception:
        return _json_response({"error": "invalid json"}, 400)
    if not isinstance(body, dict):
        return _json_response({"error": "invalid control"}, 400)
    async with _file_lock:
        try:
            current = dict(DEFAULT_CONTROL)
            current.update(_load_for_write(_control_path(), dict, {}))
        except ValueError as exc:
            return _json_response({"error": str(exc)}, 409)
        current.update({k: v for k, v in body.items() if k in DEFAULT_CONTROL})
        control = _sanitize_control(current)
        await atomic_json_write(_control_path(), control)
    return _json_response({"ok": True, "control": control})


async def control_reset(request):
    async with _file_lock:
        await atomic_json_write(_control_path(), DEFAULT_CONTROL)
    return _json_response({"ok": True, "control": dict(DEFAULT_CONTROL)})


# ---------- REM ----------


def _llm_traces_path():
    return DATA_DIR / "llm_traces.json"


async def llm_traces(request):
    if not _has_admin_auth(request):
        return _json_response({"error": "unauthorized"}, 401)
    traces = _safe_list(_load(_llm_traces_path()))
    limit = _int_env_safe("MAXWELL_TRACE_API_LIMIT", 200)
    try:
        q = int(request.query.get("limit", limit))
        limit = max(1, min(q, 1000))
    except Exception:
        pass
    return _json_response(traces[-limit:])

async def rem_status(request):
    if not _has_admin_auth(request):
        return _json_response({"error": "unauthorized"}, 401)
    return _json_response(_load_rem_status())


async def rem_runs(request):
    if not _has_admin_auth(request):
        return _json_response({"error": "unauthorized"}, 401)
    runs = _safe_list(_load(_rem_runs_path()))
    try:
        limit = max(1, min(int(request.query.get("limit", "50")), 200))
    except (TypeError, ValueError):
        limit = 50
    try:
        offset = max(0, int(request.query.get("offset", "0")))
    except (TypeError, ValueError):
        offset = 0
    ordered = list(reversed(runs))
    return _json_response({"items": ordered[offset:offset + limit], "total": len(runs), "offset": offset, "limit": limit})


async def _queue_rem_command(cmd_type: str):
    async with _file_lock:
        try:
            cmds = _load_commands_for_write()
        except ValueError as exc:
            return "", str(exc)
        cmd_id = str(_uuid.uuid4())[:8]
        cmds.append({
            "id": cmd_id,
            "type": cmd_type,
            "status": "pending",
            "result": "",
            "created_at": time.time(),
        })
        if len(cmds) > MAX_COMMANDS:
            cmds = cmds[-MAX_COMMANDS:]
        await atomic_json_write(_commands_path(), cmds)
    return cmd_id, ""


async def _queue_command(cmd_type: str, extra: dict | None = None):
    """Generic command queue helper (same pattern as _queue_rem_command)."""
    async with _file_lock:
        try:
            cmds = _load_commands_for_write()
        except ValueError as exc:
            return "", str(exc)
        cmd_id = str(_uuid.uuid4())[:8]
        entry = {
            "id": cmd_id,
            "type": cmd_type,
            "status": "pending",
            "result": "",
            "created_at": time.time(),
        }
        if extra:
            entry.update(extra)
        cmds.append(entry)
        if len(cmds) > MAX_COMMANDS:
            cmds = cmds[-MAX_COMMANDS:]
        await atomic_json_write(_commands_path(), cmds)
    return cmd_id, ""


async def rem_run(request):
    status = _load_rem_status()
    if status.get("running"):
        return _json_response({"ok": True, "started": False, "reason": "already running"})
    cmd_id, err = await _queue_rem_command("rem_run")
    if err:
        return _json_response({"error": err}, 409)
    return _json_response({"ok": True, "started": True, "id": cmd_id})


async def _set_rem_enabled(enabled: bool, cmd_type: str):
    async with _file_lock:
        try:
            control = _load_rem_control_for_write()
            cmds = _load_commands_for_write()
        except ValueError as exc:
            return "", str(exc)
        control["enabled"] = enabled
        cmd_id = str(_uuid.uuid4())[:8]
        cmds.append({
            "id": cmd_id,
            "type": cmd_type,
            "status": "pending",
            "result": "",
            "created_at": time.time(),
        })
        if len(cmds) > MAX_COMMANDS:
            cmds = cmds[-MAX_COMMANDS:]
        await _save_rem_control(control)
        await atomic_json_write(_commands_path(), cmds)
        return cmd_id, ""


async def rem_enable(request):
    cmd_id, err = await _set_rem_enabled(True, "rem_enable")
    if err:
        return _json_response({"error": err}, 409)
    return _json_response({"ok": True, "enabled": True, "id": cmd_id})


async def rem_disable(request):
    cmd_id, err = await _set_rem_enabled(False, "rem_disable")
    if err:
        return _json_response({"error": err}, 409)
    return _json_response({"ok": True, "enabled": False, "id": cmd_id})


# ---------- Autonomy ----------


def _load_autonomy_state():
    return _safe_object(_load(_autonomy_state_path()))


def _load_autonomy_goals():
    data = _safe_object(_load(_autonomy_goals_path()))
    goals = data.get("goals", [])
    return goals if isinstance(goals, list) else []


def _load_autonomy_log():
    data = _safe_object(_load(_autonomy_log_path()))
    entries = data.get("entries", [])
    return entries if isinstance(entries, list) else []


async def autonomy_status(request):
    if not _has_admin_auth(request):
        return _json_response({"error": "unauthorized"}, 401)
    control = _load_control()
    state = _load_autonomy_state()
    return _json_response({
        "enabled": control.get("autonomy_enabled", False),
        "interval_seconds": control.get("autonomy_interval_seconds", 300),
        "last_tick": state.get("last_tick"),
        "last_tick_duration": state.get("last_tick_duration"),
        "actions_executed_total": state.get("actions_executed_total", 0),
        "actions_failed_total": state.get("actions_failed_total", 0),
        "last_error": state.get("last_error"),
        "last_thought": state.get("last_thought"),
    })


async def autonomy_log(request):
    if not _has_admin_auth(request):
        return _json_response({"error": "unauthorized"}, 401)
    entries = _load_autonomy_log()
    try:
        limit = max(1, min(int(request.query.get("limit", "200")), 500))
    except (TypeError, ValueError):
        limit = 200
    return _json_response({"entries": entries[-limit:]})


async def autonomy_goals(request):
    if not _has_admin_auth(request):
        return _json_response({"error": "unauthorized"}, 401)
    goals = _load_autonomy_goals()
    return _json_response({"goals": goals})


async def autonomy_run(request):
    if not _has_admin_auth(request):
        return _json_response({"error": "unauthorized"}, 401)
    cmd_id, err = await _queue_command("autonomy_run")
    if err:
        return _json_response({"error": err}, 409)
    return _json_response({"ok": True, "started": True, "id": cmd_id})


async def _set_autonomy_enabled(enabled: bool):
    async with _file_lock:
        try:
            control = dict(DEFAULT_CONTROL)
            loaded = _load_for_write(_control_path(), dict, {})
            control.update({k: v for k, v in loaded.items() if k in DEFAULT_CONTROL})
            control["autonomy_enabled"] = enabled
            control = _sanitize_control(control)
            cmds = _load_commands_for_write()
        except ValueError as exc:
            return "", str(exc)
        cmd_type = "autonomy_enable" if enabled else "autonomy_disable"
        cmd_id = str(_uuid.uuid4())[:8]
        cmds.append({
            "id": cmd_id,
            "type": cmd_type,
            "status": "pending",
            "result": "",
            "created_at": time.time(),
        })
        if len(cmds) > MAX_COMMANDS:
            cmds = cmds[-MAX_COMMANDS:]
        await atomic_json_write(_control_path(), control)
        await atomic_json_write(_commands_path(), cmds)
        return cmd_id, ""


async def autonomy_enable(request):
    if not _has_admin_auth(request):
        return _json_response({"error": "unauthorized"}, 401)
    cmd_id, err = await _set_autonomy_enabled(True)
    if err:
        return _json_response({"error": err}, 409)
    return _json_response({"ok": True, "enabled": True, "id": cmd_id})


async def autonomy_disable(request):
    if not _has_admin_auth(request):
        return _json_response({"error": "unauthorized"}, 401)
    cmd_id, err = await _set_autonomy_enabled(False)
    if err:
        return _json_response({"error": err}, 409)
    return _json_response({"ok": True, "enabled": False, "id": cmd_id})


async def autonomy_interval(request):
    if not _has_admin_auth(request):
        return _json_response({"error": "unauthorized"}, 401)
    try:
        body = await request.json()
    except Exception:
        return _json_response({"error": "invalid json"}, 400)
    try:
        new_interval = max(30, int(body.get("interval_seconds", 300)))
    except (TypeError, ValueError):
        return _json_response({"error": "invalid interval"}, 400)
    async with _file_lock:
        try:
            control = dict(DEFAULT_CONTROL)
            loaded = _load_for_write(_control_path(), dict, {})
            control.update({k: v for k, v in loaded.items() if k in DEFAULT_CONTROL})
            control["autonomy_interval_seconds"] = new_interval
            control = _sanitize_control(control)
            cmds = _load_commands_for_write()
        except ValueError as exc:
            return _json_response({"error": str(exc)}, 409)
        cmd_id = str(_uuid.uuid4())[:8]
        cmds.append({
            "id": cmd_id,
            "type": "autonomy_interval",
            "status": "pending",
            "interval_seconds": new_interval,
            "result": "",
            "created_at": time.time(),
        })
        if len(cmds) > MAX_COMMANDS:
            cmds = cmds[-MAX_COMMANDS:]
        await atomic_json_write(_control_path(), control)
        await atomic_json_write(_commands_path(), cmds)
    return _json_response({"ok": True, "interval_seconds": new_interval, "id": cmd_id})


async def autonomy_goal_add(request):
    if not _has_admin_auth(request):
        return _json_response({"error": "unauthorized"}, 401)
    try:
        body = await request.json()
    except Exception:
        return _json_response({"error": "invalid json"}, 400)
    description = str(body.get("description", "")).strip()[:500]
    if not description:
        return _json_response({"error": "description required"}, 400)
    goal = {
        "id": f"goal_{_uuid.uuid4().hex[:8]}",
        "description": description,
        "active": True,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
        "last_acted_on": None,
    }
    async with _file_lock:
        try:
            data = _load_for_write(_autonomy_goals_path(), dict, {})
        except ValueError as exc:
            return _json_response({"error": str(exc)}, 409)
        goals = data.get("goals", [])
        if not isinstance(goals, list):
            return _json_response({"error": "refusing to overwrite malformed autonomy_goals.json"}, 409)
        if len(goals) >= MAX_AUTONOMY_GOALS:
            return _json_response({"error": f"goal limit reached ({MAX_AUTONOMY_GOALS})"}, 409)
        goals.append(goal)
        await atomic_json_write(_autonomy_goals_path(), {"goals": goals[-MAX_AUTONOMY_GOALS:]})
    return _json_response({"ok": True, "goal": goal})


async def autonomy_goal_delete(request):
    if not _has_admin_auth(request):
        return _json_response({"error": "unauthorized"}, 401)
    goal_id = str(request.match_info.get("goal_id", "")).strip()
    if not goal_id:
        return _json_response({"error": "goal_id required"}, 400)
    async with _file_lock:
        try:
            data = _load_for_write(_autonomy_goals_path(), dict, {})
        except ValueError as exc:
            return _json_response({"error": str(exc)}, 409)
        goals = data.get("goals", [])
        if not isinstance(goals, list):
            return _json_response({"error": "refusing to overwrite malformed autonomy_goals.json"}, 409)
        before = len(goals)
        goals = [g for g in goals if g.get("id") != goal_id]
        if len(goals) == before:
            return _json_response({"error": "not found"}, 404)
        await atomic_json_write(_autonomy_goals_path(), {"goals": goals})
    return _json_response({"ok": True})


async def autonomy_log_clear(request):
    if not _has_admin_auth(request):
        return _json_response({"error": "unauthorized"}, 401)
    await atomic_json_write(_autonomy_log_path(), {"entries": []})
    return _json_response({"ok": True})


# ---------- Command queue ----------
def _commands_path():
    return DATA_DIR / "bot_commands.json"


def _load_commands():
    return _safe_list(_load(_commands_path()))


def _load_commands_for_write():
    return _load_for_write(_commands_path(), list, [])


async def commands_post(request):
    try:
        body = await request.json()
    except Exception:
        return _json_response({"error": "invalid json"}, 400)
    cmd_type = str(body.get("type", "")).strip()
    if not cmd_type:
        return _json_response({"error": "type is required"}, 400)
    cmd_id = str(_uuid.uuid4())[:8]
    command = {
        "id": cmd_id,
        "type": cmd_type,
        "status": "pending",
        "result": "",
        "created_at": time.time(),
    }
    if cmd_type == "send_message":
        command["channel_id"] = str(body.get("channel_id", "")).strip()
        command["content"] = str(body.get("content", ""))[:2000]
        if not command["channel_id"] or not command["content"]:
            return _json_response({"error": "channel_id and content required"}, 400)
    elif cmd_type == "send_dm":
        command["user_id"] = str(body.get("user_id", "")).strip()
        command["content"] = str(body.get("content", ""))[:2000]
        if not command["user_id"] or not command["content"]:
            return _json_response({"error": "user_id and content required"}, 400)
    elif cmd_type == "set_presence":
        # "status" is the queue lifecycle field. Do not reuse it for Discord presence.
        command["presence_status"] = str(body.get("status", "online")).strip()
        command["activity_type"] = str(body.get("activity_type", "")).strip()
        command["activity_text"] = str(body.get("activity_text", "")).strip()[:128]
    elif cmd_type == "set_custom_status":
        command["text"] = str(body.get("text", "")).strip()[:128]
    elif cmd_type == "change_avatar":
        command["url"] = str(body.get("url", "")).strip()[:2048]
    elif cmd_type == "shell":
        # Shell commands via web API are disabled for security.
        # Use the bot's Discord shell tool or SSH directly instead.
        return _json_response({"error": "shell commands are not allowed via the web API"}, 403)
    elif cmd_type == "clear_memory":
        command["channel_id"] = str(body.get("channel_id", "")).strip()
    elif cmd_type == "reload_controls":
        pass
    elif cmd_type in {"rem_run", "rem_enable", "rem_disable", "autonomy_run", "autonomy_enable", "autonomy_disable", "autonomy_interval"}:
        pass
    else:
        return _json_response({"error": f"unknown command type: {cmd_type}"}, 400)
    async with _file_lock:
        try:
            cmds = _load_commands_for_write()
        except ValueError as exc:
            return _json_response({"error": str(exc)}, 409)
        cmds.append(command)
        if len(cmds) > MAX_COMMANDS:
            cmds = cmds[-MAX_COMMANDS:]
        await atomic_json_write(_commands_path(), cmds)
    return _json_response({"ok": True, "id": cmd_id})


async def commands_get(request):
    if not _has_admin_auth(request):
        return _json_response({"error": "unauthorized"}, 401)
    cmds = _load_commands()
    return _json_response(cmds[-100:])


async def commands_del(request):
    cid = request.query.get("id", "")
    async with _file_lock:
        try:
            cmds = _load_commands_for_write()
        except ValueError as exc:
            return _json_response({"error": str(exc)}, 409)
        cmds = [c for c in cmds if c.get("id") != cid]
        await atomic_json_write(_commands_path(), cmds)
    return _json_response({"ok": True})


async def discord_state(request):
    if not _has_admin_auth(request):
        return _json_response({"error": "unauthorized"}, 401)
    state = _safe_object(_load(DATA_DIR / "discord_state.json"))
    return _json_response(state)


# ---------- PM2 / System ----------
_pm2_cache = None
_pm2_cache_time = 0.0

async def _pm2_json():
    global _pm2_cache, _pm2_cache_time
    now = time.time()
    if _pm2_cache is not None and (now - _pm2_cache_time) < 10.0:
        return _pm2_cache
    try:
        proc = await asyncio.create_subprocess_exec(
            "pm2", "jlist",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        data = json.loads(stdout.decode("utf-8", errors="replace"))
        _pm2_cache = data if isinstance(data, list) else []
        _pm2_cache_time = now
        return _pm2_cache
    except Exception:
        return _pm2_cache if _pm2_cache is not None else []


async def pm2_status(request):
    if not _has_admin_auth(request):
        return _json_response({"error": "unauthorized"}, 401)
    data = await _pm2_json()
    wanted = {"maxwell-bot", "maxwell-api"}
    out = []
    for proc in data:
        name = proc.get("name", "")
        if name not in wanted:
            continue
        env = proc.get("pm2_env", {})
        mon = proc.get("monit", {})
        out.append({
            "name": name,
            "pid": proc.get("pid"),
            "status": env.get("status"),
            "uptime": env.get("pm_uptime"),
            "restart_time": env.get("restart_time"),
            "cpu": mon.get("cpu"),
            "memory": mon.get("memory"),
        })
    return _json_response(out)


async def pm2_logs(request):
    if not _has_admin_auth(request):
        return _json_response({"error": "unauthorized"}, 401)
    process = request.query.get("process", "maxwell-bot")
    lines = request.query.get("lines", "30")
    try:
        lines_int = max(1, min(int(lines), 500))
    except (ValueError, TypeError):
        lines_int = 30
    if process not in {"maxwell-bot", "maxwell-api"}:
        return _json_response({"error": "bad process"}, 400)
    try:
        proc = await asyncio.create_subprocess_exec(
            "pm2", "logs", process, "--lines", str(lines_int), "--nostream",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        text = stdout.decode("utf-8", errors="replace")
        # Strip ANSI escape sequences for clean HTML display
        text = re.sub(r'\x1b\[[0-9;]*m', '', text)
        # Drop PM2 headers and log file labels
        lines_raw = text.splitlines()
        clean = []
        for ln in lines_raw:
            if ln.startswith('[TAILING]'):
                continue
            if ' last ' in ln and ' lines:' in ln:
                continue
            if ln.startswith('/root/.pm2/logs/'):
                continue
            clean.append(ln)
        text = "\n".join(clean)
        return _json_response({"process": process, "lines": lines_int, "log": text})
    except Exception:
        return _json_response({"error": "internal error"}, 500)


async def pm2_restart(request):
    if not _has_admin_auth(request):
        return _json_response({"error": "unauthorized"}, 401)
    target = request.query.get("target", "maxwell-bot")
    if target not in {"maxwell-bot", "maxwell-api", "all"}:
        return _json_response({"error": "bad target"}, 400)
    try:
        cmd = ["pm2", "restart", target] if target != "all" else ["pm2", "restart", "maxwell-bot", "maxwell-api"]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        text = (stdout + stderr).decode("utf-8", errors="replace")
        return _json_response({"ok": True, "output": text})
    except Exception:
        return _json_response({"error": "internal error"}, 500)


async def channel_list(request):
    if not _has_admin_auth(request):
        return _json_response({"error": "unauthorized"}, 401)
    mem = _safe_object(_load(DATA_DIR / "memory.json"))
    out = []
    for cid, msgs in mem.items():
        out.append({
            "id": str(cid),
            "messages": len(msgs) if isinstance(msgs, list) else 0,
            "last": msgs[-1].get("timestamp", "") if isinstance(msgs, list) and msgs else "",
        })
    out.sort(key=lambda x: x["messages"], reverse=True)
    return _json_response(out)


async def chat_history(request):
    if not _has_admin_auth(request):
        return _json_response({"error": "unauthorized"}, 401)
    cid = request.query.get("channel_id", "")
    if not cid:
        return _json_response({"error": "channel_id required"}, 400)
    mem = _safe_object(_load(DATA_DIR / "memory.json"))
    msgs = mem.get(cid, [])
    return _json_response(msgs[-100:])


async def bot_status(request):
    if not _has_admin_auth(request):
        return _json_response({"error": "unauthorized"}, 401)
    control = _load_control()
    mem = _safe_object(_load(DATA_DIR / "memory.json"))
    pm2 = await _pm2_json()
    bot_proc = next((p for p in pm2 if p.get("name") == "maxwell-bot"), None)
    api_proc = next((p for p in pm2 if p.get("name") == "maxwell-api"), None)
    return _json_response({
        "online": bool(bot_proc and bot_proc.get("pm2_env", {}).get("status") == "online"),
        "control": {k: control.get(k) for k in [
            "bot_enabled", "reply_dms", "reply_groups", "reply_mentions",
            "auto_mode_enabled", "tools_enabled", "store_memory", "cross_context_enabled",
            "cross_context_extract_enabled"
        ]},
        "stats": {
            "channels": len(mem),
            "messages": sum(len(v) for v in mem.values() if isinstance(v, list)),
            "context": len(_load_context_entries()),
        },
        "pm2": {
            "bot": {
                "status": bot_proc.get("pm2_env", {}).get("status") if bot_proc else "unknown",
                "uptime": bot_proc.get("pm2_env", {}).get("pm_uptime") if bot_proc else None,
                "restart_time": bot_proc.get("pm2_env", {}).get("restart_time") if bot_proc else None,
            },
            "api": {
                "status": api_proc.get("pm2_env", {}).get("status") if api_proc else "unknown",
                "uptime": api_proc.get("pm2_env", {}).get("pm_uptime") if api_proc else None,
            },
        },
    })


# ---------- Login ----------
async def login_post(request):
    """Validate dashboard credentials without persisting them."""
    try:
        body = await request.json()
    except Exception:
        return _json_response({"error": "invalid json"}, 400)
    user = str(body.get("user", "")).strip()
    pwd = str(body.get("pass", "")).strip()
    if not user or not pwd:
        return _json_response({"error": "user and pass required"}, 400)
    if not ADMIN_USER or not ADMIN_PASSWORD:
        return _json_response({"error": "admin auth not configured"}, 503)
    if not (hmac.compare_digest(user, ADMIN_USER) and hmac.compare_digest(pwd, ADMIN_PASSWORD)):
        _record_auth_failure(request)
        return _json_response({"error": "unauthorized"}, 401)
    return _json_response({"ok": True, "message": "credentials valid"})


# ---------- System Stats ----------
async def system_stats(request):
    if not _has_admin_auth(request):
        return _json_response({"error": "unauthorized"}, 401)
    try:
        loadavg = [f"{x:.2f}" for x in os.getloadavg()]
    except Exception:
        loadavg = ["0.00", "0.00", "0.00"]
    try:
        meminfo = Path("/proc/meminfo").read_text(encoding="utf-8")
        mem_total_kb = 0
        mem_avail_kb = 0
        for line in meminfo.splitlines():
            if line.startswith("MemTotal:"):
                mem_total_kb = int(line.split()[1])
            elif line.startswith("MemAvailable:"):
                mem_avail_kb = int(line.split()[1])
        mem_total = mem_total_kb // 1024
        mem_used = (mem_total_kb - mem_avail_kb) // 1024
    except Exception:
        mem_total, mem_used = 0, 0
    try:
        usage = shutil.disk_usage("/")
        disk_total = usage.total
        disk_used = usage.used
    except Exception:
        disk_total, disk_used = 0, 0
    uptime_seconds = 0
    try:
        uptime_text = Path("/proc/uptime").read_text(encoding="utf-8").strip()
        uptime_seconds = float(uptime_text.split()[0])
    except Exception:
        pass
    return _json_response({
        "load": loadavg,
        "memory": {"total_mb": mem_total, "used_mb": mem_used},
        "disk": {"total_bytes": disk_total, "used_bytes": disk_used},
        "uptime_seconds": round(uptime_seconds),
    })


# ---------- App ----------
app = web.Application(middlewares=[_auth_middleware_unless_login], client_max_size=256 * 1024)
app.router.add_get("/data/{file}", data_file)
app.router.add_options("/data/{file}", lambda r: web.Response(status=204, headers={"Access-Control-Allow-Origin": CORS_ORIGIN, "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS", "Access-Control-Allow-Headers": "Content-Type, Authorization"}))
app.router.add_post("/api/memory", memory_add)
app.router.add_put("/api/memory", memory_update)
app.router.add_delete("/api/memory", memory_delete)
app.router.add_options("/api/memory", lambda r: web.Response(status=204, headers={"Access-Control-Allow-Origin": CORS_ORIGIN, "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS", "Access-Control-Allow-Headers": "Content-Type, Authorization"}))
app.router.add_get("/api/context", context_get)
app.router.add_post("/api/context", context_post)
app.router.add_put("/api/context", context_put)
app.router.add_delete("/api/context", context_delete)
app.router.add_post("/api/prompts", prompt_save)
app.router.add_delete("/api/prompts", prompt_delete)
app.router.add_post("/api/blacklist", blacklist_post)
app.router.add_delete("/api/blacklist", blacklist_del)
app.router.add_post("/api/auto_channels", auto_channel_post)
app.router.add_delete("/api/auto_channels", auto_channel_del)
app.router.add_put("/api/sites", site_update)
app.router.add_delete("/api/sites", site_delete)
app.router.add_put("/api/control", control_put)
app.router.add_delete("/api/control", control_reset)
app.router.add_get("/api/llm/traces", llm_traces)
app.router.add_get("/api/rem/status", rem_status)
app.router.add_get("/api/rem/runs", rem_runs)
app.router.add_post("/api/rem/run", rem_run)
app.router.add_post("/api/rem/enable", rem_enable)
app.router.add_post("/api/rem/disable", rem_disable)
app.router.add_get("/api/autonomy/status", autonomy_status)
app.router.add_get("/api/autonomy/log", autonomy_log)
app.router.add_get("/api/autonomy/goals", autonomy_goals)
app.router.add_post("/api/autonomy/run", autonomy_run)
app.router.add_post("/api/autonomy/enable", autonomy_enable)
app.router.add_post("/api/autonomy/disable", autonomy_disable)
app.router.add_put("/api/autonomy/interval", autonomy_interval)
app.router.add_post("/api/autonomy/goals", autonomy_goal_add)
app.router.add_delete("/api/autonomy/goals/{goal_id}", autonomy_goal_delete)
app.router.add_delete("/api/autonomy/log", autonomy_log_clear)
app.router.add_get("/api/commands", commands_get)
app.router.add_post("/api/commands", commands_post)
app.router.add_delete("/api/commands", commands_del)
app.router.add_get("/api/discord/state", discord_state)
app.router.add_post("/api/login", login_post)
app.router.add_get("/api/pm2", pm2_status)
app.router.add_get("/api/pm2/logs", pm2_logs)
app.router.add_post("/api/pm2/restart", pm2_restart)
app.router.add_get("/api/channels", channel_list)
app.router.add_get("/api/chat/history", chat_history)
app.router.add_get("/api/status", bot_status)
app.router.add_get("/api/system", system_stats)
app.router.add_options("/api/{path:.*}", lambda r: web.Response(status=204, headers={"Access-Control-Allow-Origin": CORS_ORIGIN, "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS", "Access-Control-Allow-Headers": "Content-Type, Authorization"}))

if __name__ == "__main__":
    web.run_app(app, host=API_HOST, port=API_PORT, access_log=None)
