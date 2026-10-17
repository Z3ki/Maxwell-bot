"""Deletion and retention orchestration. Failures are reported per store, never hidden."""

from __future__ import annotations

import asyncio
from contextlib import closing
import json
import logging
from pathlib import Path
import sqlite3
import time

from conversation_memory import delete_author_content_file
from privacy import retention_days
from utils import FileLock, _atomic_json_write_sync

logger = logging.getLogger(__name__)


def _clean_json(path, user_id, owner_keys, *, nested=None):
    if not path.is_file():
        return 0
    with FileLock(path, timeout=5):
        raw = json.loads(path.read_text("utf-8"))
        rows = raw[nested] if nested else raw

        def owned(row):
            return isinstance(row, dict) and any(
                str(row.get(key) or "") == user_id for key in owner_keys
            )

        if isinstance(rows, list):
            kept = [row for row in rows if not owned(row)]
        elif isinstance(rows, dict):
            kept = {
                key: row
                for key, row in rows.items()
                if key != user_id
                and not key.startswith(user_id + ":")
                and not owned(row)
            }
        else:
            raise TypeError("Unexpected store format")
        if nested:
            raw[nested] = kept
        else:
            raw = kept
        _atomic_json_write_sync(path, raw)
        return len(rows) - len(kept)


def delete_data(root, user_id, preferences, *, sessions=None):
    """Persist suppression first; other failures never undo it. Safe to retry.

    Running workspaces and externally published content need lifecycle-aware operator
    cleanup. Return that limitation explicitly instead of recursively deleting files
    underneath containers or claiming provider/backups were erased.
    """
    uid = str(user_id)
    if not uid.isdigit():
        raise ValueError("Invalid user")
    root = Path(root)
    extra = root / "plugins" / "maxwell_extras"
    preferences.delete_personal(
        uid
    )  # abort before deletion if suppression cannot persist
    results, failed = {}, []

    def run(name, callback):
        try:
            results[name] = callback()
        except Exception as exc:
            failed.append(name)
            logger.warning(
                "Privacy deletion failed: store=%s type=%s", name, type(exc).__name__
            )

    run("consents", lambda: preferences.consents.delete(uid))
    agent_path = extra / "task_agents.sqlite3"
    if agent_path.is_file():
        from plugins.maxwell_extras.task_agents import TaskAgentStore

        run("custom_agents", lambda: TaskAgentStore(agent_path).delete(uid))
    job_path = extra / "task_jobs.sqlite3"
    if job_path.is_file():
        from plugins.maxwell_extras.task_job_store import TaskJobStore
        run("worker_jobs", lambda: TaskJobStore(job_path).delete(uid))
    quota_path = root / "message_quota.sqlite3"
    if quota_path.is_file():

        def remove_plan():
            with closing(sqlite3.connect(quota_path, timeout=10)) as db, db:
                return db.execute(
                    "DELETE FROM overrides WHERE user_id=?", (uid,)
                ).rowcount

        run("plan_grants", remove_plan)
    run(
        "conversation", lambda: delete_author_content_file(root / "maxwell_rag.db", uid)
    )
    from plugins.maxwell_extras.byok import CredentialVault

    if (extra / "byok.sqlite3").is_file():
        run("byok", lambda: CredentialVault(extra / "byok.sqlite3").delete(uid))
    if (extra / "worker_byok.sqlite3").is_file():
        run("worker_byok", lambda: CredentialVault(extra / "worker_byok.sqlite3").delete(uid))
    for name, path, keys, nested in (
        ("traces", root / "llm_traces.json", ("user_id",), None),
        ("reminders", extra / "reminders.json", ("owner_id",), None),
        (
            "buttons",
            extra / "rich_interactions.json",
            ("user_id", "owner_id", "author_id"),
            "actions",
        ),
        ("github_tokens", root / "plugins/github_projects/user_tokens.json", (), None),
        ("github_policy", root / "plugins/github_projects/policies.json", (), None),
        (
            "github_knowledge",
            root / "plugins/github_projects/repo_knowledge.json",
            (),
            None,
        ),
    ):
        run(name, lambda p=path, k=keys, n=nested: _clean_json(p, uid, k, nested=n))
    if sessions is None and (root / "dashboard/sessions.sqlite3").is_file():

        def delete_dashboard_sessions():
            from api.dashboard_auth import DashboardConfig, DashboardSessions

            cfg = DashboardConfig.from_env()
            if cfg is None:
                raise RuntimeError("Dashboard key unavailable")
            DashboardSessions(root / "dashboard/sessions.sqlite3", cfg.key).delete_user(
                uid
            )

        run("dashboard_sessions", delete_dashboard_sessions)
    elif sessions is not None:
        run("dashboard_sessions", lambda: sessions.delete_user(uid))
    manual = [
        "Operator review: published sites/files, shell and GitHub workspaces, inbox/thread records and optional plugin stores; stop active jobs before erasing them.",
        "Discord posts, previous provider copies and operator backups are outside this deletion. Contact the operator for these copies.",
    ]
    return {
        "deleted": results,
        "failed": failed,
        "manual_required": manual,
        "complete": not failed and not manual,
        "storage": "off",
        "retained": "Storage suppression, short-lived usage/security metadata, and any billing records required by law.",
    }


