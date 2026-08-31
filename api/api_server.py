#!/usr/bin/env python3
"""Backend server for the Maxwell dashboard/admin API.

All API and data routes require Basic username/password auth by default.
"""

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import sqlite3
import hashlib
import time
import uuid as _uuid
from pathlib import Path

import aiohttp
from aiohttp import web

logger = logging.getLogger("maxwell_api")
logging.basicConfig(level=logging.INFO)

import sys as _sys  # noqa: E402

_sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.storage import (  # noqa: E402
    APP_ROOT,
    _autonomy_goals_path,
    _autonomy_log_path,
    _clean_id,
    _commands_path,
    _control_path,
    _int_env_safe,
    _llm_traces_path,
    _load,
    _load_for_write,
    _rem_runs_path,
    _safe_int,
    _safe_list,
    _safe_object,
    atomic_json_write,
)

from control_defaults import (  # noqa: E402
    DEFAULT_CONTROL,
    KNOWN_TOOLS,
)
from utils import (  # noqa: E402 - fd-safe atomic writes
    FileLock,
    FileLockTimeout,
    _atomic_json_write_sync,
)

DATA_DIR = Path(os.getenv("DATA_DIR", APP_ROOT / "data"))

# RAG vector memory SQLite DB (managed by rag_memory.RAGMemoryManager in the bot
# process). The API server opens it read/write for stats + LTM admin edits. We
# use a fresh connection per request with check_same_thread=False so we never
# share a cursor across the aiohttp event-loop's thread pool.
RAG_DB_PATH = DATA_DIR / "maxwell_rag.db"
RAG_EMBED_MODEL = os.getenv("MAXWELL_EMBED_MODEL", os.getenv("EMBED_MODEL", "qwen3-embedding:0.6b"))


def _rag_db() -> sqlite3.Connection:
    """Open a short-lived SQLite connection to the RAG vector DB.

    Caller is responsible for closing it (or use the `_rag_query`/`_rag_exec`
    helpers which close automatically). WAL mode means concurrent readers +
    one writer from the bot process coexist safely; busy_timeout prevents
    "database is locked" errors when the bot holds a write lock.
    """
    conn = sqlite3.connect(
        str(RAG_DB_PATH), check_same_thread=False, timeout=10.0
    )
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.Error:
        pass
    return conn


def _rag_query(sql: str, params: tuple = ()) -> list:
    """Run a SELECT and return all rows, closing the connection after."""
    conn = _rag_db()
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _rag_query_one(sql: str, params: tuple = ()):
    """Run a SELECT and return one row (or None), closing after."""
    conn = _rag_db()
    try:
        return conn.execute(sql, params).fetchone()
    finally:
        conn.close()


def _rag_exec(sql: str, params: tuple = ()) -> sqlite3.Cursor:
    """Run an INSERT/UPDATE/DELETE (autocommit), closing after.

    Returns the cursor so the caller can read .rowcount / .lastrowid before
    the connection is closed.
    """
    conn = _rag_db()
    try:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur
    finally:
        conn.close()


from api.config import (  # noqa: E402
    API_HOST,
    API_PORT,
    BASE_SITE_DIR,
    CORS_ORIGIN,
    DISCORD_CLIENT_ID,
    DISCORD_CLIENT_SECRET,
    DISCORD_TOKEN_TTL,
    MAX_AUTONOMY_GOALS,
    MAX_COMMANDS,
    MAX_PROMPT_CHARS,
)
import site_backend  # noqa: E402
import site_server  # noqa: E402

from api.auth import (  # noqa: E402
    _DISCORD_TOKENS,
    _auth_middleware_unless_login,
    _get_client_ip,
    _has_admin_auth,
    _json_response,
    _load_admin_creds,
    _load_bot_admins,
    _record_auth_failure,
    _safe_compare,
)

_load_admin_creds()
_file_lock = asyncio.Lock()

# Discord OAuth bearer tokens issued by the /api/auth/discord flow. Kept in
# process memory; users re-authenticate after a restart.


from api.state import (  # noqa: E402
    _load_autonomy_goals,
    _load_autonomy_log,
    _load_autonomy_state,
    _load_commands,
    _load_commands_for_write,
    _load_control,
    _load_inbox,
    _load_rem_control_for_write,
    _load_rem_status,
    _normalize_context_content,
    _normalize_memory_line,
    _save_rem_control,
    _sanitize_control,
)


# ---------- Data (all authenticated) ----------
async def data_file(request):
    file = request.match_info.get("file", "")
    if ".." in file or "/" in file or not file.endswith(".json"):
        return _json_response({"error": "bad file"}, 403)
    # All data files require auth. memory.json / long_term_memory.json removed —
    # memory now lives in the RAG SQLite DB, served via /api/rag/* endpoints.
    ALLOWED_FILES = {
        "sites.json",
        "prompts.json",
        "blacklist.json",
        "auto_channels.json",
        "bot_control.json",
    }
    if file not in ALLOWED_FILES:
        return _json_response({"error": "not allowed"}, 403)
    path = DATA_DIR / file
    if not path.exists():
        return _json_response([])
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, ValueError):
        return _json_response({"error": "read failed"}, 500)
    if isinstance(data, list):
        return _json_response(data[:500])
    return _json_response(data)


# ---------- RAG vector memory (replaces memory.json / long_term_memory.json) ----------
async def rag_memory_stats(request):
    """Per-channel message counts + overall RAG memory stats from SQLite.

    Replaces the old /data/memory.json fetch for the admin dashboard.
    """
    if not _has_admin_auth(request):
        return _json_response({"error": "unauthorized"}, 401)
    try:
        channels = _rag_query(
            "SELECT channel_id, COUNT(*) as c, "
            "MAX(timestamp) as last "
            "FROM vectors WHERE kind='message' GROUP BY channel_id"
        )
        total_vectors = _rag_query_one("SELECT COUNT(*) as c FROM vectors")
        embedded = _rag_query_one(
            "SELECT COUNT(*) as c FROM vectors WHERE embedding IS NOT NULL"
        )
        messages = _rag_query_one(
            "SELECT COUNT(*) as c FROM vectors WHERE kind='message'"
        )
        context = _rag_query_one(
            "SELECT COUNT(*) as c FROM vectors WHERE kind='shared_context'"
        )
        ltm = _rag_query_one("SELECT COUNT(*) as c FROM vectors WHERE kind='ltm'")
        chan_list = [
            {
                "id": row["channel_id"],
                "messages": row["c"],
                "last": row["last"] or "",
            }
            for row in channels
        ]
        chan_list.sort(key=lambda x: x["messages"], reverse=True)
        return _json_response(
            {
                "channels": chan_list,
                "channel_count": len(chan_list),
                "messages": messages["c"] if messages else 0,
                "context": context["c"] if context else 0,
                "ltm": ltm["c"] if ltm else 0,
                "total_vectors": total_vectors["c"] if total_vectors else 0,
                "embedded": embedded["c"] if embedded else 0,
                "pending_embeddings": (total_vectors["c"] if total_vectors else 0)
                - (embedded["c"] if embedded else 0),
                "embed_model": RAG_EMBED_MODEL,
            }
        )
    except sqlite3.Error as e:
        return _json_response({"error": f"rag db: {e}"}, 500)


async def rag_ltm_list(request):
    """Return all long-term-memory entries from the RAG SQLite DB.

    Replaces the old /data/long_term_memory.json fetch. Each entry has a
    UUID id, content, and timestamp.
    """
    if not _has_admin_auth(request):
        return _json_response({"error": "unauthorized"}, 401)
    try:
        rows = _rag_query(
            "SELECT id, content, timestamp FROM vectors "
            "WHERE kind='ltm' ORDER BY created_at ASC"
        )
        return _json_response(
            [
                {
                    "id": row["id"],
                    "content": row["content"],
                    "timestamp": row["timestamp"],
                }
                for row in rows
            ]
        )
    except sqlite3.Error as e:
        return _json_response({"error": f"rag db: {e}"}, 500)


