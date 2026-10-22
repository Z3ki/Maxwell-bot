"""Premium wake-up schedules. One sqlite file, read by the bot and the dashboard.

Rows are owned by a Discord user id and every user-facing query is scoped by
it; there is no "get schedule by id" without an owner on purpose.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Iterator

_EDITABLE = frozenset({"name", "kind", "spec", "tz", "prompt", "enabled", "next_run", "last_status"})

# Fixed statements only. Edits read the row, merge in Python and write every
# mutable column back inside one BEGIN IMMEDIATE, so nobody gets to assemble
# SQL out of caller-supplied keys, not even "trusted" ones.
_INSERT = (
    "INSERT INTO schedules(id, user_id, name, kind, spec, tz, prompt, channel_id, guild_id, dm, enabled, "
    "next_run, last_run, last_status, last_job, runs, failures, created_at, updated_at) VALUES("
    ":id, :user_id, :name, :kind, :spec, :tz, :prompt, :channel_id, :guild_id, :dm, :enabled, "
    ":next_run, :last_run, :last_status, :last_job, :runs, :failures, :created_at, :updated_at)"
)
_SAVE = (
    "UPDATE schedules SET name=:name, kind=:kind, spec=:spec, tz=:tz, prompt=:prompt, enabled=:enabled, "
    "next_run=:next_run, last_run=:last_run, last_status=:last_status, last_job=:last_job, runs=:runs, "
    "failures=:failures, updated_at=:updated_at WHERE id=:id AND user_id=:user_id"
)


def max_per_user() -> int:
    try:
        value = int(os.environ.get("MAXWELL_SCHEDULE_MAX_PER_USER", "") or 10)
    except ValueError:
        value = 10
    return max(1, min(value, 100))


def min_interval_minutes() -> int:
    try:
        value = int(os.environ.get("MAXWELL_SCHEDULE_MIN_MINUTES", "") or 15)
    except ValueError:
        value = 15
    return max(1, min(value, 1440))


def daily_run_limit() -> int:
    try:
        value = int(os.environ.get("MAXWELL_SCHEDULE_DAILY_RUNS", "") or 48)
    except ValueError:
        value = 48
    return max(1, min(value, 1440))


class ScheduleLimitError(ValueError):
    pass


class ScheduleStore:
    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS schedules ("
                "id TEXT PRIMARY KEY, user_id TEXT NOT NULL, name TEXT NOT NULL, "
                "kind TEXT NOT NULL, spec TEXT NOT NULL, tz TEXT NOT NULL, prompt TEXT NOT NULL, "
                "channel_id TEXT NOT NULL DEFAULT '', guild_id TEXT NOT NULL DEFAULT '', "
                "dm INTEGER NOT NULL DEFAULT 0, enabled INTEGER NOT NULL DEFAULT 1, "
                "next_run REAL, last_run REAL, last_status TEXT NOT NULL DEFAULT '', "
                "last_job TEXT NOT NULL DEFAULT '', runs INTEGER NOT NULL DEFAULT 0, "
                "failures INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, "
                "updated_at REAL NOT NULL)"
            )
            db.execute("CREATE INDEX IF NOT EXISTS schedules_due ON schedules(enabled, next_run)")
            db.execute("CREATE INDEX IF NOT EXISTS schedules_user ON schedules(user_id)")
            db.execute(
                "CREATE TABLE IF NOT EXISTS runs (schedule_id TEXT NOT NULL, user_id TEXT NOT NULL, "
                "at REAL NOT NULL, status TEXT NOT NULL, job TEXT NOT NULL DEFAULT '')"
            )
            db.execute("CREATE INDEX IF NOT EXISTS runs_user ON runs(user_id, at)")

    @contextlib.contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA busy_timeout=30000")
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _row(row) -> dict:
        data = dict(row)
        data["dm"] = bool(data.get("dm"))
        data["enabled"] = bool(data.get("enabled"))
        return data

    def add(self, *, user_id: str, name: str, kind: str, spec: str, tz: str, prompt: str,
            channel_id: str, guild_id: str, dm: bool, next_run: float) -> dict:
        now = time.time()
        row = {
            "id": secrets.token_hex(4),
            "user_id": str(user_id),
            "name": name,
            "kind": kind,
            "spec": spec,
            "tz": tz,
            "prompt": prompt,
            "channel_id": str(channel_id or ""),
            "guild_id": str(guild_id or ""),
            "dm": int(bool(dm)),
            "enabled": 1,
            "next_run": next_run,
            "last_run": None,
            "last_status": "",
            "last_job": "",
            "runs": 0,
            "failures": 0,
            "created_at": now,
            "updated_at": now,
        }
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            count = db.execute("SELECT COUNT(*) FROM schedules WHERE user_id=?", (row["user_id"],)).fetchone()[0]
            if count >= max_per_user():
                raise ScheduleLimitError(f"you already have {count} schedules (limit {max_per_user()}); delete one first")
            db.execute(_INSERT, row)
        return self._row(row)

    def list_user(self, user_id: str) -> list[dict]:
        with self._db() as db:
            rows = db.execute(
                "SELECT * FROM schedules WHERE user_id=? ORDER BY created_at", (str(user_id),)
            ).fetchall()
        return [self._row(r) for r in rows]

    def get(self, user_id: str, schedule_id: str) -> dict | None:
        with self._db() as db:
            row = db.execute(
                "SELECT * FROM schedules WHERE user_id=? AND id=?", (str(user_id), str(schedule_id))
            ).fetchone()
        return self._row(row) if row else None

    def update(self, user_id: str, schedule_id: str, **fields) -> dict | None:
        bad = set(fields) - _EDITABLE
        if bad:
            raise ValueError(f"not editable: {sorted(bad)}")
        if "enabled" in fields:
            fields["enabled"] = int(bool(fields["enabled"]))
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                "SELECT * FROM schedules WHERE user_id=? AND id=?", (str(user_id), str(schedule_id))
            ).fetchone()
            if current is None:
                return None
            row = {**dict(current), **fields, "updated_at": time.time()}
            if fields.get("enabled"):
                # Re-enabling is a fresh start; old failures don't get to re-pause it.
                row["failures"] = 0
            db.execute(_SAVE, row)
        return self._row(row)

    def delete(self, user_id: str, schedule_id: str) -> bool:
        with self._db() as db:
            cursor = db.execute(
                "DELETE FROM schedules WHERE user_id=? AND id=?", (str(user_id), str(schedule_id))
            )
            return bool(cursor.rowcount)

    def delete_user(self, user_id: str) -> int:
        """Privacy deletion: schedules and their run history."""
        with self._db() as db:
            count = db.execute("DELETE FROM schedules WHERE user_id=?", (str(user_id),)).rowcount
            db.execute("DELETE FROM runs WHERE user_id=?", (str(user_id),))
        return count

    # ---- runner side ------------------------------------------------------

    def due(self, now: float, limit: int = 25) -> list[dict]:
        with self._db() as db:
            rows = db.execute(
                "SELECT * FROM schedules WHERE enabled=1 AND next_run IS NOT NULL AND next_run<=? "
                "ORDER BY next_run LIMIT ?",
                (now, int(limit)),
            ).fetchall()
        return [self._row(r) for r in rows]

    def next_due(self) -> float | None:
        with self._db() as db:
            row = db.execute(
                "SELECT MIN(next_run) FROM schedules WHERE enabled=1 AND next_run IS NOT NULL"
            ).fetchone()
        return float(row[0]) if row and row[0] is not None else None

    def record(self, schedule_id: str, *, status: str, next_run: float | None, enabled: bool,
               ran: bool = False, job: str = "", failed: bool = False, expected_next: float | None = None) -> bool:
        """Finish one firing. ``expected_next`` guards against an edit made mid-run."""
        now = time.time()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT * FROM schedules WHERE id=?", (schedule_id,)).fetchone()
            if current is None:
                return False
            row = {**dict(current), "last_status": status[:200], "updated_at": now}
            # Someone rescheduled or paused it while we were firing: keep their
            # next_run/enabled. A pause leaves next_run untouched, so the
            # enabled flag must match too or this write silently resumes it.
            if expected_next is None or (
                current["next_run"] == expected_next and current["enabled"]
            ):
                row["next_run"], row["enabled"] = next_run, int(bool(enabled))
            if ran:
                row.update(last_run=now, runs=int(row["runs"]) + 1, failures=0, last_job=job)
            elif failed:
                row["failures"] = int(row["failures"]) + 1
            db.execute(_SAVE, row)
            if ran or failed or status:
                db.execute(
                    "INSERT INTO runs(schedule_id, user_id, at, status, job) VALUES(?,?,?,?,?)",
                    (schedule_id, row["user_id"], now, status[:200], job),
                )
                db.execute("DELETE FROM runs WHERE at<?", (now - 30 * 86400,))
        return True

    def retry_later(self, schedule_id: str, when: float, status: str) -> None:
        with self._db() as db:
            db.execute(
                "UPDATE schedules SET next_run=?, last_status=?, updated_at=? WHERE id=?",
                (when, status[:200], time.time(), schedule_id),
            )

    def runs_since(self, user_id: str, since: float) -> int:
        with self._db() as db:
            return int(
                db.execute(
                    "SELECT COUNT(*) FROM runs WHERE user_id=? AND at>=? AND job!=''", (str(user_id), since)
                ).fetchone()[0]
            )

    def history(self, user_id: str, limit: int = 30) -> list[dict]:
        with self._db() as db:
            rows = db.execute(
                "SELECT schedule_id, at, status, job FROM runs WHERE user_id=? ORDER BY at DESC LIMIT ?",
                (str(user_id), int(limit)),
            ).fetchall()
        return [dict(r) for r in rows]
