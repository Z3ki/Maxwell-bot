"""Durable job metadata only: never persist task text, reports or credentials."""

from contextlib import closing, contextmanager
from contextvars import ContextVar
from pathlib import Path
import sqlite3
import time

from privacy import owned_write_allowed, retention_days

active_job = ContextVar("maxwell_active_worker_job", default=None)
LIVE_STATES = ("queued", "running", "cancelling")


class TaskJobStore:
    def __init__(self, path, *, recover=False):
        self.path = Path(path)
        self.root = self.path.parent.parent.parent
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path.parent.chmod(0o700)
        with self.db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL, source_id TEXT NOT NULL,
                channel_id TEXT NOT NULL, guild_id TEXT NOT NULL, private INTEGER NOT NULL,
                kinds TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                state TEXT NOT NULL, effects INTEGER NOT NULL DEFAULT 0,
                delivery TEXT NOT NULL DEFAULT 'pending', tool_calls INTEGER NOT NULL DEFAULT 0,
                UNIQUE(user_id,source_id))""")
            if recover:
                db.execute(
                    "UPDATE jobs SET state='interrupted', updated_at=? WHERE state IN ('queued','running','cancelling')",
                    (time.time(),),
                )
                db.execute(
                    "UPDATE jobs SET delivery='unknown' WHERE delivery='attempting'"
                )
        self.path.chmod(0o600)

    @contextmanager
    def db(self):
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA secure_delete=ON")
            with db:
                yield db

    def create(self, row, started_at):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if not owned_write_allowed(self.root, row["user_id"], started_at):
                raise ValueError("Request predates privacy deletion")
            existing = db.execute(
                "SELECT * FROM jobs WHERE user_id=? AND source_id=?",
                (row["user_id"], row["source_id"]),
            ).fetchone()
            if existing:
                return dict(existing)
            db.execute(
                "INSERT INTO jobs(id,user_id,source_id,channel_id,guild_id,private,kinds,created_at,updated_at,state) VALUES(?,?,?,?,?,?,?,?,?,'queued')",
                tuple(
                    row[k]
                    for k in (
                        "id",
                        "user_id",
                        "source_id",
                        "channel_id",
                        "guild_id",
                        "private",
                        "kinds",
                        "created_at",
                        "updated_at",
                    )
                ),
            )
            return row

    def get(self, job_id):
        with self.db() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            return dict(row) if row else None

    def list(self, uid):
        with self.db() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM jobs WHERE user_id=? ORDER BY created_at DESC LIMIT 20",
                    (str(uid),),
                )
            ]

    def update(self, job_id, **values):
        if not values or not values.keys() <= {
            "state",
            "effects",
            "delivery",
            "tool_calls",
        }:
            raise ValueError("Unsupported job metadata")
        with self.db() as db:
            return (
                db.execute(
                    "UPDATE jobs SET "
                    + ",".join(k + "=?" for k in values)
                    + ",updated_at=? WHERE id=?",
                    (*values.values(), time.time(), job_id),
                ).rowcount
                == 1
            )

    def claim_delivery(self, job_id):
        with self.db() as db:
            return (
                db.execute(
                    "UPDATE jobs SET delivery='attempting',updated_at=? WHERE id=? AND delivery='pending'",
                    (time.time(), job_id),
                ).rowcount
                == 1
            )

    def delete(self, uid):
        with self.db() as db:
            return db.execute("DELETE FROM jobs WHERE user_id=?", (str(uid),)).rowcount

    def prune(self):
        cutoff = (
            time.time()
            - retention_days("MAXWELL_TASK_METADATA_RETENTION_DAYS", 1) * 86400
        )
        with self.db() as db:
            return db.execute(
                "DELETE FROM jobs WHERE updated_at<? AND state NOT IN ('queued','running','cancelling')",
                (cutoff,),
            ).rowcount