async def rag_entities_list(request):
    """Global per-user entity memory: who the bot knows, and what about them.

    One row per Discord user id, independent of guild — the point of the tier
    is that it follows a person between servers and DMs. `?user_id=` returns
    one person with their facts; without it, the roster.
    """
    if not _has_admin_auth(request):
        return _json_response({"error": "unauthorized"}, 401)
    user_id = str(request.query.get("user_id") or "").strip()
    try:
        limit = max(1, min(_safe_int(request.query.get("limit"), 200), 1000))
        if user_id:
            rows = _rag_query(
                "SELECT * FROM user_entities WHERE user_id=?", (user_id,)
            )
            facts = _rag_query(
                "SELECT id, content, importance, timestamp, metadata FROM vectors "
                "WHERE kind='entity' AND author_id=? "
                "ORDER BY importance DESC, created_at DESC LIMIT 200",
                (user_id,),
            )
            return _json_response(
                {
                    "entity": _entity_row(rows[0]) if rows else {"user_id": user_id},
                    "facts": [
                        {
                            "id": f["id"],
                            "content": f["content"],
                            "importance": f["importance"],
                            "timestamp": f["timestamp"],
                        }
                        for f in facts
                    ],
                }
            )
        rows = _rag_query(
            "SELECT * FROM user_entities ORDER BY last_seen DESC LIMIT ?", (limit,)
        )
        counts = {
            str(r["author_id"]): r["c"]
            for r in _rag_query(
                "SELECT author_id, COUNT(*) AS c FROM vectors "
                "WHERE kind='entity' GROUP BY author_id"
            )
        }
        entities = []
        for row in rows:
            entity = _entity_row(row)
            entity["fact_count"] = counts.get(str(row["user_id"]), 0)
            entities.append(entity)
        return _json_response(
            {
                "entities": entities,
                "count": len(entities),
                "facts": sum(counts.values()),
            }
        )
    except sqlite3.Error as e:
        # The table is created by the bot on first run. A dashboard opened
        # against a database from before this feature should show an empty
        # panel, not a 500 that fails the whole page load.
        if "no such table" in str(e):
            return _json_response({"entities": [], "count": 0, "facts": 0})
        return _json_response({"error": f"rag db: {e}"}, 500)


def _entity_row(row) -> dict:
    def _load(raw, fallback):
        try:
            val = json.loads(raw or "")
            return val if isinstance(val, type(fallback)) else fallback
        except (ValueError, TypeError):
            return fallback

    return {
        "user_id": row["user_id"],
        "display_names": _load(row["display_names"], []),
        "guild_ids": _load(row["guild_ids"], []),
        "dm_seen": bool(row["dm_seen"]),
        "message_count": int(row["message_count"] or 0),
        "first_seen": row["first_seen"],
        "last_seen": row["last_seen"],
    }


async def context_get(request):
    """List shared-context facts from the live RAG SQLite DB.

    The old JSON file (`data/shared_context.json`) is a one-time migration
    source for the bot and is not written back. Dashboard edits must hit
    the same `kind='shared_context'` rows the bot injects into prompts.
    """
    if not _has_admin_auth(request):
        return _json_response({"error": "unauthorized"}, 401)
    try:
        rows = _rag_query(
            "SELECT id, content, scope, importance, timestamp, metadata "
            "FROM vectors WHERE kind='shared_context' "
            "ORDER BY importance DESC, created_at DESC"
        )
    except sqlite3.Error as e:
        return _json_response({"error": f"rag db: {e}"}, 500)
    entries = []
    for row in rows:
        entry = {
            "id": row["id"],
            "content": row["content"],
            "scope": row["scope"] or "global",
            "importance": row["importance"] or 5,
            "timestamp": row["timestamp"],
        }
        try:
            meta = json.loads(row["metadata"] or "{}")
            if isinstance(meta, dict):
                entry.update(meta)
        except Exception as e:
            # Corrupt metadata JSON: serve the row without its extras.
            logger.debug("Bad metadata JSON on row %s: %s", row["id"], e)
        entries.append(entry)
    return _json_response(entries)


async def context_post(request):
    try:
        body = await request.json()
    except Exception:
        return _json_response({"error": "invalid json"}, 400)
    if not isinstance(body, dict):
        return _json_response({"error": "body must be an object"}, 400)
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
    cid = str(_uuid.uuid4())[:8]
    visibility = str(body.get("visibility") or "shared")[:32]
    if visibility not in {"private", "shared", "admin_only", "public_hint"}:
        visibility = "shared"
    metadata = {
        "visibility": visibility,
        "source_user_id": str(body.get("source_user_id") or "admin")[:64],
        "source_channel_id": str(body.get("source_channel_id") or "dashboard")[:64],
        "source_guild_id": str(body.get("source_guild_id") or "")[:64],
        "source_kind": "admin",
        "tags": [str(t).strip()[:32] for t in tags if str(t).strip()][:12],
        "created_at": now,
        "last_seen_at": now,
        "expires_at": str(body.get("expires_at") or "")[:64],
    }
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    try:
        _rag_exec(
            "INSERT INTO vectors "
            "(id, kind, channel_id, guild_id, author, author_id, source, content, "
            "content_hash, embedding, metadata, scope, importance, parent_id, "
            "chunk_index, downvotes, timestamp, created_at) "
            "VALUES (?, 'shared_context', '', '', '', '', 'user', ?, ?, NULL, ?, ?, ?, "
            "'', 0, 0, ?, ?)",
            (
                cid,
                content,
                content_hash,
                json.dumps(metadata),
                str(body.get("scope") or "global")[:80],
                max(1, min(importance, 10)),
                now,
                time.time(),
            ),
        )
    except sqlite3.Error as e:
        return _json_response({"error": f"rag db: {e}"}, 500)
    entry = {"id": cid, "content": content, "scope": str(body.get("scope") or "global"), **metadata}
    return _json_response({"ok": True, "id": cid, "entry": entry})


async def context_put(request):
    try:
        body = await request.json()
    except Exception:
        return _json_response({"error": "invalid json"}, 400)
    if not isinstance(body, dict):
        return _json_response({"error": "body must be an object"}, 400)
    context_id = str(body.get("id") or "").strip()
    if not context_id:
        return _json_response({"error": "id required"}, 400)
    allowed = {"scope", "visibility", "importance", "content", "tags", "expires_at"}
    try:
        row = _rag_query_one(
            "SELECT id, content, scope, importance, timestamp, metadata "
            "FROM vectors WHERE id=? AND kind='shared_context'",
            (context_id,),
        )
    except sqlite3.Error as e:
        return _json_response({"error": f"rag db: {e}"}, 500)
    if row is None:
        return _json_response({"error": "not found"}, 404)
    try:
        meta = json.loads(row["metadata"] or "{}")
        if not isinstance(meta, dict):
            meta = {}
    except Exception:
        meta = {}
    content = row["content"]
    scope = row["scope"] or "global"
    importance = row["importance"] or 5
    sets = []
    params: list = []
    for key in allowed:
        if key not in body:
            continue
        if key == "content":
            content = _normalize_context_content(body[key])
            sets.append("content=?")
            params.append(content)
            sets.append("content_hash=?")
            params.append(hashlib.sha256(content.encode("utf-8")).hexdigest())
            sets.append("embedding=NULL")
        elif key == "scope":
            scope = str(body[key] or "global")[:80]
            sets.append("scope=?")
            params.append(scope)
        elif key == "importance":
            try:
                importance = max(1, min(int(body[key]), 10))
            except (TypeError, ValueError):
                continue
            sets.append("importance=?")
            params.append(importance)
        else:
            meta[key] = body[key]
    meta["last_seen_at"] = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    sets.append("metadata=?")
    params.append(json.dumps(meta))
    params.append(context_id)
    try:
        _rag_exec(
            f"UPDATE vectors SET {', '.join(sets)} WHERE id=? AND kind='shared_context'",
            tuple(params),
        )
    except sqlite3.Error as e:
        return _json_response({"error": f"rag db: {e}"}, 500)
    entry = {
        "id": context_id,
        "content": content,
        "scope": scope,
        "importance": importance,
        "timestamp": row["timestamp"],
        **meta,
    }
    return _json_response({"ok": True, "entry": entry})


async def context_delete(request):
    context_id = str(request.query.get("id", "")).strip()
    if not context_id:
        return _json_response({"error": "id required"}, 400)
    try:
        cur = _rag_exec(
            "DELETE FROM vectors WHERE id=? AND kind='shared_context'",
            (context_id,),
        )
    except sqlite3.Error as e:
        return _json_response({"error": f"rag db: {e}"}, 500)
    if cur.rowcount == 0:
        return _json_response({"error": "not found"}, 404)
    return _json_response({"ok": True})


