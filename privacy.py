"""Trusted policy references and minimal, versioned optional consent records.

Notice delivery is not agreement. Ordinary requests never require blanket consent.
No message bodies, credentials, names, IP addresses or voice samples belong here.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
import contextvars
import hashlib
import logging
import os
from pathlib import Path
import sqlite3
import time
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)
STORAGE_VERSION = "optional-history-2026-10-17"
VOICE_VERSION = "voice-authorization-2026-10-17"


def policy_urls(config=None):
    def value(name):
        return str(getattr(config, name, os.getenv(name, "")) or "").strip()

    base = value("MAXWELL_PUBLIC_BASE_URL").rstrip("/")
    result = {}
    for name, suffix in (
        ("terms", "terms/"),
        ("privacy", "privacy/"),
        ("contact", "contact/"),
    ):
        explicit = value(f"MAXWELL_{name.upper()}_URL")
        url = explicit or (f"{base}/{suffix}" if base else "")
        parsed = urlsplit(url)
        if (
            parsed.scheme == "https"
            and parsed.hostname
            and not parsed.username
            and not parsed.password
        ):
            result[name] = url
    return result


def retention_days(name, default):
    try:
        return max(1, min(int(os.getenv(name, str(default))), 3650))
    except (TypeError, ValueError):
        return default


def voice_policy(reference):
    return "voice:" + hashlib.sha256(str(reference).strip().encode()).hexdigest()


class ConsentStore:
    """SQLite transactions are shared across Discord and dashboard processes."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS consents (user_id TEXT NOT NULL, policy TEXT NOT NULL, version TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL, recorded_at REAL NOT NULL, withdrawn_at REAL, PRIMARY KEY(user_id,policy,version,kind))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS notices (user_id TEXT NOT NULL, version TEXT NOT NULL, delivered_at REAL NOT NULL, PRIMARY KEY(user_id,version))"
            )
        os.chmod(self.path, 0o600)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def record(self, user_id, policy, version, kind, *, affirmative=False):
        if not affirmative or kind not in {
            "optional_storage",
            "voice_authorization",
            "agreement",
        }:
            raise ValueError(
                "An affirmative user action and specific consent type are required"
            )
        if not str(user_id).isdigit() or not policy or not version:
            raise ValueError("Invalid consent identity")
        with self.db() as db:
            db.execute(
                "INSERT INTO consents VALUES (?,?,?,?,?,?,NULL) ON CONFLICT(user_id,policy,version,kind) DO UPDATE SET status='granted', recorded_at=excluded.recorded_at, withdrawn_at=NULL WHERE consents.status!='granted'",
                (str(user_id), policy, version, kind, "granted", time.time()),
            )

    def granted(self, user_id, policy, version, kind):
        with self.db() as db:
            return (
                db.execute(
                    "SELECT 1 FROM consents WHERE user_id=? AND policy=? AND version=? AND kind=? AND status='granted'",
                    (str(user_id), policy, version, kind),
                ).fetchone()
                is not None
            )

    def withdraw(self, user_id, policy=None):
        with self.db() as db:
            sql = "UPDATE consents SET status='withdrawn',withdrawn_at=? WHERE user_id=? AND status='granted'"
            args = [time.time(), str(user_id)]
            if policy is not None:
                sql += " AND policy=?"
                args.append(policy)
            return db.execute(sql, args).rowcount

    def review(self, user_id):
        with self.db() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM consents WHERE user_id=? ORDER BY recorded_at DESC",
                    (str(user_id),),
                )
            ]

    def delete(self, user_id):
        with self.db() as db:
            db.execute("DELETE FROM consents WHERE user_id=?", (str(user_id),))
            db.execute("DELETE FROM notices WHERE user_id=?", (str(user_id),))

    def claim_notice(self, user_id, version):
        with self.db() as db:
            return (
                db.execute(
                    "INSERT OR IGNORE INTO notices VALUES (?,?,?)",
                    (str(user_id), version, time.time()),
                ).rowcount
                == 1
            )

    def retry_notice(self, user_id, version):
        with self.db() as db:
            db.execute(
                "DELETE FROM notices WHERE user_id=? AND version=?",
                (str(user_id), version),
            )

    def cleanup(self):
        cutoff = (
            time.time() - retention_days("MAXWELL_CONSENT_RETENTION_DAYS", 365) * 86400
        )
        with self.db() as db:
            db.execute(
                "DELETE FROM consents WHERE status='withdrawn' AND withdrawn_at<?",
                (cutoff,),
            )
            # Keep the current notice receipt while the instance operates so unchanged
            # policies never nag a returning user. This contains only id/version/time.


def consent_store(root):
    return ConsentStore(Path(root) / "plugins" / "maxwell_extras" / "privacy.sqlite3")


_ephemeral_notice: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "maxwell_ephemeral_notice", default=False
)


def notice_text(bot, user_id) -> str | None:
    """Disclosure copy when this user has not been shown the current version."""
    store = getattr(bot, "_user_preferences", None)
    urls = policy_urls(getattr(bot, "config", None))
    uid = str(user_id or "")
    if store is None or not uid.isdigit() or not {"terms", "privacy"} <= urls.keys():
        return None
    version = os.getenv("MAXWELL_NOTICE_VERSION", "processing-notice-2").strip()
    if not store.consents.claim_notice(uid, version):
        return None
    return (
        "Maxwell sends your request and necessary context to the selected AI provider. "
        "Saved messages are on until you turn them off in `/config → Privacy`. "
        "Turning storage off does not stop provider processing. "
        f"[Terms]({urls['terms']}) · [Privacy]({urls['privacy']})"
    )


def release_notice(bot, user_id) -> None:
    store = getattr(bot, "_user_preferences", None)
    uid = str(user_id or "")
    if store is None or not uid.isdigit():
        return
    version = os.getenv("MAXWELL_NOTICE_VERSION", "processing-notice-2").strip()
    store.consents.retry_notice(uid, version)


async def maybe_notice(bot, interaction):
    """One private disclosure that stays put.

    An ephemeral follow-up is dismissed by Discord when the Ask Maxwell reply
    updates the deferred response. A private answer keeps the notice in that
    reply. A public answer is sent by DM so it is not painted into someone
    else's channel and then removed.
    """
    user = getattr(interaction, "user", None)
    uid = getattr(user, "id", "")
    try:
        text = await asyncio.to_thread(notice_text, bot, uid)
    except Exception:
        return None
    if not text:
        return None
    ephemeral = _ephemeral_notice.get()
    try:
        if ephemeral:
            return text
        send = getattr(user, "send", None)
        if not callable(send):
            raise TypeError("no dm")
        await send(text)
    except Exception as exc:
        logger.warning("Private policy notice unavailable (%s)", type(exc).__name__)
        try:
            await asyncio.to_thread(release_notice, bot, uid)
        except Exception:
            pass
        return None
    return text


def request_started_at():
    """Carry the originating turn's time through awaited tool work."""
    import sys

    common = sys.modules.get("bot_common")
    context = getattr(common, "_current_inbound", None)
    message = context.get() if context is not None else None
    created = getattr(message, "created_at", None)
    return created.timestamp() if created is not None else time.time()


def owned_write_allowed(root, user_id, started_at):
    """Feature records are separate from history, but cannot undo full deletion."""
    import json

    path = Path(root) / "plugins/maxwell_extras/user_preferences.json"
    try:
        data = json.loads(path.read_text("utf-8"))
        row = data["users"].get(str(user_id), {})
        return float(started_at) > float(row.get("deleted_at") or 0)
    except FileNotFoundError:
        return True
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return False