async def delete_live_data(bot, user_id):
    uid = str(user_id)
    preferences = getattr(bot, "_user_preferences", None)
    if preferences is None:
        raise RuntimeError("Privacy settings unavailable")
    # This must finish before cancelling writers. Off applies across API processes.
    await asyncio.to_thread(preferences.set_default, uid, "tracking", "off")
    manager = getattr(bot, "_task_jobs", None)
    if manager is not None:
        await manager.cancel(uid, reason="stopped")
        for jid, job in list(manager.jobs.items()):
            if job.row["user_id"] == uid:
                manager.jobs.pop(jid, None)
    owners = getattr(bot, "_active_request_user", {}) or {}
    tasks = getattr(bot, "_active_requests", {}) or {}
    current = asyncio.current_task()
    pending = []
    for channel, owner in list(owners.items()):
        task = tasks.get(channel)
        if str(owner) == uid and task and task is not current and not task.done():
            task.cancel()
            pending.append(task)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    # These caches may contain quoted content. Clearing is deliberately conservative.
    for name in (
        "_media_context",
        "_message_snapshots",
        "_message_update_state",
        "_recent_users",
    ):
        cache = getattr(bot, name, None)
        if isinstance(cache, dict):
            cache.clear()
    service = getattr(getattr(bot, "plugin_manager", None), "services", None)
    lastfm_failed = False
    if service:
        try:
            accounts = service.get("lastfm", None)
            if accounts:
                await accounts.disconnect(uid)
        except Exception:
            lastfm_failed = True
    result = await asyncio.to_thread(delete_data, bot.config.DATA_DIR, uid, preferences)
    if lastfm_failed:
        result["failed"].append("lastfm")
    return result


def deletion_text(result):
    done = ", ".join(result["deleted"]) or "none"
    text = f"Storage is off. Active stores processed: {done}."
    if result["failed"]:
        text += (
            " Failed: "
            + ", ".join(result["failed"])
            + ". Retry Delete my data or contact the operator."
        )
    if result["manual_required"]:
        text += "\n" + "\n".join(result["manual_required"])
    return text + "\n" + result["retained"]