# ---------- Prompts ----------
async def prompt_save(request):
    try:
        body = await request.json()
    except Exception:
        return _json_response({"error": "invalid json"}, 400)
    if not isinstance(body, dict):
        return _json_response({"error": "body must be an object"}, 400)
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
    if not isinstance(body, dict):
        return _json_response({"error": "body must be an object"}, 400)
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
    if not isinstance(body, dict):
        return _json_response({"error": "body must be an object"}, 400)
    # Accept both `id` (older clients) and `channel_id` (admin dashboard).
    cid = _clean_id(body.get("id") or body.get("channel_id") or "")
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
    # Accept both `id` (older clients) and `channel_id` (admin dashboard).
    cid = _clean_id(request.query.get("id") or request.query.get("channel_id") or "")
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
    if not isinstance(body, dict):
        return _json_response({"error": "body must be an object"}, 400)
    slug = _safe_site_slug(body.get("slug", ""))
    if not slug:
        return _json_response({"error": "bad slug"}, 400)
    path = DATA_DIR / "sites.json"

    def _do_update():
        # Cross-process FileLock so this RMW can't lose a concurrent
        # create_site commit (bot process) or vice versa.
        with FileLock(path, timeout=15.0):
            try:
                sites = _load_for_write(path, dict, {})
            except ValueError as exc:
                return ("err", str(exc), 409)
            if (
                not isinstance(sites, dict)
                or slug not in sites
                or not isinstance(sites.get(slug), dict)
            ):
                return ("notfound", None, 404)
            site = dict(sites[slug])
            if "title" in body:
                site["title"] = str(body.get("title") or "untitled")[:200]
            if body.get("extend_24h"):
                site["created_at"] = time.time()
            sites[slug] = site
            _atomic_json_write_sync(path, sites)
            return ("ok", site, 200)

    try:
        kind, payload, code = await asyncio.to_thread(_do_update)
    except FileLockTimeout as exc:
        return _json_response({"error": str(exc)}, 409)
    except OSError as exc:
        return _json_response({"error": f"site update failed: {exc}"}, 500)
    if kind == "err":
        return _json_response({"error": payload}, code)
    if kind == "notfound":
        return _json_response({"error": "not found"}, code)
    return _json_response({"ok": True, "site": payload})


async def site_delete(request):
    slug = _safe_site_slug(request.query.get("slug", ""))
    if not slug:
        return _json_response({"error": "bad slug"}, 400)
    site_dir = (BASE_SITE_DIR / slug).resolve()
    if BASE_SITE_DIR not in site_dir.parents and site_dir != BASE_SITE_DIR:
        return _json_response({"error": "bad path"}, 400)
    path = DATA_DIR / "sites.json"

    def _do_delete():
        # Cross-process FileLock (see site_update). Returns whether the slug
        # existed so we only rmtree a dir we actually owned in the metadata.
        with FileLock(path, timeout=15.0):
            try:
                sites = _load_for_write(path, dict, {})
            except ValueError as exc:
                return ("err", str(exc), 409)
            if not isinstance(sites, dict) or slug not in sites:
                return ("notfound", None, 404)
            sites.pop(slug, None)
            _atomic_json_write_sync(path, sites)
            return ("ok", None, 200)

    try:
        kind, payload, code = await asyncio.to_thread(_do_delete)
    except FileLockTimeout as exc:
        return _json_response({"error": str(exc)}, 409)
    except OSError as exc:
        return _json_response({"error": f"site delete failed: {exc}"}, 500)
    if kind == "err":
        return _json_response({"error": payload}, code)
    if kind == "notfound":
        return _json_response({"error": "not found"}, code)
    if site_dir.exists():
        await asyncio.to_thread(shutil.rmtree, site_dir)
    # Drop the site's backend store too, or a slug reused later inherits the
    # old site's guestbook.
    await asyncio.to_thread(site_backend.destroy, DATA_DIR, slug)
    # The real per-site server has its own code, data, image, container, and
    # registry row; deleting only the static site would leave that service
    # running and reachable through the proxy.
    await site_server.destroy(DATA_DIR, slug)
    return _json_response({"ok": True})


# ---------- Public site backend (no auth — a visitor's browser calls this) ----------
# Static pages made by create_site get a server side here: named values and
# append-only lists, same origin as the page, so a guestbook or a highscore
# table is a fetch() away. Only sites created with backend=true are served,
# everything is size-capped, and writes are rate-limited per IP because these
# routes are deliberately unauthenticated. See site_backend.py.
_SITE_RATE = site_backend.RateLimiter(rate=2.0, burst=40)
_SITE_READ_RATE = site_backend.RateLimiter(rate=10.0, burst=120)


def _site_json(data, status=200):
    return web.json_response(
        data,
        status=status,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
            "Cache-Control": "no-store",
        },
    )


def _site_backend_enabled(slug: str) -> bool:
    """A store exists only for a site whose metadata opted into one."""
    try:
        sites = json.loads((DATA_DIR / "sites.json").read_text(encoding="utf-8"))
    except (
        FileNotFoundError,
        json.JSONDecodeError,
        OSError,
        UnicodeError,
        ValueError,
    ):
        return False
    entry = sites.get(slug) if isinstance(sites, dict) else None
    return bool(isinstance(entry, dict) and entry.get("backend"))


def _site_server_enabled(slug: str) -> bool:
    """A proxy target is valid only while its site still exists and owns it."""
    try:
        sites = json.loads((DATA_DIR / "sites.json").read_text(encoding="utf-8"))
    except (
        FileNotFoundError,
        json.JSONDecodeError,
        OSError,
        UnicodeError,
        ValueError,
    ):
        return False
    entry = sites.get(slug) if isinstance(sites, dict) else None
    # ``server`` is set only after site_server.start() succeeds. Requiring the
    # metadata flag prevents an orphaned registry row from keeping a deleted
    # or never-published backend reachable.
    return bool(isinstance(entry, dict) and entry.get("server") is True)


async def _site_guard(request, write: bool):
    """(slug, None) when the call may proceed, else (None, error response)."""
    slug = _safe_site_slug(request.match_info.get("slug", ""))
    if not slug:
        return None, _site_json({"error": "bad slug"}, 404)
    limiter = _SITE_RATE if write else _SITE_READ_RATE
    if not limiter.allow(f"{_get_client_ip(request)}:{slug}"):
        return None, _site_json({"error": "slow down"}, 429)
    if not await asyncio.to_thread(_site_backend_enabled, slug):
        return None, _site_json(
            {"error": "this site has no backend (create it with backend=true)"}, 404
        )
    return slug, None


async def _site_body(request):
    try:
        return await request.json(), None
    except Exception:
        return None, _site_json({"error": "invalid json"}, 400)


def _site_error(exc: site_backend.SiteBackendError):
    return _site_json({"error": exc.message}, exc.status)


async def site_kv_get(request):
    slug, err = await _site_guard(request, write=False)
    if err:
        return err
    key = request.query.get("key")
    try:
        if key is None:
            return _site_json(await asyncio.to_thread(site_backend.kv_get, DATA_DIR, slug))
        value = await asyncio.to_thread(site_backend.kv_get, DATA_DIR, slug, key)
    except site_backend.SiteBackendError as exc:
        return _site_error(exc)
    return _site_json({"key": key, "value": value})


async def site_kv_put(request):
    slug, err = await _site_guard(request, write=True)
    if err:
        return err
    body, err = await _site_body(request)
    if err:
        return err
    if not isinstance(body, dict) or "key" not in body:
        return _site_json({"error": 'body must be {"key": ..., "value": ...}'}, 400)
    try:
        out = await asyncio.to_thread(
            site_backend.kv_set, DATA_DIR, slug, body.get("key"), body.get("value")
        )
    except site_backend.SiteBackendError as exc:
        return _site_error(exc)
    return _site_json({"ok": True, **out})


async def site_kv_bump(request):
    slug, err = await _site_guard(request, write=True)
    if err:
        return err
    body, err = await _site_body(request)
    if err:
        return err
    if not isinstance(body, dict) or "key" not in body:
        return _site_json({"error": 'body must be {"key": ..., "by": 1}'}, 400)
    try:
        value = await asyncio.to_thread(
            site_backend.kv_bump, DATA_DIR, slug, body.get("key"), body.get("by", 1)
        )
    except site_backend.SiteBackendError as exc:
        return _site_error(exc)
    return _site_json({"ok": True, "key": body.get("key"), "value": value})


async def site_kv_delete(request):
    slug, err = await _site_guard(request, write=True)
    if err:
        return err
    key = request.query.get("key", "")
    try:
        existed = await asyncio.to_thread(site_backend.kv_delete, DATA_DIR, slug, key)
    except site_backend.SiteBackendError as exc:
        return _site_error(exc)
    return _site_json({"ok": True, "deleted": existed})


