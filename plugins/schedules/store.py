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
    "failures=:failures, updated_at=:updated_at, retry_since=:retry_since, "
    "claim_token=:claim_token, claim_until=:claim_until, revision=:revision WHERE id=:id AND user_id=:user_id"
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
            db.execute("BEGIN IMMEDIATE")
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
            columns = {row[1] for row in db.execute("PRAGMA table_info(schedules)")}
            for name, declaration in (
                ("retry_since", "REAL"),
                ("claim_token", "TEXT NOT NULL DEFAULT ''"),
                ("claim_until", "REAL"),
                ("revision", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if name not in columns:
                    db.execute(f"ALTER TABLE schedules ADD COLUMN {name} {declaration}")
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

    def _require_current_request(self, user_id: str, started_at: float | None) -> None:
        """Refuse a write whose request began before privacy deletion.

        Must run inside the caller's immediate transaction. Deletion sets the
        marker before it takes this database, so a check outside the lock can
        pass and then insert after the rows are gone.
        """
        from privacy import owned_write_allowed, request_started_at

        when = request_started_at() if started_at is None else started_at
        if not owned_write_allowed(self.path.parent.parent.parent, user_id, when):
            raise ScheduleLimitError(
                "Request predates a privacy deletion; submit a new request"
            )


    def add(self, *, user_id: str, name: str, kind: str, spec: str, tz: str, prompt: str,
            channel_id: str, guild_id: str, dm: bool, next_run: float,
            started_at: float | None = None) -> dict:
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
            "retry_since": None,
            "claim_token": "",
            "claim_until": None,
            "revision": 0,
        }
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._require_current_request(row["user_id"], started_at)
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

    def update(self, user_id: str, schedule_id: str, *, started_at: float | None = None, **fields) -> dict | None:
        bad = set(fields) - _EDITABLE
        if bad:
            raise ValueError(f"not editable: {sorted(bad)}")
        if "enabled" in fields:
            fields["enabled"] = int(bool(fields["enabled"]))
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._require_current_request(user_id, started_at)
            current = db.execute(
                "SELECT * FROM schedules WHERE user_id=? AND id=?", (str(user_id), str(schedule_id))
            ).fetchone()
            if current is None:
                return None
            row = {**dict(current), **fields, "updated_at": time.time()}
            # Any user edit invalidates a pending firing's snapshot.
            row.update(claim_token="", claim_until=None, revision=int(current["revision"]) + 1)
            if "next_run" in fields or "enabled" in fields:
                row["retry_since"] = None
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

    def claim(self, row: dict, now: float, lease_seconds: float) -> dict | None:
        """Atomically reserve a due snapshot across runner instances/processes."""
        token = secrets.token_hex(16)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute(
                "UPDATE schedules SET claim_token=?, claim_until=?, "
                "retry_since=COALESCE(retry_since, next_run) "
                "WHERE id=? AND enabled=1 AND next_run=? AND next_run<=? AND revision=? "
                "AND (claim_until IS NULL OR claim_until<=?) "
                "AND NOT EXISTS (SELECT 1 FROM schedules AS active WHERE active.user_id=schedules.user_id "
                "AND active.claim_until>?)",
                (token, now + lease_seconds, row["id"], row["next_run"], now, row["revision"], now, now),
            ).rowcount
            if not changed:
                return None
            claimed = db.execute("SELECT * FROM schedules WHERE id=?", (row["id"],)).fetchone()
        return self._row(claimed)

    def claimed(self, row: dict) -> bool:
        """Recheck authorization after asynchronous preparation, before submission."""
        with self._db() as db:
            return db.execute(
                "SELECT 1 FROM schedules WHERE id=? AND enabled=1 AND claim_token=? AND claim_until>?",
                (row["id"], row["claim_token"], time.time()),
            ).fetchone() is not None

    def record(self, schedule_id: str, *, status: str, next_run: float | None, enabled: bool,
               ran: bool = False, job: str = "", failed: bool = False, expected_next: float | None = None,
               claim_token: str | None = None, retry: bool = False) -> bool:
        """Finish one firing without overwriting an edit or another runner's claim."""
        now = time.time()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT * FROM schedules WHERE id=?", (schedule_id,)).fetchone()
            if current is None:
                return False
            owned = claim_token is None or current["claim_token"] == claim_token
            if not owned and not ran:
                return False
            row = {**dict(current), "updated_at": now, "revision": int(current["revision"]) + 1}
            if owned:
                row["last_status"] = status[:200]
            # Someone rescheduled or paused it while we were firing: keep their
            # next_run/enabled. A pause leaves next_run untouched, so the
            # enabled flag must match too or this write silently resumes it.
            # A name/prompt edit clears the claim without moving next_run.
            # That firing still happened; advance it or the same due row
            # starts a second job. Do not advance if another claim holds it
            # or the owner changed when it runs.
            scheduling_unchanged = expected_next is None or (
                current["next_run"] == expected_next and current["enabled"]
            )
            claim_free = not current["claim_token"]
            if owned or (ran and scheduling_unchanged and claim_free):
                if scheduling_unchanged:
                    row["next_run"], row["enabled"] = next_run, int(bool(enabled))
                    row["retry_since"] = row["retry_since"] if retry else None
                row.update(claim_token="", claim_until=None)
                row["last_status"] = status[:200]
            if ran:
                row.update(last_run=now, runs=int(row["runs"]) + 1, last_job=job)
                if owned:
                    row["failures"] = 0
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

    def retry_later(self, row: dict, when: float, status: str, *, failed: bool = False) -> bool:
        return self.record(
            row["id"], status=status, next_run=when, enabled=True, failed=failed,
            expected_next=row["next_run"], claim_token=row["claim_token"], retry=True,
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
