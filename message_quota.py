"""Customer-facing message quotas.

Usage is counted in messages over a rolling window, not tokens. Token and
API-cost accounting stay in ``daily_tokens`` for internal spending protection.

Plus allowances are not applied. Premium is not launched, and this module
must not grant a paid tier, start checkout, or transfer a server subscription.
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

FREE_MESSAGE_LIMIT = 300
FREE_WINDOW_SECONDS = 5 * 60 * 60


class MessageQuotaExceeded(Exception):
    def __init__(self, used: int, limit: int, window_seconds: int):
        self.used = int(used)
        self.limit = int(limit)
        self.window_seconds = int(window_seconds)
        super().__init__(
            f"Message limit reached ({self.used}/{self.limit} used in the last "
            f"{format_window(self.window_seconds)}). Older messages leave the "
            "window as they age out."
        )


def format_window(seconds: int) -> str:
    seconds = max(1, int(seconds))
    if seconds % 3600 == 0:
        hours = seconds // 3600
        return "1 hour" if hours == 1 else f"{hours} hours"
    if seconds % 60 == 0:
        minutes = seconds // 60
        return "1 minute" if minutes == 1 else f"{minutes} minutes"
    return f"{seconds} seconds"


def enforced_message_limit(control: dict | None) -> int:
    """Free allowance only. Plus numbers are stored elsewhere and ignored."""
    raw = (control or {}).get("message_quota_limit", FREE_MESSAGE_LIMIT)
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return FREE_MESSAGE_LIMIT


def enforced_window_seconds(control: dict | None) -> int:
    raw = (control or {}).get("message_quota_window_seconds", FREE_WINDOW_SECONDS)
    try:
        return max(60, int(raw))
    except (TypeError, ValueError):
        return FREE_WINDOW_SECONDS


class MessageQuota:
    def __init__(self, path: str | Path, *, clock=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock or time.time
        with self._db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS messages ("
                "id TEXT PRIMARY KEY, user_id TEXT NOT NULL, guild_id TEXT NOT NULL DEFAULT '', "
                "charged_at REAL NOT NULL)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS messages_user_time ON messages(user_id, charged_at)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS overrides ("
                "user_id TEXT PRIMARY KEY, message_limit INTEGER, exempt INTEGER NOT NULL DEFAULT 0)"
            )

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.execute("PRAGMA busy_timeout=30000")
        try:
            with db:
                yield db
        finally:
            db.close()

    def _now(self) -> float:
        return float(self._clock())

    def status(self, user_id: str, default_limit: int, window_seconds: int) -> dict:
        user_id = str(user_id)
        window_seconds = max(1, int(window_seconds))
        now = self._now()
        cutoff = now - window_seconds
        with self._db() as db:
            db.execute("DELETE FROM messages WHERE user_id=? AND charged_at<?", (user_id, cutoff))
            used = db.execute(
                "SELECT COUNT(*) FROM messages WHERE user_id=? AND charged_at>=?",
                (user_id, cutoff),
            ).fetchone()[0]
            oldest = db.execute(
                "SELECT MIN(charged_at) FROM messages WHERE user_id=? AND charged_at>=?",
                (user_id, cutoff),
            ).fetchone()[0]
            override = db.execute(
                "SELECT message_limit, exempt FROM overrides WHERE user_id=?",
                (user_id,),
            ).fetchone() or (None, 0)
        limit = override[0] if override[0] is not None else int(default_limit)
        resets_in = 0
        if oldest is not None:
            resets_in = max(0, int(oldest + window_seconds - now))
        return {
            "user_id": user_id,
            "used": int(used),
            "limit": int(limit),
            "window_seconds": window_seconds,
            "override": override[0],
            "exempt": bool(override[1]),
            "resets_in": resets_in,
        }

    def charge(
        self,
        user_id: str,
        default_limit: int,
        window_seconds: int,
        *,
        guild_id: str = "",
    ) -> str | None:
        """Record one customer message. None when the user is exempt."""
        user_id = str(user_id)
        window_seconds = max(1, int(window_seconds))
        now = self._now()
        cutoff = now - window_seconds
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM messages WHERE user_id=? AND charged_at<?", (user_id, cutoff))
            override = db.execute(
                "SELECT message_limit, exempt FROM overrides WHERE user_id=?",
                (user_id,),
            ).fetchone() or (None, 0)
            if override[1]:
                return None
            limit = override[0] if override[0] is not None else int(default_limit)
            used = db.execute(
                "SELECT COUNT(*) FROM messages WHERE user_id=? AND charged_at>=?",
                (user_id, cutoff),
            ).fetchone()[0]
            if int(used) >= int(limit):
                raise MessageQuotaExceeded(int(used), int(limit), window_seconds)
            message_id = uuid.uuid4().hex
            db.execute(
                "INSERT INTO messages(id, user_id, guild_id, charged_at) VALUES(?,?,?,?)",
                (message_id, user_id, str(guild_id or ""), now),
            )
            return message_id

    def configure(
        self,
        user_id: str,
        *,
        limit: int | None = None,
        exempt: bool | None = None,
        clear: bool = False,
        reset: bool = False,
    ) -> None:
        user_id = str(user_id)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            if clear:
                db.execute("DELETE FROM overrides WHERE user_id=?", (user_id,))
            elif limit is not None or exempt is not None:
                row = db.execute(
                    "SELECT message_limit, exempt FROM overrides WHERE user_id=?",
                    (user_id,),
                ).fetchone() or (None, 0)
                new_limit = row[0] if limit is None else max(1, int(limit))
                new_exempt = row[1] if exempt is None else int(bool(exempt))
                db.execute(
                    "INSERT INTO overrides(user_id, message_limit, exempt) VALUES(?,?,?) "
                    "ON CONFLICT(user_id) DO UPDATE SET "
                    "message_limit=excluded.message_limit, exempt=excluded.exempt",
                    (user_id, new_limit, new_exempt),
                )
            if reset:
                db.execute("DELETE FROM messages WHERE user_id=?", (user_id,))