async def site_items_get(request):
    slug, err = await _site_guard(request, write=False)
    if err:
        return err
    try:
        items = await asyncio.to_thread(
            site_backend.items_list,
            DATA_DIR,
            slug,
            request.match_info.get("name", ""),
            request.query.get("limit", 100),
            request.query.get("after"),
        )
    except site_backend.SiteBackendError as exc:
        return _site_error(exc)
    return _site_json({"items": items})


async def site_items_post(request):
    slug, err = await _site_guard(request, write=True)
    if err:
        return err
    body, err = await _site_body(request)
    if err:
        return err
    try:
        item = await asyncio.to_thread(
            site_backend.items_add, DATA_DIR, slug, request.match_info.get("name", ""), body
        )
    except site_backend.SiteBackendError as exc:
        return _site_error(exc)
    return _site_json({"ok": True, "item": item})


async def site_items_delete(request):
    slug, err = await _site_guard(request, write=True)
    if err:
        return err
    try:
        removed = await asyncio.to_thread(
            site_backend.items_delete,
            DATA_DIR,
            slug,
            request.match_info.get("name", ""),
            request.query.get("id"),
            str(request.query.get("all", "")).lower() in {"1", "true", "yes"},
        )
    except site_backend.SiteBackendError as exc:
        return _site_error(exc)
    return _site_json({"ok": True, "removed": removed})


# ---------- Site backend servers (proxy to the per-site container) ----------
# /bot/<slug>/api/<path> is the public face of a site's own server (see
# site_server.py). Caddy sends that path here; we look the slug's loopback port
# up in the registry and pass the request through with the prefix stripped, so
# a route the site defines as /notes is reached at /bot/<slug>/api/notes.
#
# Only the registry can name a destination, and only ever 127.0.0.1 on a port
# this process assigned — a slug cannot steer the proxy anywhere else.
_SITE_PROXY_RATE = site_backend.RateLimiter(rate=20.0, burst=200)
_SITE_PROXY_HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}
# Uploads are bounded by the /bot sub-app's client_max_size, not by holding the
# body in memory here. sock_read is a per-chunk idle limit, not a total: an SSE
# stream that sends a heartbeat every 20s stays open forever, as it should.
SITE_PROXY_READ_TIMEOUT = 120
SITE_UPLOAD_MAX = 32 * 1024 * 1024
SITE_WS_MAX_MSG = 4 * 1024 * 1024


async def _proxy_websocket(request, slug: str, target: str):
    """Pump a WebSocket both ways between the visitor and the site's app.

    This is what multiplayer runs on: a browser opens a socket to
    /bot/<slug>/api/ws, and it lands on the site's own server. Both directions
    are relayed until either end hangs up.

    Upstream is connected FIRST, on purpose. Preparing the client socket before
    knowing whether the backend is even reachable turns every failure into an
    accepted-then-closed connection, which the browser reports as a clean
    close — an error the caller cannot tell from a normal hangup.
    """
    requested = [
        p.strip()
        for p in (request.headers.get("Sec-WebSocket-Protocol") or "").split(",")
        if p.strip()
    ]
    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in _SITE_PROXY_HOP_HEADERS
        and not k.lower().startswith("sec-websocket-")
    }
    headers["X-Forwarded-For"] = _get_client_ip(request)
    headers["X-Forwarded-Prefix"] = f"/bot/{slug}/api"
    headers["X-Site-Slug"] = slug

    session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=None, sock_connect=10)
    )
    try:
        upstream = await session.ws_connect(
            target.replace("http://", "ws://", 1),
            headers=headers,
            protocols=requested,
            max_msg_size=SITE_WS_MAX_MSG,
            heartbeat=30,
            autoclose=False,
        )
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        await session.close()
        logger.warning("site %s websocket upstream failed: %s", slug, e)
        return _site_json({"error": "the site backend refused the websocket"}, 502)

    # Mirror whatever subprotocol the backend actually chose.
    chosen = upstream.protocol
    client = web.WebSocketResponse(
        heartbeat=30,
        max_msg_size=SITE_WS_MAX_MSG,
        protocols=[chosen] if chosen else (),
    )
    if not client.can_prepare(request).ok:
        await upstream.close()
        await session.close()
        return _site_json({"error": "not a websocket request"}, 400)
    await client.prepare(request)

    async def pump(src, dst, direction):
        try:
            async for msg in src:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await dst.send_str(msg.data)
                elif msg.type == aiohttp.WSMsgType.BINARY:
                    await dst.send_bytes(msg.data)
                elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING):
                    break
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    logger.warning("site %s ws %s error: %s", slug, direction, src.exception())
                    break
        except Exception as e:
            # Never silent: a bug in here used to look exactly like a browser
            # closing the tab.
            logger.warning("site %s ws %s pump failed: %r", slug, direction, e)
        finally:
            # One side hanging up ends the other; nothing is left half-open.
            with contextlib.suppress(Exception):
                await dst.close()

    try:
        await asyncio.gather(
            pump(client, upstream, "client->backend"),
            pump(upstream, client, "backend->client"),
        )
    finally:
        with contextlib.suppress(Exception):
            await upstream.close()
        with contextlib.suppress(Exception):
            await session.close()
        with contextlib.suppress(Exception):
            await client.close()
    return client


async def site_proxy(request):
    slug = _safe_site_slug(request.match_info.get("slug", ""))
    if not slug:
        return _site_json({"error": "bad slug"}, 404)
    if not _SITE_PROXY_RATE.allow(f"{_get_client_ip(request)}:{slug}"):
        return _site_json({"error": "slow down"}, 429)
    if not await asyncio.to_thread(_site_server_enabled, slug):
        return _site_json(
            {"error": "this site has no backend server running"}, 404
        )
    port = await asyncio.to_thread(site_server.port_for, DATA_DIR, slug)
    if not port:
        return _site_json(
            {"error": "this site has no backend server running"}, 404
        )
    tail = request.match_info.get("path", "") or ""
    target = f"http://127.0.0.1:{port}/{tail.lstrip('/')}"
    if request.query_string:
        target += "?" + request.query_string

    # WebSocket upgrade: hand off to the socket pump and never come back here.
    if (
        request.headers.get("Upgrade", "").lower() == "websocket"
        and request.method == "GET"
    ):
        return await _proxy_websocket(request, slug, target)

    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in _SITE_PROXY_HOP_HEADERS
    }
    # The backend gets to know where it really is, and who is really calling.
    headers["X-Forwarded-For"] = _get_client_ip(request)
    headers["X-Forwarded-Prefix"] = f"/bot/{slug}/api"
    headers["X-Site-Slug"] = slug

    # Reject an oversize upload from its Content-Length instead of streaming
    # 40MB only to fail mid-body — which surfaced as a confusing 502.
    declared = request.content_length or 0
    if declared > SITE_UPLOAD_MAX:
        return _site_json(
            {"error": f"upload too large (max {SITE_UPLOAD_MAX // (1024 * 1024)}MB)"},
            413,
        )
    # The body is streamed rather than buffered, so an upload is bounded by the
    # route's client_max_size instead of this process's memory.
    body = request.content if request.can_read_body else None

    # No total timeout: an SSE stream or a slow download is a legitimate long
    # response. sock_read still kills a backend that stops sending mid-body.
    timeout = aiohttp.ClientTimeout(
        total=None, sock_connect=10, sock_read=SITE_PROXY_READ_TIMEOUT
    )
    session = aiohttp.ClientSession(timeout=timeout, auto_decompress=False)
    try:
        upstream = await session.request(
            request.method, target, headers=headers, data=body,
            allow_redirects=False,
        )
    except asyncio.TimeoutError:
        await session.close()
        return _site_json({"error": "the site backend timed out"}, 504)
    except aiohttp.ClientError as e:
        await session.close()
        logger.warning("site backend %s unreachable: %s", slug, e)
        return _site_json({"error": "the site backend is not responding"}, 502)

    try:
        # Stream the response through chunk by chunk. Buffering it whole would
        # break Server-Sent Events and long polling (the reply would only
        # arrive once the stream ended) and would hold a big download in RAM.
        out = web.StreamResponse(status=upstream.status)
        for key, value in upstream.headers.items():
            if key.lower() in _SITE_PROXY_HOP_HEADERS:
                continue
            out.headers[key] = value
        out.headers["Access-Control-Allow-Origin"] = "*"
        await out.prepare(request)
        async for chunk in upstream.content.iter_chunked(65536):
            await out.write(chunk)
        await out.write_eof()
        return out
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        logger.warning("site backend %s died mid-response: %s", slug, e)
        with contextlib.suppress(Exception):
            await out.write_eof()
        return out
    finally:
        upstream.release()
        await session.close()