def cleanup_data(root, preferences):
    """Run at startup and every ten minutes. No raw diagnostic content is needed."""
    root = Path(root)
    now = time.time()
    job_path = root / "plugins/maxwell_extras/task_jobs.sqlite3"
    if job_path.is_file():
        from plugins.maxwell_extras.task_job_store import TaskJobStore
        TaskJobStore(job_path).prune()
    agent_path = root / "plugins/maxwell_extras/task_agents.sqlite3"
    if agent_path.is_file():
        from plugins.maxwell_extras.task_agents import TaskAgentStore

        TaskAgentStore(agent_path).prune()
    db_path = root / "maxwell_rag.db"
    if db_path.is_file():
        with closing(sqlite3.connect(db_path, timeout=30)) as db:
            db.execute("PRAGMA secure_delete=ON")
            users = [
                str(row[0])
                for row in db.execute(
                    "SELECT DISTINCT author_id FROM vectors WHERE author_id!='' AND source!='bot' UNION SELECT json_extract(metadata,'$.requested_by') FROM vectors WHERE json_valid(metadata) AND json_extract(metadata,'$.requested_by') IS NOT NULL UNION SELECT json_extract(metadata,'$.reply_to_author_id') FROM vectors WHERE json_valid(metadata) AND json_extract(metadata,'$.reply_to_author_id') IS NOT NULL"
                )
            ]
            blocked = [
                uid
                for uid in users
                if uid.isdigit() and not preferences.content_allowed(uid)
            ]
            with db:
                db.execute(
                    "CREATE TEMP TABLE privacy_blocked (user_id TEXT PRIMARY KEY)"
                )
                db.executemany(
                    "INSERT INTO privacy_blocked VALUES (?)",
                    ((uid,) for uid in blocked),
                )
                db.execute(
                    "DELETE FROM vectors WHERE author_id IN (SELECT user_id FROM privacy_blocked) OR (json_valid(metadata) AND (json_extract(metadata,'$.requested_by') IN (SELECT user_id FROM privacy_blocked) OR (kind='bot_output' AND json_extract(metadata,'$.reply_to_author_id') IN (SELECT user_id FROM privacy_blocked))))"
                )
                db.execute(
                    "UPDATE vectors SET metadata=json_remove(metadata,'$.reply_to_content') WHERE json_valid(metadata) AND json_extract(metadata,'$.reply_to_author_id') IN (SELECT user_id FROM privacy_blocked)"
                )
                db.execute(
                    "DELETE FROM vectors WHERE created_at<?",
                    (
                        now
                        - retention_days("MAXWELL_HISTORY_RETENTION_DAYS", 30) * 86400,
                    ),
                )
                db.execute(
                    "DELETE FROM vectors WHERE kind NOT IN ('message','bot_output')"
                )
                db.execute(
                    "DELETE FROM vectors WHERE kind='bot_output' AND json_valid(metadata) AND COALESCE(json_extract(metadata,'$.requested_by'),json_extract(metadata,'$.reply_to_author_id'),'')=''"
                )
                if db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='user_entities'"
                ).fetchone():
                    db.execute("DELETE FROM user_entities")
                db.execute(
                    "DELETE FROM vectors WHERE json_valid(metadata) AND (json_extract(metadata,'$.is_tool') OR json_extract(metadata,'$.author_is_bot')) AND COALESCE(json_extract(metadata,'$.requested_by'),json_extract(metadata,'$.reply_to_author_id'),'')=''"
                )
    preferences.consents.cleanup()
    # Trace logs are metadata only going forward. Scrub old payloads on migration.
    path = root / "llm_traces.json"
    if path.is_file():
        with FileLock(path, timeout=5):
            rows = json.loads(path.read_text("utf-8"))
            if not isinstance(rows, list):
                raise TypeError("Unexpected trace format")
            # Legacy trace payloads may embed raw model reasoning: remove them entirely.
            rows = [
                row
                for row in rows
                if row.get("privacy_metadata_only")
                and float(row.get("recorded_at") or 0)
                > now - retention_days("MAXWELL_LOG_RETENTION_DAYS", 7) * 86400
            ]
            _atomic_json_write_sync(path, rows[-300:])
    for filename, table, column in (
        ("inbound_requests.sqlite3", "requests", "updated_at"),
        ("message_quota.sqlite3", "messages", "charged_at"),
    ):
        path = root / filename
        if path.is_file():
            with closing(sqlite3.connect(path, timeout=5)) as db:
                with db:
                    db.execute(
                        f"DELETE FROM {table} WHERE {column}<?",
                        (
                            now
                            - retention_days("MAXWELL_USAGE_RETENTION_DAYS", 7) * 86400,
                        ),
                    )

    session_path = root / "dashboard/sessions.sqlite3"
    if session_path.is_file():
        with closing(sqlite3.connect(session_path, timeout=5)) as db:
            with db:
                db.execute("DELETE FROM sessions WHERE expires<=?", (now,))
                db.execute("DELETE FROM flows WHERE expires<=?", (now,))
    callback_path = root / "plugins/maxwell_extras/rich_interactions.json"
    if callback_path.is_file():
        with FileLock(callback_path, timeout=5):
            data = json.loads(callback_path.read_text("utf-8"))
            from plugins.maxwell_extras.rich_interactions import _ACTION_TTL_SECONDS

            data["actions"] = {
                key: row
                for key, row in data["actions"].items()
                if row.get("owner_id")
                and float(row.get("created_at") or 0) > now - _ACTION_TTL_SECONDS
            }
            _atomic_json_write_sync(callback_path, data)
