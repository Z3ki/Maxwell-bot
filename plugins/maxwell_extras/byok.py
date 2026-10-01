"""Encrypted personal credentials and the fixed BYOK provider registry."""

from __future__ import annotations

import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


PROVIDERS: dict[str, dict[str, str]] = {
    "openai": {
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "suggested_model": "gpt-4.1-mini",
    },
    "openrouter": {
        "label": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "suggested_model": "openai/gpt-4.1-mini",
    },
    "groq": {
        "label": "Groq",
        "base_url": "https://api.groq.com/openai/v1",
        "suggested_model": "openai/gpt-oss-120b",
    },
}

_MODEL_RE = re.compile(r"^[A-Za-z0-9_./:@~+-]{1,120}$")
_KEY_MIN = 8
_KEY_MAX = 512


class VaultUnavailable(RuntimeError):
    """The operator has not configured a usable encryption key."""


def _master_key(value: str | bytes | None) -> bytes | None:
    if value is None:
        value = os.environ.get("MAXWELL_BYOK_ENCRYPTION_KEY", "")
    if isinstance(value, bytes):
        return value if len(value) == 32 else None
    text = str(value or "").strip()
    if not text:
        return None
    try:
        raw = bytes.fromhex(text)
    except ValueError:
        return None
    return raw if len(raw) == 32 else None