# ---------- Runtime controls ----------
async def control_get(request):
    """Return the live control set: persisted values merged over DEFAULT_CONTROL
    and run through the same sanitizer a PUT goes through.

    The dashboard used to read /data/bot_control.json directly, which is only
    what has ever been *written*. Any key an operator never touched came back
    undefined, so its input rendered blank instead of showing the default the
    bot is actually running with.
    """
    return _json_response({"ok": True, "control": _load_control()})


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


async def llm_traces(request):
    if not _has_admin_auth(request):
        return _json_response({"error": "unauthorized"}, 401)
    traces = _safe_list(_load(_llm_traces_path()))
    limit = _int_env_safe("MAXWELL_TRACE_API_LIMIT", 200)
    try:
        q = int(request.query.get("limit", limit))
        limit = max(1, min(q, 1000))
    except Exception as e:
        # Non-numeric ?limit — fall back to the configured default.
        logger.debug("Ignoring bad ?limit on /llm_traces: %s", e)
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
    return _json_response(
        {
            "items": ordered[offset : offset + limit],
            "total": len(runs),
            "offset": offset,
            "limit": limit,
        }
    )


async def _queue_rem_command(cmd_type: str):
    # Use the same cross-process FileLock path as _queue_command.
    return await _queue_command(cmd_type)


async def _queue_command(cmd_type: str, extra: dict | None = None):
    """Generic command queue helper (same pattern as _queue_rem_command)."""

    # Use cross-process FileLock in addition to in-process _file_lock for
    # better protection against bot reader/writer races on bot_commands.json.
    def _do_append():
        # _load_commands_for_write raises ValueError on a corrupt file and the
        # caller turns that into a 500; no local handling needed.
        cmds = _load_commands_for_write()
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
        # Note: atomic_json_write inside lock; keep the write short.
        # The outer async with _file_lock is kept for API-internal serialization.
        _atomic_json_write_sync(_commands_path(), cmds)
        return cmd_id

    async with _file_lock:
        try:
            with FileLock(_commands_path(), timeout=5.0):
                cmd_id = await asyncio.to_thread(_do_append)
            return cmd_id, ""
        except ValueError as exc:
            return "", str(exc)
        except FileLockTimeout as exc:
            return "", str(exc)
        except Exception as e:
            return "", str(e)


async def rem_run(request):
    status = _load_rem_status()
    if status.get("running"):
        return _json_response(
            {"ok": True, "started": False, "reason": "already running"}
        )
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
        cmds.append(
            {
                "id": cmd_id,
                "type": cmd_type,
                "status": "pending",
                "result": "",
                "created_at": time.time(),
            }
        )
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



async def autonomy_status(request):
    if not _has_admin_auth(request):
        return _json_response({"error": "unauthorized"}, 401)
    control = _load_control()
    state = _load_autonomy_state()
    return _json_response(
        {
            "enabled": control.get("autonomy_enabled", False),
            "interval_seconds": control.get("autonomy_interval_seconds", 300),
            "model": control.get("autonomy_model", ""),
            "base_url": control.get("autonomy_base_url", ""),
            "disable_reasoning": control.get("autonomy_disable_reasoning", True),
            "recent_reply_block_seconds": control.get(
                "autonomy_recent_reply_block_seconds", 0
            ),
            "aux_model": control.get("aux_model", ""),
            "aux_base_url": control.get("aux_base_url", ""),
            "aux_disable_reasoning": control.get("aux_disable_reasoning", True),
            "last_tick": state.get("last_tick"),
            "last_tick_duration": state.get("last_tick_duration"),
            "actions_executed_total": state.get("actions_executed_total", 0),
            "actions_failed_total": state.get("actions_failed_total", 0),
            "last_error": state.get("last_error"),
            "last_thought": state.get("last_thought"),
        }
    )


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
        cmds.append(
            {
                "id": cmd_id,
                "type": cmd_type,
                "status": "pending",
                "result": "",
                "created_at": time.time(),
            }
        )
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
    if not isinstance(body, dict):
        return _json_response({"error": "body must be an object"}, 400)
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
        cmds.append(
            {
                "id": cmd_id,
                "type": "autonomy_interval",
                "status": "pending",
                "interval_seconds": new_interval,
                "result": "",
                "created_at": time.time(),
            }
        )
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
    if not isinstance(body, dict):
        return _json_response({"error": "body must be an object"}, 400)
    description = str(body.get("description", "")).strip()[:2000]
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
            return _json_response(
                {"error": "refusing to overwrite malformed autonomy_goals.json"}, 409
            )
        if len(goals) >= MAX_AUTONOMY_GOALS:
            return _json_response(
                {"error": f"goal limit reached ({MAX_AUTONOMY_GOALS})"}, 409
            )
        goals.append(goal)
        await atomic_json_write(
            _autonomy_goals_path(), {"goals": goals[-MAX_AUTONOMY_GOALS:]}
        )
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
            return _json_response(
                {"error": "refusing to overwrite malformed autonomy_goals.json"}, 409
            )
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


# ---------- Context cleanup agent (removed — RAG memory active) ----------
# The old ContextCleanupEngine (context_cleanup.py) has been replaced by the
# RAG vector memory system (rag_memory.py). These endpoints are kept as no-op
# stubs so the admin dashboard and any external callers don't 404; they all
# report that the engine has been removed.
_CC_REMOVED = "context cleanup engine removed (RAG memory active)"


async def context_cleanup_status(request):
    if not _has_admin_auth(request):
        return _json_response({"error": "unauthorized"}, 401)
    return _json_response({"enabled": False, "running": False, "removed": True})


async def context_cleanup_run(request):
    return _json_response({"ok": True, "message": _CC_REMOVED})


async def context_cleanup_enable(request):
    return _json_response({"ok": True, "message": _CC_REMOVED})


async def context_cleanup_disable(request):
    return _json_response({"ok": True, "message": _CC_REMOVED})


async def context_cleanup_interval(request):
    return _json_response({"ok": True, "message": _CC_REMOVED})


async def context_cleanup_log_clear(request):
    if not _has_admin_auth(request):
        return _json_response({"error": "unauthorized"}, 401)
    return _json_response({"ok": True, "message": _CC_REMOVED})


# ---------- Command queue ----------
async def commands_post(request):
    try:
        body = await request.json()
    except Exception:
        return _json_response({"error": "invalid json"}, 400)
    if not isinstance(body, dict):
        return _json_response({"error": "body must be an object"}, 400)
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
        return _json_response(
            {"error": "shell commands are not allowed via the web API"}, 403
        )
    elif cmd_type == "clear_memory":
        command["channel_id"] = str(body.get("channel_id", "")).strip()
    elif cmd_type == "reload_controls" or cmd_type in {
        "rem_run",
        "rem_enable",
        "rem_disable",
        "autonomy_run",
        "autonomy_enable",
        "autonomy_disable",
        "autonomy_interval",
        "context_cleanup_run",
        "context_cleanup_enable",
        "context_cleanup_disable",
        "context_cleanup_interval",
    }:
        pass
    elif cmd_type == "inbox_act":
        command["action"] = str(body.get("action", "")).strip().lower()
        command["item_id"] = str(body.get("item_id", "")).strip()
        command["user_id"] = str(body.get("user_id", "")).strip()
        if command["action"] not in {"accept", "decline", "dismiss"}:
            return _json_response(
                {"error": "action must be accept, decline, or dismiss"}, 400
            )
        if not command["item_id"] and not command["user_id"]:
            return _json_response({"error": "item_id or user_id required"}, 400)
    else:
        return _json_response({"error": f"unknown command type: {cmd_type}"}, 400)

    def _do_append():
        with FileLock(_commands_path(), timeout=5.0):
            cmds = _load_commands_for_write()
            cmds.append(command)
            if len(cmds) > MAX_COMMANDS:
                cmds = cmds[-MAX_COMMANDS:]
            _atomic_json_write_sync(_commands_path(), cmds)

    async with _file_lock:
        try:
            await asyncio.to_thread(_do_append)
        except ValueError as exc:
            return _json_response({"error": str(exc)}, 409)
        except FileLockTimeout as exc:
            return _json_response({"error": str(exc)}, 409)
    return _json_response({"ok": True, "id": cmd_id})


async def commands_get(request):
    if not _has_admin_auth(request):
        return _json_response({"error": "unauthorized"}, 401)
    cmds = _load_commands()
    return _json_response(cmds[-100:])


