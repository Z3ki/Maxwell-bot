"""Discord authorization-code login with encrypted, revocable server sessions."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


@dataclass(frozen=True)
class DashboardConfig:
    origin: str
    client_id: str
    client_secret: str
    key: bytes
    bot_token: str

    @property
    def secure(self):
        return self.origin.startswith("https://")

    @property
    def cookie(self):
        return "__Host-maxwell_session" if self.secure else "maxwell_dev_session"

    @property
    def flow_cookie(self):
        return "__Host-maxwell_login" if self.secure else "maxwell_dev_login"

    @property
    def callback(self):
        return self.origin + "/api/dashboard/callback"

    @classmethod
    def from_env(cls):
        origin = os.getenv("MAXWELL_DASHBOARD_URL", "").rstrip("/")
        if not origin:
            return None
        parsed = urlsplit(origin)
        local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if (
            parsed.scheme != "https"
            and not (local and parsed.scheme == "http")
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("MAXWELL_DASHBOARD_URL must be a dedicated HTTPS origin")
        public = urlsplit(os.getenv("MAXWELL_PUBLIC_BASE_URL", "")).hostname
        if public and public == parsed.hostname and not local:
            raise ValueError("Dashboard must use a different host from generated sites")
        key = bytes.fromhex(os.getenv("MAXWELL_DASHBOARD_SECRET", ""))
        client_id = os.getenv("DISCORD_CLIENT_ID", "").strip()
        secret = os.getenv("DISCORD_CLIENT_SECRET", "").strip()
        token = (
            os.getenv("DISCORD_BOT_TOKEN") or os.getenv("DISCORD_TOKEN") or ""
        ).strip()
        if len(key) != 32 or not client_id.isdigit() or not secret or not token:
            raise ValueError(
                "Dashboard Discord credentials and 32-byte secret are required"
            )
        return cls(origin, client_id, secret, key, token)


def _digest(value: str):
    return hashlib.sha256(value.encode()).hexdigest()


class DashboardSessions:
    """Cookies hold opaque random IDs; Discord tokens never enter the browser."""

    def __init__(self, path: Path, key: bytes):
        self.path = path
        self.cipher = AESGCM(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, payload BLOB NOT NULL, expires REAL NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS flows (id TEXT PRIMARY KEY, browser TEXT NOT NULL, expires REAL NOT NULL)"
            )
        os.chmod(path, 0o600)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def prune(db):
        now = time.time()
        db.execute("DELETE FROM flows WHERE expires <= ?", (now,))
        db.execute("DELETE FROM sessions WHERE expires <= ?", (now,))

    def begin(self):
        state, browser = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.db() as db:
            self.prune(db)
            if db.execute("SELECT COUNT(*) FROM flows").fetchone()[0] >= 10000:
                raise ValueError("Too many sign-ins. Try again shortly.")
            db.execute(
                "INSERT INTO flows VALUES (?, ?, ?)",
                (_digest(state), _digest(browser), time.time() + 300),
            )
        return state, browser

    def consume(self, state: str, browser: str):
        with self.db() as db:
            cursor = db.execute(
                "DELETE FROM flows WHERE id=? AND browser=? AND expires>?",
                (_digest(state), _digest(browser), time.time()),
            )
            return cursor.rowcount == 1

    def create(self, user: dict, token: str, lifetime: int):
        cookie = secrets.token_urlsafe(32)
        session = {"user": user, "token": token, "csrf": secrets.token_urlsafe(32)}
        nonce = secrets.token_bytes(12)
        identity = _digest(cookie)
        encrypted = nonce + self.cipher.encrypt(
            nonce, json.dumps(session).encode(), identity.encode()
        )
        expires = time.time() + min(max(lifetime, 1), 8 * 3600)
        with self.db() as db:
            self.prune(db)
            db.execute(
                "INSERT INTO sessions VALUES (?, ?, ?)", (identity, encrypted, expires)
            )
        return cookie

    def get(self, cookie: str):
        identity = _digest(cookie)
        with self.db() as db:
            row = db.execute(
                "SELECT payload FROM sessions WHERE id=? AND expires>?",
                (identity, time.time()),
            ).fetchone()
        if not row:
            return None
        try:
            return json.loads(
                self.cipher.decrypt(row[0][:12], row[0][12:], identity.encode())
            )
        except Exception:
            # A rotated key invalidates old sessions without exposing errors.
            return None

    def delete(self, cookie: str):
        with self.db() as db:
            db.execute("DELETE FROM sessions WHERE id=?", (_digest(cookie),))
