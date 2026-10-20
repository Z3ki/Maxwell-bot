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


_OFF = {"0", "false", "no", "off"}
LINK_TTL = 15 * 60
LINK_SESSION_TTL = 8 * 3600


def _valid_origin(parsed, *, allow_path: bool) -> bool:
    local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    return bool(
        (parsed.scheme == "https" or (local and parsed.scheme == "http"))
        and parsed.hostname
        and not parsed.username
        and not parsed.password
        and (allow_path or not parsed.path)
        and not parsed.query
        and not parsed.fragment
    )


def dashboard_key(root: Path | None) -> bytes | None:
    """MAXWELL_DASHBOARD_SECRET, else a generated key under the data dir."""
    raw = os.getenv("MAXWELL_DASHBOARD_SECRET", "").strip()
    if raw:
        try:
            key = bytes.fromhex(raw)
        except ValueError:
            key = b""
        if len(key) == 32:
            return key
    if root is None:
        return None
    from plugins.user_storage.store import load_or_create_key

    return load_or_create_key(Path(root) / "dashboard" / "session.key")


@dataclass(frozen=True)
class DashboardConfig:
    """Two sign-in modes.

    ``oauth``: Discord authorization-code login on a dedicated dashboard host.
    The session is an HttpOnly cookie, which is only safe because nothing
    else (no generated site) runs JavaScript on that origin.

    ``link``: no OAuth app needed. ``/dashboard`` in Discord hands the user a
    one-time link on the public site. Generated sites share that origin, so
    the session never touches a cookie or web storage: the page trades the
    link for a bearer token and keeps it in memory. Reload means a new link.
    That's the price of sharing a host with arbitrary user JavaScript.
    """

    origin: str
    client_id: str
    client_secret: str
    key: bytes
    bot_token: str
    mode: str = "oauth"
    base_path: str = ""

    @property
    def link_mode(self):
        return self.mode == "link"

    @property
    def url(self):
        return self.origin + self.base_path + "/dashboard/"

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
    def from_env(cls, root: Path | None = None):
        origin = os.getenv("MAXWELL_DASHBOARD_URL", "").rstrip("/")
        if not origin:
            return cls.link_from_env(root)
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

    @classmethod
    def link_from_env(cls, root: Path | None = None):
        if os.getenv("MAXWELL_DASHBOARD_LINKS", "1").strip().lower() in _OFF:
            return None
        public = os.getenv("MAXWELL_PUBLIC_BASE_URL", "").strip().rstrip("/")
        token = (
            os.getenv("DISCORD_BOT_TOKEN") or os.getenv("DISCORD_TOKEN") or ""
        ).strip()
        parsed = urlsplit(public)
        if not public or not token or not _valid_origin(parsed, allow_path=True):
            return None
        key = dashboard_key(root)
        if key is None:
            return None
        origin = f"{parsed.scheme}://{parsed.netloc}"
        client_id = os.getenv("DISCORD_CLIENT_ID", "").strip()
        return cls(
            origin,
            client_id if client_id.isdigit() else "",
            "",
            key,
            token,
            mode="link",
            base_path=parsed.path.rstrip("/"),
        )


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
            db.execute(
                "CREATE TABLE IF NOT EXISTS links (id TEXT PRIMARY KEY, uid TEXT NOT NULL, payload BLOB NOT NULL, expires REAL NOT NULL)"
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
        db.execute("DELETE FROM links WHERE expires <= ?", (now,))

    def _seal(self, identity: str, value: dict) -> bytes:
        nonce = secrets.token_bytes(12)
        return nonce + self.cipher.encrypt(nonce, json.dumps(value).encode(), identity.encode())

    def _open(self, identity: str, payload: bytes):
        try:
            return json.loads(self.cipher.decrypt(payload[:12], payload[12:], identity.encode()))
        except Exception:
            return None

    def mint_link(self, user: dict, guilds: list[dict], *, ttl: int = LINK_TTL) -> str:
        """One-time sign-in link token, written by the bot, redeemed by the API."""
        token = secrets.token_urlsafe(32)
        identity = _digest("link:" + token)
        uid = str(user["id"])
        payload = self._seal(identity, {"user": user, "guilds": guilds})
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self.prune(db)
            if db.execute("SELECT COUNT(*) FROM links WHERE uid=?", (uid,)).fetchone()[0] >= 5:
                # Older unused links die first; nobody needs six open tabs of it.
                db.execute(
                    "DELETE FROM links WHERE id IN (SELECT id FROM links WHERE uid=? ORDER BY expires LIMIT 1)",
                    (uid,),
                )
            if db.execute("SELECT COUNT(*) FROM links").fetchone()[0] >= 10000:
                raise ValueError("Too many sign-in links. Try again shortly.")
            db.execute(
                "INSERT INTO links VALUES (?, ?, ?, ?)",
                (identity, uid, payload, time.time() + max(30, min(int(ttl), 3600))),
            )
        return token

    def redeem_link(self, token: str):
        """Consume a link token exactly once; returns its payload or None."""
        if not token or len(token) > 128:
            return None
        identity = _digest("link:" + token)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT payload FROM links WHERE id=? AND expires>?", (identity, time.time())
            ).fetchone()
            db.execute("DELETE FROM links WHERE id=?", (identity,))
        return self._open(identity, row[0]) if row else None

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

    def create(self, user: dict, token: str, lifetime: int, **extra):
        cookie = secrets.token_urlsafe(32)
        session = {"user": user, "token": token, "csrf": secrets.token_urlsafe(32), **extra}
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

    def delete_user(self, user_id):
        with self.db() as db:
            for identity, payload in db.execute("SELECT id,payload FROM sessions").fetchall():
                try:
                    data = json.loads(self.cipher.decrypt(payload[:12], payload[12:], identity.encode()))
                except Exception:
                    # Unreadable sessions are unusable and should not be retained.
                    db.execute("DELETE FROM sessions WHERE id=?", (identity,))
                    continue
                if str(data.get("user", {}).get("id")) == str(user_id):
                    db.execute("DELETE FROM sessions WHERE id=?", (identity,))
            db.execute("DELETE FROM links WHERE uid=?", (str(user_id),))