async def commands_del(request):
    cid = request.query.get("id", "")

    def _do_delete():
        with FileLock(_commands_path(), timeout=5.0):
            cmds = _load_commands_for_write()
            cmds = [c for c in cmds if c.get("id") != cid]
            _atomic_json_write_sync(_commands_path(), cmds)

    async with _file_lock:
        try:
            await asyncio.to_thread(_do_delete)
        except ValueError as exc:
            return _json_response({"error": str(exc)}, 409)
        except FileLockTimeout as exc:
            return _json_response({"error": str(exc)}, 409)
    return _json_response({"ok": True})


async def discord_state(request):
    if not _has_admin_auth(request):
        return _json_response({"error": "unauthorized"}, 401)
    state = _safe_object(_load(DATA_DIR / "discord_state.json"))
    return _json_response(state)


async def inbox_get(request):
    if not _has_admin_auth(request):
        return _json_response({"error": "unauthorized"}, 401)
    return _json_response({"items": _load_inbox()})


async def inbox_act(request):
    if not _has_admin_auth(request):
        return _json_response({"error": "unauthorized"}, 401)
    item_id = str(request.match_info.get("id", "")).strip()
    if not item_id:
        return _json_response({"error": "id required"}, 400)
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    class _Queued:
        async def json(self):
            return {
                "type": "inbox_act",
                "action": body.get("action", ""),
                "item_id": item_id,
                "user_id": body.get("user_id", ""),
            }

    # Command queue only — this process never calls Discord HTTP.
    return await commands_post(_Queued())


# ---------- PM2 / System ----------
_pm2_cache = None
_pm2_cache_time = 0.0


async def _pm2_json():
    global _pm2_cache, _pm2_cache_time
    now = time.time()
    if _pm2_cache is not None and (now - _pm2_cache_time) < 10.0:
        return _pm2_cache
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "pm2",
            "jlist",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        data = json.loads(stdout.decode("utf-8", errors="replace"))
        _pm2_cache = data if isinstance(data, list) else []
        _pm2_cache_time = now
        return _pm2_cache
    except asyncio.CancelledError:
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
        raise
    except asyncio.TimeoutError:
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
        return _pm2_cache if _pm2_cache is not None else []
    except Exception:
        return _pm2_cache if _pm2_cache is not None else []
    finally:
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()


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
        out.append(
            {
                "name": name,
                "pid": proc.get("pid"),
                "status": env.get("status"),
                "uptime": env.get("pm_uptime"),
                "restart_time": env.get("restart_time"),
                "cpu": mon.get("cpu"),
                "memory": mon.get("memory"),
            }
        )
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
            "pm2",
            "logs",
            process,
            "--lines",
            str(lines_int),
            "--nostream",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
            text = stdout.decode("utf-8", errors="replace")
        except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
            if isinstance(exc, asyncio.CancelledError):
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()
                raise
            proc.kill()
            await proc.wait()
            return _json_response({"error": "pm2 logs timed out"}, 500)
        finally:
            if proc.returncode is None:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception as e:
                    # Usually means the process already exited.
                    logger.debug("pm2 subprocess cleanup: %s", e)
        # Strip ANSI escape sequences for clean HTML display
        text = re.sub(r"\x1b\[[0-9;]*m", "", text)
        # Drop PM2 headers and log file labels
        lines_raw = text.splitlines()
        clean = []
        for ln in lines_raw:
            if ln.startswith("[TAILING]"):
                continue
            if " last " in ln and " lines:" in ln:
                continue
            if ln.startswith("/root/.pm2/logs/"):
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
        cmd = (
            ["pm2", "restart", target]
            if target != "all"
            else ["pm2", "restart", "maxwell-bot", "maxwell-api"]
        )
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            text = (stdout + stderr).decode("utf-8", errors="replace")
            return _json_response({"ok": True, "output": text})
        except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
            if isinstance(exc, asyncio.CancelledError):
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()
                raise
            proc.kill()
            await proc.wait()
            return _json_response({"ok": False, "error": "pm2 restart timed out"})
        finally:
            if proc.returncode is None:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception as e:
                    # Usually means the process already exited.
                    logger.debug("pm2 subprocess cleanup: %s", e)
    except Exception:
        return _json_response({"error": "internal error"}, 500)


async def channel_list(request):
    if not _has_admin_auth(request):
        return _json_response({"error": "unauthorized"}, 401)
    # Read channel message counts directly from the RAG SQLite DB.
    try:
        rows = _rag_query(
            "SELECT channel_id, COUNT(*) as c, MAX(timestamp) as last "
            "FROM vectors WHERE kind='message' GROUP BY channel_id"
        )
    except sqlite3.Error as e:
        return _json_response({"error": f"rag db: {e}"}, 500)
    out = [
        {"id": row["channel_id"], "messages": row["c"], "last": row["last"] or ""}
        for row in rows
    ]
    out.sort(key=lambda x: x["messages"], reverse=True)
    return _json_response(out)


async def chat_history(request):
    if not _has_admin_auth(request):
        return _json_response({"error": "unauthorized"}, 401)
    cid = request.query.get("channel_id", "")
    if not cid:
        return _json_response({"error": "channel_id required"}, 400)
    # Pull the last 100 messages for this channel from the RAG DB, oldest-first
    # to match the old memory.json shape.
    try:
        rows = _rag_query(
            "SELECT id, author, author_id, content, timestamp, metadata "
            "FROM vectors WHERE kind='message' AND channel_id=? "
            "ORDER BY created_at DESC LIMIT 100",
            (str(cid),),
        )
    except sqlite3.Error as e:
        return _json_response({"error": f"rag db: {e}"}, 500)
    msgs = []
    for row in reversed(rows):
        entry = {
            "author": row["author"],
            "author_id": row["author_id"],
            "content": row["content"],
            "message_id": row["id"],
            "timestamp": row["timestamp"],
        }
        try:
            meta = json.loads(row["metadata"] or "{}")
            if isinstance(meta, dict):
                entry.update(meta)
        except Exception as e:
            # Corrupt metadata JSON: serve the row without its extras.
            logger.debug("Bad metadata JSON on row %s: %s", row["id"], e)
        msgs.append(entry)
    return _json_response(msgs)


async def bot_status(request):
    if not _has_admin_auth(request):
        return _json_response({"error": "unauthorized"}, 401)
    control = _load_control()
    pm2 = await _pm2_json()
    bot_proc = next((p for p in pm2 if p.get("name") == "maxwell-bot"), None)
    api_proc = next((p for p in pm2 if p.get("name") == "maxwell-api"), None)
    # RAG memory stats — read directly from the SQLite vector DB. Fall back to
    # zeros if the DB is unavailable (e.g. first run before the bot creates it).
    try:
        chan_row = _rag_query_one(
            "SELECT COUNT(DISTINCT channel_id) as c FROM vectors WHERE kind='message'"
        )
        msg_row = _rag_query_one(
            "SELECT COUNT(*) as c FROM vectors WHERE kind='message'"
        )
        ctx_row = _rag_query_one(
            "SELECT COUNT(*) as c FROM vectors WHERE kind='shared_context'"
        )
        ltm_row = _rag_query_one("SELECT COUNT(*) as c FROM vectors WHERE kind='ltm'")
        total_row = _rag_query_one("SELECT COUNT(*) as c FROM vectors")
        emb_row = _rag_query_one(
            "SELECT COUNT(*) as c FROM vectors WHERE embedding IS NOT NULL"
        )
        rag_stats = {
            "channels": chan_row["c"] if chan_row else 0,
            "messages": msg_row["c"] if msg_row else 0,
            "context": ctx_row["c"] if ctx_row else 0,
            "ltm": ltm_row["c"] if ltm_row else 0,
            "total_vectors": total_row["c"] if total_row else 0,
            "embedded": emb_row["c"] if emb_row else 0,
            "pending_embeddings": (total_row["c"] if total_row else 0)
            - (emb_row["c"] if emb_row else 0),
            "embed_model": RAG_EMBED_MODEL,
        }
    except sqlite3.Error:
        rag_stats = {
            "channels": 0,
            "messages": 0,
            "context": 0,
            "ltm": 0,
            "total_vectors": 0,
            "embedded": 0,
            "pending_embeddings": 0,
            "embed_model": RAG_EMBED_MODEL,
        }
    return _json_response(
        {
            "online": bool(
                bot_proc and bot_proc.get("pm2_env", {}).get("status") == "online"
            ),
            "control": {
                k: control.get(k)
                for k in [
                    "bot_enabled",
                    "reply_dms",
                    "reply_groups",
                    "reply_mentions",
                    "tools_enabled",
                    "store_memory",
                    "cross_context_enabled",
                    "cross_context_extract_enabled",
                ]
            },
            "stats": rag_stats,
            "known_tools": list(KNOWN_TOOLS),
            "pm2": {
                "bot": {
                    "status": bot_proc.get("pm2_env", {}).get("status")
                    if bot_proc
                    else "unknown",
                    "uptime": bot_proc.get("pm2_env", {}).get("pm_uptime")
                    if bot_proc
                    else None,
                    "restart_time": bot_proc.get("pm2_env", {}).get("restart_time")
                    if bot_proc
                    else None,
                },
                "api": {
                    "status": api_proc.get("pm2_env", {}).get("status")
                    if api_proc
                    else "unknown",
                    "uptime": api_proc.get("pm2_env", {}).get("pm_uptime")
                    if api_proc
                    else None,
                },
            },
        }
    )


