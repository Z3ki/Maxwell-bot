"""Internal token and API-cost accounting for spending protection."""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DAILY_LIMIT = 3_000_000


class DailyTokenLimitExceeded(Exception):
    def __init__(self, message="This request was stopped by an internal spending guard.", *, spent=None, limit=None):
        super().__init__(message)
        self.spent = spent
        self.limit = limit


class DailyTokens:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS usage (day TEXT NOT NULL, user_id TEXT NOT NULL, spent INTEGER NOT NULL DEFAULT 0, reserved INTEGER NOT NULL DEFAULT 0, cost_microusd INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(day, user_id))")
            cols = {row[1] for row in db.execute("PRAGMA table_info(usage)")}
            if "cost_microusd" not in cols:
                db.execute("ALTER TABLE usage ADD COLUMN cost_microusd INTEGER NOT NULL DEFAULT 0")
            db.execute("CREATE TABLE IF NOT EXISTS reservations (id TEXT PRIMARY KEY, day TEXT NOT NULL, user_id TEXT NOT NULL, tokens INTEGER NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS overrides (user_id TEXT PRIMARY KEY, daily_limit INTEGER, exempt INTEGER NOT NULL DEFAULT 0)")

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.execute("PRAGMA busy_timeout=30000")
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def today():
        return datetime.now(timezone.utc).date().isoformat()

    def status(self, user_id: str, default_limit: int) -> dict:
        day = self.today()
        with self._db() as db:
            row = db.execute("SELECT spent, reserved, COALESCE(cost_microusd, 0) FROM usage WHERE day=? AND user_id=?", (day, str(user_id))).fetchone() or (0, 0, 0)
            override = db.execute("SELECT daily_limit, exempt FROM overrides WHERE user_id=?", (str(user_id),)).fetchone() or (None, 0)
        limit = override[0] if override[0] is not None else default_limit
        return {"day": day, "user_id": str(user_id), "spent": row[0], "reserved": row[1], "cost_usd": row[2] / 1_000_000, "limit": limit, "override": override[0], "exempt": bool(override[1])}

    def reserve(self, user_id: str, default_limit: int, prompt_estimate: int, max_output: int) -> tuple[str | None, int]:
        """Reserve estimated input and allowed output atomically across processes."""
        user_id = str(user_id)
        day = self.today()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            override = db.execute("SELECT daily_limit, exempt FROM overrides WHERE user_id=?", (user_id,)).fetchone() or (None, 0)
            if override[1]:
                return None, max_output
            limit = override[0] if override[0] is not None else default_limit
            row = db.execute("SELECT spent, reserved FROM usage WHERE day=? AND user_id=?", (day, user_id)).fetchone() or (0, 0)
            available = max(0, int(limit) - row[0] - row[1])
            output = min(max(1, int(max_output)), available - max(1, int(prompt_estimate)))
            if output < 1:
                raise DailyTokenLimitExceeded(spent=row[0], limit=limit)
            amount = max(1, int(prompt_estimate)) + output
            reservation_id = uuid.uuid4().hex
            db.execute("INSERT INTO usage(day,user_id,spent,reserved) VALUES(?,?,0,?) ON CONFLICT(day,user_id) DO UPDATE SET reserved=reserved+excluded.reserved", (day, user_id, amount))
            db.execute("INSERT INTO reservations(id,day,user_id,tokens) VALUES(?,?,?,?)", (reservation_id, day, user_id, amount))
            return reservation_id, output

    def settle(self, reservation_id: str | None, actual: int, cost_usd: float = 0.0) -> None:
        if not reservation_id:
            return
        micro = int(round(max(0.0, float(cost_usd or 0)) * 1_000_000))
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT day,user_id,tokens FROM reservations WHERE id=?", (reservation_id,)).fetchone()
            if row is None:
                return
            db.execute("UPDATE usage SET spent=spent+?, reserved=MAX(0,reserved-?), cost_microusd=COALESCE(cost_microusd,0)+? WHERE day=? AND user_id=?", (max(0, int(actual)), row[2], micro, row[0], row[1]))
            db.execute("DELETE FROM reservations WHERE id=?", (reservation_id,))

    def configure(self, user_id: str, *, limit: int | None = None, exempt: bool | None = None, clear: bool = False, reset: bool = False) -> None:
        user_id = str(user_id)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            if clear:
                db.execute("DELETE FROM overrides WHERE user_id=?", (user_id,))
            elif limit is not None or exempt is not None:
                row = db.execute("SELECT daily_limit,exempt FROM overrides WHERE user_id=?", (user_id,)).fetchone() or (None, 0)
                new_limit = row[0] if limit is None else max(1, int(limit))
                new_exempt = row[1] if exempt is None else int(exempt)
                db.execute("INSERT INTO overrides(user_id,daily_limit,exempt) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET daily_limit=excluded.daily_limit,exempt=excluded.exempt", (user_id, new_limit, new_exempt))
            if reset:
                db.execute("UPDATE usage SET spent=0 WHERE day=? AND user_id=?", (self.today(), user_id))