class CredentialVault:
    """SQLite metadata plus AES-GCM encrypted keys, bound to owner/provider."""

    def __init__(self, path: str | Path, master_key: str | bytes | None = None):
        self.path = Path(path)
        self._key = _master_key(master_key)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        with self._db() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS credentials (
                    user_id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    nonce BLOB NOT NULL,
                    ciphertext BLOB NOT NULL,
                    updated_at INTEGER NOT NULL
                )"""
            )
        self._secure_mode()

    @property
    def enabled(self) -> bool:
        return self._key is not None

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.execute("PRAGMA secure_delete=ON")
        db.execute("PRAGMA journal_mode=DELETE")
        return db

    @contextmanager
    def _db(self):
        db = self._connect()
        try:
            yield db
        finally:
            db.close()

    def _secure_mode(self) -> None:
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def _require_key(self) -> bytes:
        if self._key is None:
            raise VaultUnavailable(
                "personal BYOK is unavailable because operator encryption is not configured"
            )
        return self._key

    @staticmethod
    def _owner(user_id: Any) -> str:
        owner = str(user_id or "").strip()
        if not owner.isdigit():
            raise ValueError("authenticated owner ID is required")
        return owner

    @staticmethod
    def _provider(provider: str) -> str:
        selected = str(provider or "").strip().lower()
        if selected not in PROVIDERS:
            raise ValueError("select a supported provider")
        return selected

    @staticmethod
    def _model(model: str) -> str:
        selected = str(model or "").strip()
        if not _MODEL_RE.fullmatch(selected):
            raise ValueError("enter a valid model ID (1 to 120 safe characters)")
        return selected

    @staticmethod
    def _api_key(api_key: str) -> str:
        value = str(api_key or "").strip()
        if not (_KEY_MIN <= len(value) <= _KEY_MAX) or any(
            ord(char) < 33 or ord(char) == 127 for char in value
        ):
            raise ValueError("API key must be 8 to 512 non-whitespace characters")
        return value

    @staticmethod
    def _aad(owner: str, provider: str) -> bytes:
        return f"maxwell-byok-v1\0{owner}\0{provider}".encode("utf-8")

    def save(self, user_id: Any, provider: str, model: str, api_key: str) -> None:
        owner = self._owner(user_id)
        selected_provider = self._provider(provider)
        selected_model = self._model(model)
        secret = self._api_key(api_key).encode("utf-8")
        key = self._require_key()
        nonce = secrets.token_bytes(12)
        ciphertext = AESGCM(key).encrypt(
            nonce, secret, self._aad(owner, selected_provider)
        )
        with self._db() as db:
            db.execute(
                """INSERT INTO credentials(user_id, provider, model, nonce, ciphertext, updated_at)
                   VALUES(?, ?, ?, ?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET
                     provider=excluded.provider, model=excluded.model,
                     nonce=excluded.nonce, ciphertext=excluded.ciphertext,
                     updated_at=excluded.updated_at""",
                (owner, selected_provider, selected_model, nonce, ciphertext, int(time.time())),
            )
        self._secure_mode()

    def get(self, user_id: Any) -> dict[str, str] | None:
        owner = self._owner(user_id)
        key = self._require_key()
        with self._db() as db:
            row = db.execute(
                "SELECT provider, model, nonce, ciphertext FROM credentials WHERE user_id=?",
                (owner,),
            ).fetchone()
        if not row:
            return None
        provider, model, nonce, ciphertext = row
        if provider not in PROVIDERS:
            raise ValueError("stored provider is not supported")
        try:
            secret = AESGCM(key).decrypt(
                bytes(nonce), bytes(ciphertext), self._aad(owner, provider)
            ).decode("utf-8")
        except (InvalidTag, UnicodeError) as exc:
            raise VaultUnavailable("stored BYOK credential could not be decrypted") from exc
        return {"provider": provider, "model": str(model), "api_key": secret}

    def status(self, user_id: Any) -> dict[str, str] | None:
        credential = self.get(user_id)
        if credential is None:
            return None
        provider = credential["provider"]
        return {
            "provider": provider,
            "provider_label": PROVIDERS[provider]["label"],
            "model": credential["model"],
            "masked_key": "••••" + credential["api_key"][-4:],
        }

    def has_credential(self, user_id: Any) -> bool:
        """Check only whether a record exists, even when the key is unavailable."""
        owner = self._owner(user_id)
        with self._db() as db:
            row = db.execute(
                "SELECT 1 FROM credentials WHERE user_id=?", (owner,)
            ).fetchone()
        return row is not None

    def delete(self, user_id: Any) -> bool:
        owner = self._owner(user_id)
        with self._db() as db:
            cursor = db.execute("DELETE FROM credentials WHERE user_id=?", (owner,))
            removed = cursor.rowcount > 0
            db.execute("PRAGMA secure_delete=ON")
            db.execute("VACUUM")
        return removed

    def rotate_master_key(self, new_master_key: str | bytes) -> None:
        old_key = self._require_key()
        new_key = _master_key(new_master_key)
        if new_key is None:
            raise ValueError("new encryption key must be exactly 32 bytes (64 hex characters)")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                rows = db.execute(
                    "SELECT user_id, provider, nonce, ciphertext FROM credentials"
                ).fetchall()
                rotated = []
                for owner, provider, nonce, ciphertext in rows:
                    aad = self._aad(owner, provider)
                    secret = AESGCM(old_key).decrypt(bytes(nonce), bytes(ciphertext), aad)
                    next_nonce = secrets.token_bytes(12)
                    next_ciphertext = AESGCM(new_key).encrypt(next_nonce, secret, aad)
                    rotated.append((next_nonce, next_ciphertext, owner))
                db.executemany(
                    "UPDATE credentials SET nonce=?, ciphertext=?, updated_at=? WHERE user_id=?",
                    [(nonce, ciphertext, int(time.time()), owner) for nonce, ciphertext, owner in rotated],
                )
                db.commit()
            except Exception:
                db.rollback()
                raise
            try:
                db.execute("VACUUM")
            except sqlite3.Error:
                # Re-encryption already committed; failing to compact must not
                # leave this process using the old key for new reads.
                pass
        self._key = new_key
        self._secure_mode()


def make_request_provider(bot: Any, credential: dict[str, str]):
    """Build an uncached, single-credential client for one authenticated user."""
    provider_id = credential["provider"]
    config = PROVIDERS[provider_id]
    from maxwell_core.providers.models import ProviderPolicy

    client = bot._make_chat_provider(
        name=f"byok:{provider_id}",
        base_url=config["base_url"],
        model=credential["model"],
        max_tokens=4096,
        temperature=0.4,
        api_key=credential["api_key"],
        disable_reasoning=True,
        retry_attempts=1,
        empty_response_retries=0,
        policy=ProviderPolicy(
            sensitive_credentials=True, public_network_only=True,
            allow_provider_fallback=False, max_request_seconds=300,
            max_response_bytes=2 * 1024 * 1024, max_output_tokens=4096,
        ),
    )
    client.available = True
    return client


__all__ = [
    "CredentialVault",
    "PROVIDERS",
    "VaultUnavailable",
    "make_request_provider",
]