# ---------- Login ----------
async def login_post(request):
    """Validate dashboard credentials without persisting them."""
    try:
        body = await request.json()
    except Exception:
        return _json_response({"error": "invalid json"}, 400)
    if not isinstance(body, dict):
        return _json_response({"error": "body must be an object"}, 400)
    user = str(body.get("user", "")).strip()
    pwd = str(body.get("pass", "")).strip()
    if not user or not pwd:
        return _json_response({"error": "user and pass required"}, 400)
    admin_user, admin_pwd = _load_admin_creds()
    if not admin_user or not admin_pwd:
        return _json_response({"error": "admin auth not configured"}, 503)
    if not (_safe_compare(user, admin_user) and _safe_compare(pwd, admin_pwd)):
        _record_auth_failure(request)
        return _json_response({"error": "unauthorized"}, 401)
    return _json_response({"ok": True, "message": "credentials valid"})


# ---------- Discord OAuth login ----------
# The frontend hits /api/auth/discord/state to get a one-time state token and
# the authorize URL, then Discord redirects to /api/auth/discord/callback which
# exchanges the code and issues a bearer token the dashboard stores and sends
# as `X-Discord-Token` for subsequent API calls.
_DISCORD_STATES: dict[str, float] = {}


def _discord_redirect_base(request) -> str:
    # Prefer fixed public base so Host-header open redirects cannot steal tokens.
    fixed = (
        os.getenv("MAXWELL_PUBLIC_BASE_URL") or os.getenv("DISCORD_REDIRECT_BASE") or ""
    ).rstrip("/")
    if fixed:
        return fixed
    return f"{request.scheme}://{request.host}"


async def discord_auth_state(request):
    import secrets as _secrets

    # This endpoint is unauthenticated (OAuth entry point), so bound the
    # in-memory state table: drop expired states (TTL matches the callback's
    # 600s check) and cap the table so a spammer can't balloon memory.
    _DISCORD_STATE_TTL = 600
    _DISCORD_STATE_MAX = 200
    now = time.time()
    expired = [
        s for s, issued in _DISCORD_STATES.items() if now - issued > _DISCORD_STATE_TTL
    ]
    for s in expired:
        _DISCORD_STATES.pop(s, None)
    while len(_DISCORD_STATES) >= _DISCORD_STATE_MAX:
        # Evict an arbitrary (oldest-insertion) entry to cap table size.
        _DISCORD_STATES.pop(next(iter(_DISCORD_STATES)), None)

    state = _secrets.token_urlsafe(24)
    _DISCORD_STATES[state] = now
    redirect = (
        os.getenv("DISCORD_REDIRECT_URI")
        or f"{_discord_redirect_base(request)}/api/auth/discord/callback"
    )
    client_id = DISCORD_CLIENT_ID
    from urllib.parse import quote as _url_quote

    return _json_response(
        {
            "client_id": client_id,
            "redirect_uri": redirect,
            "state": state,
            "authorize_url": (
                "https://discord.com/api/oauth2/authorize"
                f"?client_id={_url_quote(client_id, safe='')}"
                "&response_type=code"
                f"&redirect_uri={_url_quote(redirect, safe='')}"
                "&scope=identify"
                f"&state={_url_quote(state, safe='')}"
            )
            if client_id
            else "",
            "enabled": bool(client_id and DISCORD_CLIENT_SECRET),
        }
    )


