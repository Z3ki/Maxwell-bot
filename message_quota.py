"""Customer-facing message quotas.

Usage is counted in messages over a rolling window. That is the only usage
measure: there is no token, spend, or plan-based quota.
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

FREE_MESSAGE_LIMIT = 150
PREMIUM_MESSAGE_LIMIT = 500
PREMIUM_PLAN = "premium"
FREE_WINDOW_SECONDS = 60 * 60


def resolved_message_limit(default_limit: int, override_limit, plan: str) -> int:
    """Custom overrides win. Premium otherwise uses the higher allowance."""
    if override_limit is not None:
        return max(1, int(override_limit))
    if plan == PREMIUM_PLAN:
        # Never grant premium *less* than the operator's configured free
        # default; a default above 500 used to cap premium users below free.
        return max(PREMIUM_MESSAGE_LIMIT, max(1, int(default_limit)))
    return max(1, int(default_limit))


class MessageQuotaExceeded(Exception):
    def __init__(self, used: int, limit: int, window_seconds: int):
        self.used = int(used)
        self.limit = int(limit)
        self.window_seconds = int(window_seconds)
        super().__init__(
            "Current message allowance reached. Older usage leaves the rolling "
            f"{format_window(self.window_seconds)} window as it ages out."
        )


def usage_percent(used: int, limit: int) -> int:
    """Rounded share of the allowance. Users only ever see this, never the
    raw count or limit: the limit gets retuned and people remember numbers."""
    return int(max(0, int(used)) * 100 / max(1, int(limit)) + 0.5)


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
    """The configured per-user message allowance."""
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
                "user_id TEXT PRIMARY KEY, message_limit INTEGER, exempt INTEGER NOT NULL DEFAULT 0, "
                "plan TEXT NOT NULL DEFAULT '')"
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(overrides)")}
            if "plan" not in columns:
                db.execute(
                    "ALTER TABLE overrides ADD COLUMN plan TEXT NOT NULL DEFAULT ''"
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
                "SELECT message_limit, exempt, plan FROM overrides WHERE user_id=?",
                (user_id,),
            ).fetchone() or (None, 0, "")
        plan = override[2] if override[2] == PREMIUM_PLAN else "free"
        limit = resolved_message_limit(default_limit, override[0], plan)
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
            "plan": plan,
            "resets_in": resets_in,
        }

    def plan_for(self, user_id: str) -> str:
        """Return ``premium`` or ``free`` without changing the ledger."""
        user_id = str(user_id)
        with self._db() as db:
            row = db.execute(
                "SELECT plan FROM overrides WHERE user_id=?",
                (user_id,),
            ).fetchone()
        if row and row[0] == PREMIUM_PLAN:
            return PREMIUM_PLAN
        return "free"

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
                "SELECT message_limit, exempt, plan FROM overrides WHERE user_id=?",
                (user_id,),
            ).fetchone() or (None, 0, "")
            if override[1]:
                return None
            plan = override[2] if override[2] == PREMIUM_PLAN else "free"
            limit = resolved_message_limit(default_limit, override[0], plan)
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
        plan: str | None = None,
        clear: bool = False,
        reset: bool = False,
    ) -> None:
        user_id = str(user_id)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            if clear:
                db.execute("DELETE FROM overrides WHERE user_id=?", (user_id,))
            elif limit is not None or exempt is not None or plan is not None:
                row = db.execute(
                    "SELECT message_limit, exempt, plan FROM overrides WHERE user_id=?",
                    (user_id,),
                ).fetchone() or (None, 0, "")
                new_limit = row[0] if limit is None else max(1, int(limit))
                new_exempt = row[1] if exempt is None else int(bool(exempt))
                if plan is None:
                    new_plan = row[2] or ""
                else:
                    normalized = str(plan).strip().lower()
                    if normalized in {"", "free"}:
                        new_plan = ""
                    elif normalized == PREMIUM_PLAN:
                        new_plan = PREMIUM_PLAN
                    else:
                        raise ValueError("plan must be free or premium")
                db.execute(
                    "INSERT INTO overrides(user_id, message_limit, exempt, plan) "
                    "VALUES(?,?,?,?) "
                    "ON CONFLICT(user_id) DO UPDATE SET "
                    "message_limit=excluded.message_limit, exempt=excluded.exempt, "
                    "plan=excluded.plan",
                    (user_id, new_limit, new_exempt, new_plan),
                )
            if reset:
                db.execute("DELETE FROM messages WHERE user_id=?", (user_id,))