async def discord_auth_callback(request):
    code = request.query.get("code")
    state = request.query.get("state")
    if not code or not state:
        return _json_response({"error": "missing code/state"}, 400)
    issued = _DISCORD_STATES.pop(state, None)
    if not issued or time.time() - issued > 600:
        return _json_response({"error": "invalid or expired state"}, 400)
    if not DISCORD_CLIENT_ID or not DISCORD_CLIENT_SECRET:
        return _json_response({"error": "discord oauth not configured"}, 503)
    redirect = (
        os.getenv("DISCORD_REDIRECT_URI")
        or f"{_discord_redirect_base(request)}/api/auth/discord/callback"
    )
    import aiohttp as _aiohttp

    async with _aiohttp.ClientSession() as sess:
        token_resp = await sess.post(
            "https://discord.com/api/oauth2/token",
            data={
                "client_id": DISCORD_CLIENT_ID,
                "client_secret": DISCORD_CLIENT_SECRET,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect,
                "scope": "identify",
            },
            headers={"Accept": "application/json"},
        )
        if token_resp.status != 200:
            body = await token_resp.text()
            logger.warning("discord token exchange failed: %s", body[:300])
            return _json_response({"error": "discord token exchange failed"}, 502)
        token_json = await token_resp.json()
        access_token = token_json.get("access_token")
        if not access_token:
            return _json_response({"error": "no access token from discord"}, 502)
        me_resp = await sess.get(
            "https://discord.com/api/users/@me",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if me_resp.status != 200:
            return _json_response({"error": "failed to fetch discord user"}, 502)
        me = await me_resp.json()
    user_id = str(me.get("id", ""))
    username = str(me.get("username", "")) + "#" + str(me.get("discriminator", "0"))
    avatar = me.get("avatar")
    avatar_url = (
        f"https://cdn.discordapp.com/avatars/{user_id}/{avatar}.png" if avatar else ""
    )
    # Source of truth: the bot's live admins.json (updated by `,admin @user`).
    # Anyone in this list can use the bot's admin commands AND log into the
    # dashboard via Discord OAuth. No hardcoded env list to keep in sync.
    allowed = _load_bot_admins()
    if not allowed:
        logger.error(
            "discord oauth denied: admins.json missing/empty and "
            "DISCORD_ALLOWED_USER_IDS unset (fail closed)"
        )
        return _json_response(
            {"error": "discord oauth not configured (no allowed user ids)"}, 403
        )
    if user_id not in allowed:
        logger.warning("discord oauth denied for user %s (%s)", user_id, username)
        return _json_response({"error": "discord account not authorized"}, 403)
    import secrets as _secrets

    bearer = _secrets.token_urlsafe(48)
    _DISCORD_TOKENS[bearer] = {
        "user_id": user_id,
        "username": username,
        "avatar_url": avatar_url,
        "expires": time.time() + DISCORD_TOKEN_TTL,
    }
    base = _discord_redirect_base(request)
    # Redirect back to the admin page with the token in the hash fragment so
    # the SPA can pick it up without it hitting server logs as a query param.
    raise web.HTTPFound(f"{base}/admin/#discord_token={bearer}")


async def discord_auth_verify(request):
    # Token via query string removed: bearer tokens in URLs leak into access
    # logs, browser history, and Referer headers. The dashboard always sends
    # the X-Discord-Token header.
    token = request.headers.get("X-Discord-Token", "")
    info = _DISCORD_TOKENS.get(token)
    if not info or info.get("expires", 0) < time.time():
        return _json_response({"ok": False}, 401)
    return _json_response(
        {
            "ok": True,
            "user_id": info["user_id"],
            "username": info["username"],
            "avatar_url": info.get("avatar_url", ""),
        }
    )


async def discord_auth_logout(request):
    # See discord_auth_verify: no token-in-query-string fallback.
    token = request.headers.get("X-Discord-Token", "")
    _DISCORD_TOKENS.pop(token, None)
    return _json_response({"ok": True})


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
    except Exception as e:
        logger.debug("Could not read disk usage: %s", e)
        disk_total, disk_used = 0, 0
    uptime_seconds = 0
    try:
        uptime_text = Path("/proc/uptime").read_text(encoding="utf-8").strip()
        uptime_seconds = float(uptime_text.split()[0])
    except Exception as e:
        # Non-Linux or restricted /proc: the dashboard shows 0 uptime.
        logger.debug("Could not read /proc/uptime: %s", e)
    return _json_response(
        {
            "load": loadavg,
            "memory": {"total_mb": mem_total, "used_mb": mem_used},
            "disk": {"total_bytes": disk_total, "used_bytes": disk_used},
            "uptime_seconds": round(uptime_seconds),
        }
    )


# ---------- App ----------
async def _options_handler(request):
    """Shared CORS preflight handler for all OPTIONS routes."""
    return web.Response(
        status=204,
        headers={
            "Access-Control-Allow-Origin": CORS_ORIGIN,
            "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, Authorization, X-Discord-Token",
        },
    )


# ---------- Long-term memory (RAG SQLite vector DB) ----------
# The old file-based long_term_memory.txt is gone. LTM entries now live as rows
# in the `vectors` table with kind='ltm'. IDs are UUIDs (uuid.uuid4().hex), not
# positional integers. The bot's RAGMemoryManager owns embedding generation; the
# API server just inserts/updates/deletes rows and leaves embedding NULL (the
# bot will embed lazily on next search, or a background _embed_pending pass will
# pick it up).
_LTM_REMOVED_MSG = "context cleanup engine removed (RAG memory active)"


async def memory_add(request):
    try:
        body = await request.json()
    except Exception:
        return _json_response({"error": "invalid json"}, 400)
    if not isinstance(body, dict):
        return _json_response({"error": "body must be an object"}, 400)
    content = _normalize_memory_line(body.get("content", ""))
    if not content:
        return _json_response({"error": "empty"}, 400)
    new_id = _uuid.uuid4().hex
    ts = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    try:
        _rag_exec(
            "INSERT INTO vectors "
            "(id, kind, channel_id, guild_id, author, author_id, source, content, "
            "content_hash, embedding, metadata, scope, importance, parent_id, "
            "chunk_index, downvotes, timestamp, created_at) "
            "VALUES (?, 'ltm', '', '', '', '', 'user', ?, ?, NULL, '{}', 'global', "
            "5, '', 0, 0, ?, ?)",
            (new_id, content, content_hash, ts, time.time()),
        )
    except sqlite3.Error as e:
        return _json_response({"error": f"rag db: {e}"}, 500)
    return _json_response(
        {"ok": True, "id": new_id, "entry": {"id": new_id, "content": content}}
    )


async def memory_update(request):
    try:
        body = await request.json()
    except Exception:
        return _json_response({"error": "invalid json"}, 400)
    if not isinstance(body, dict):
        return _json_response({"error": "body must be an object"}, 400)
    target_id = str(body.get("id") or "").strip()
    if not target_id:
        return _json_response({"error": "id required"}, 400)
    content = _normalize_memory_line(body.get("content", ""))
    if not content:
        # An edit that empties the content is treated as a delete.
        try:
            cur = _rag_exec(
                "DELETE FROM vectors WHERE id=? AND kind='ltm'", (target_id,)
            )
        except sqlite3.Error as e:
            return _json_response({"error": f"rag db: {e}"}, 500)
        if cur.rowcount == 0:
            return _json_response({"error": "not found"}, 404)
        return _json_response({"ok": True})
    try:
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        cur = _rag_exec(
            "UPDATE vectors SET content=?, content_hash=?, embedding=NULL "
            "WHERE id=? AND kind='ltm'",
            (content, content_hash, target_id),
        )
    except sqlite3.Error as e:
        return _json_response({"error": f"rag db: {e}"}, 500)
    if cur.rowcount == 0:
        return _json_response({"error": "not found"}, 404)
    return _json_response({"ok": True})


async def memory_delete(request):
    raw_id = request.query.get("id", "")
    target_id = str(raw_id).strip()
    if not target_id:
        return _json_response({"error": "id required"}, 400)
    try:
        cur = _rag_exec(
            "DELETE FROM vectors WHERE id=? AND kind='ltm'", (target_id,)
        )
    except sqlite3.Error as e:
        return _json_response({"error": f"rag db: {e}"}, 500)
    if cur.rowcount == 0:
        return _json_response({"error": "not found"}, 404)
    return _json_response({"ok": True})


app = web.Application(
    middlewares=[_auth_middleware_unless_login], client_max_size=256 * 1024
)
app.router.add_get("/data/{file}", data_file)
app.router.add_options(
    "/data/{file}",
    _options_handler,
)
app.router.add_get("/api/rag/memory", rag_memory_stats)
app.router.add_get("/api/rag/ltm", rag_ltm_list)
app.router.add_get("/api/rag/entities", rag_entities_list)
app.router.add_post("/api/memory", memory_add)
app.router.add_put("/api/memory", memory_update)
app.router.add_delete("/api/memory", memory_delete)
app.router.add_options(
    "/api/memory",
    _options_handler,
)
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
# Public, unauthenticated (see _needs_auth): the generated site's own backend.
app.router.add_get("/api/site/{slug}/kv", site_kv_get)
app.router.add_put("/api/site/{slug}/kv", site_kv_put)
app.router.add_post("/api/site/{slug}/kv/bump", site_kv_bump)
app.router.add_delete("/api/site/{slug}/kv", site_kv_delete)
app.router.add_get("/api/site/{slug}/items/{name}", site_items_get)
app.router.add_post("/api/site/{slug}/items/{name}", site_items_post)
app.router.add_delete("/api/site/{slug}/items/{name}", site_items_delete)
app.router.add_options("/api/site/{slug}/kv", _options_handler)
app.router.add_options("/api/site/{slug}/kv/bump", _options_handler)
app.router.add_options("/api/site/{slug}/items/{name}", _options_handler)
# Public, unauthenticated: a site's own backend server, reached through Caddy
# at /bot/<slug>/api/*. Every method, because the site defines its own routes.
# It lives in a sub-app purely so file uploads get a 32MB ceiling without
# raising the 256KB limit that protects every admin route. The parent app's
# auth middleware still runs (see api.auth._needs_auth, which exempts exactly
# this path shape).
site_app = web.Application(client_max_size=SITE_UPLOAD_MAX)
site_app.router.add_route("*", "/{slug}/api", site_proxy)
site_app.router.add_route("*", "/{slug}/api/{path:.*}", site_proxy)
app.add_subapp("/bot/", site_app)
app.router.add_get("/api/control", control_get)
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
app.router.add_get("/api/context_cleanup/status", context_cleanup_status)
app.router.add_post("/api/context_cleanup/run", context_cleanup_run)
app.router.add_post("/api/context_cleanup/enable", context_cleanup_enable)
app.router.add_post("/api/context_cleanup/disable", context_cleanup_disable)
app.router.add_put("/api/context_cleanup/interval", context_cleanup_interval)
app.router.add_delete("/api/context_cleanup/log", context_cleanup_log_clear)
app.router.add_get("/api/commands", commands_get)
app.router.add_post("/api/commands", commands_post)
app.router.add_delete("/api/commands", commands_del)
app.router.add_get("/api/discord/state", discord_state)
app.router.add_get("/api/inbox", inbox_get)
app.router.add_post("/api/inbox/{id}/act", inbox_act)
app.router.add_post("/api/login", login_post)
app.router.add_get("/api/auth/discord/state", discord_auth_state)
app.router.add_get("/api/auth/discord/callback", discord_auth_callback)
app.router.add_get("/api/auth/discord/verify", discord_auth_verify)
app.router.add_post("/api/auth/discord/logout", discord_auth_logout)
app.router.add_get("/api/pm2", pm2_status)
app.router.add_get("/api/pm2/logs", pm2_logs)
app.router.add_post("/api/pm2/restart", pm2_restart)
app.router.add_get("/api/channels", channel_list)
app.router.add_get("/api/chat/history", chat_history)
async def plugins_get(request):
    # This is a read-only endpoint. Resolve DATA_DIR at request time so tests
    # and deployments that override the data directory see the same state as
    # the rest of the API, and treat a missing/corrupt file as empty rather
    # than raising a NameError or returning a malformed payload.
    state = _load(DATA_DIR / "plugins.json")
    if not isinstance(state, dict):
        state = {}
    plugins = state.get("plugins")
    if not isinstance(plugins, dict):
        plugins = {}
    return _json_response({"ok": True, "plugins": plugins})


app.router.add_get("/api/plugins", plugins_get)
app.router.add_get("/api/status", bot_status)
app.router.add_get("/api/system", system_stats)
app.router.add_options(
    "/api/{path:.*}",
    _options_handler,
)

if __name__ == "__main__":
    web.run_app(app, host=API_HOST, port=API_PORT, access_log=None)
