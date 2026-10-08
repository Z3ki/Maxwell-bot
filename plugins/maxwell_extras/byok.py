"""Encrypted personal credentials and OpenAI-compatible BYOK providers."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from maxwell_core.providers.http import normalize_base_url


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
    "custom": {
        "label": "Custom OpenAI endpoint",
        "base_url": "",
        "suggested_model": "",
    },
}

_MODEL_RE = re.compile(r"^[A-Za-z0-9_./:@~+-]{1,120}$")
_KEY_MIN = 8
_KEY_MAX = 512
_URL_MAX = 300
_MAX_OUTPUT_TOKENS = 16384
_MODALITIES = {"text", "vision", "audio", "tools"}
_EFFORTS = {"", "none", "minimal", "low", "medium", "high", "max"}
_BOOL_WORDS = {
    "on": True,
    "off": False,
    "true": True,
    "false": False,
    "yes": True,
    "no": False,
    "1": True,
    "0": False,
}
# Credentials saved before model-support settings existed sent images and tools
# and kept reasoning off. An empty settings column keeps that behavior.
_LEGACY_SETTINGS: dict[str, Any] = {
    "vision": True,
    "audio": False,
    "tools": True,
    "reasoning": False,
    "max_tokens": 4096,
    "temperature": 0.4,
    "effort": "",
    "context": None,
}
_NEW_SETTINGS: dict[str, Any] = {
    "vision": False,
    "audio": False,
    "tools": True,
    "reasoning": False,
    "max_tokens": 4096,
    "temperature": 0.4,
    "effort": "",
    "context": None,
}


class VaultUnavailable(RuntimeError):
    """The operator has not configured a usable encryption key."""


def _blocked_host(host: str) -> bool:
    name = host.lower().rstrip(".")
    if name in {"localhost", "metadata", "metadata.google.internal"}:
        return True
    return name.endswith((".local", ".internal", ".localhost"))


def validate_public_endpoint(url: str, *, required: bool) -> str:
    """Accept a public HTTPS OpenAI-compatible base URL, or blank for a preset."""
    text = str(url or "").strip()
    if not text:
        if required:
            raise ValueError("enter a public HTTPS endpoint")
        return ""
    if len(text) > _URL_MAX or any(ord(char) < 33 or ord(char) == 127 for char in text):
        raise ValueError("endpoint URL must be a public HTTPS address")
    parsed = urlsplit(text)
    host = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("endpoint must be HTTPS, with no login, query, or fragment")
    if _blocked_host(host):
        raise ValueError("endpoint host must be public")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("endpoint address must be public")
    return normalize_base_url(text)


def effective_endpoint(provider: str, base_url: str = "") -> str:
    explicit = str(base_url or "").strip()
    if explicit:
        return explicit
    return str(PROVIDERS.get(str(provider or ""), {}).get("base_url") or "")


def format_modalities(settings: dict[str, Any]) -> str:
    parts = ["text"]
    if settings.get("vision"):
        parts.append("vision")
    if settings.get("audio"):
        parts.append("audio")
    if settings.get("tools"):
        parts.append("tools")
    return ", ".join(parts)


def _format_temperature(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0.4
    text = f"{number:.2f}".rstrip("0").rstrip(".")
    return text or "0"


def format_generation(settings: dict[str, Any]) -> str:
    parts = [
        f"reasoning={'on' if settings.get('reasoning') else 'off'}",
        f"max_tokens={int(settings.get('max_tokens') or 4096)}",
        f"temperature={_format_temperature(settings.get('temperature', 0.4))}",
    ]
    effort = str(settings.get("effort") or "").strip()
    if effort:
        parts.append(f"effort={effort}")
    context = settings.get("context")
    if context:
        parts.append(f"context={int(context)}")
    return " ".join(parts)


def parse_modalities(text: str) -> dict[str, bool]:
    tokens = [
        token
        for token in re.split(r"[\s,]+", str(text or "").strip().lower())
        if token
    ]
    unknown = [token for token in tokens if token not in _MODALITIES]
    if unknown:
        raise ValueError("modalities are text, vision, audio, and tools")
    chosen = set(tokens)
    return {
        "vision": "vision" in chosen,
        "audio": "audio" in chosen,
        "tools": "tools" in chosen,
    }


def _parse_bool(value: str, label: str) -> bool:
    try:
        return _BOOL_WORDS[value.strip().lower()]
    except KeyError:
        raise ValueError(f"{label} must be on or off") from None


def parse_generation(text: str) -> dict[str, Any]:
    """Read reasoning, max_tokens, temperature, effort, and context."""
    result = dict(_NEW_SETTINGS)
    raw = str(text or "").strip()
    if not raw:
        return result
    for part in re.split(r"[\s,]+", raw):
        if not part:
            continue
        if "=" not in part:
            raise ValueError(
                "generation settings use key=value, such as reasoning=off max_tokens=4096"
            )
        key, value = (piece.strip() for piece in part.split("=", 1))
        key = key.lower()
        if key in {"reasoning", "reason"}:
            result["reasoning"] = _parse_bool(value, "reasoning")
        elif key in {"max_tokens", "tokens", "max"}:
            try:
                tokens = int(value)
            except ValueError:
                raise ValueError("max_tokens must be a whole number") from None
            if not 16 <= tokens <= _MAX_OUTPUT_TOKENS:
                raise ValueError(f"max_tokens must be from 16 to {_MAX_OUTPUT_TOKENS}")
            result["max_tokens"] = tokens
        elif key in {"temperature", "temp"}:
            try:
                temperature = float(value)
            except ValueError:
                raise ValueError("temperature must be a number from 0 to 2") from None
            if not 0 <= temperature <= 2:
                raise ValueError("temperature must be a number from 0 to 2")
            result["temperature"] = round(temperature, 2)
        elif key in {"effort", "reasoning_effort"}:
            effort = value.strip().lower()
            if effort not in _EFFORTS:
                raise ValueError("effort must be minimal, low, medium, high, or max")
            result["effort"] = "" if effort == "none" else effort
        elif key in {"context", "context_window", "window"}:
            try:
                window = int(value)
            except ValueError:
                raise ValueError("context must be a whole number of tokens") from None
            if window == 0:
                result["context"] = None
            elif not 1024 <= window <= 2_000_000:
                raise ValueError("context must be from 1024 to 2000000 tokens")
            else:
                result["context"] = window
        else:
            raise ValueError(
                "generation keys are reasoning, max_tokens, temperature, effort, and context"
            )
    if not result["reasoning"]:
        result["effort"] = ""
    return result


def parse_model_support(modalities: str, generation: str) -> dict[str, Any]:
    settings = parse_generation(generation)
    settings.update(parse_modalities(modalities))
    return settings


def _load_settings(raw: Any) -> dict[str, Any] | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("stored model settings are invalid") from exc
    if not isinstance(data, dict):
        raise ValueError("stored model settings are invalid")  # noqa: TRY004
    return parse_model_support(format_modalities(data), format_generation(data))


def effective_settings(settings: dict[str, Any] | None) -> dict[str, Any]:
    if not settings:
        return dict(_LEGACY_SETTINGS)
    return dict(settings)


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
                    updated_at INTEGER NOT NULL,
                    base_url TEXT NOT NULL DEFAULT '',
                    settings TEXT NOT NULL DEFAULT ''
                )"""
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(credentials)")}
            if "base_url" not in columns:
                db.execute(
                    "ALTER TABLE credentials ADD COLUMN base_url TEXT NOT NULL DEFAULT ''"
                )
            if "settings" not in columns:
                db.execute(
                    "ALTER TABLE credentials ADD COLUMN settings TEXT NOT NULL DEFAULT ''"
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

    def save(
        self,
        user_id: Any,
        provider: str,
        model: str,
        api_key: str,
        *,
        base_url: str = "",
        settings: dict[str, Any] | None = None,
    ) -> None:
        owner = self._owner(user_id)
        selected_provider = self._provider(provider)
        selected_model = self._model(model)
        selected_url = validate_public_endpoint(
            base_url, required=not PROVIDERS[selected_provider]["base_url"]
        )
        stored_settings = (
            ""
            if settings is None
            else json.dumps(
                parse_model_support(
                    format_modalities(settings), format_generation(settings)
                ),
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        secret = self._api_key(api_key).encode("utf-8")
        key = self._require_key()
        nonce = secrets.token_bytes(12)
        ciphertext = AESGCM(key).encrypt(
            nonce, secret, self._aad(owner, selected_provider)
        )
        with self._db() as db:
            db.execute(
                """INSERT INTO credentials(
                       user_id, provider, model, nonce, ciphertext, updated_at,
                       base_url, settings
                   )
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET
                     provider=excluded.provider, model=excluded.model,
                     nonce=excluded.nonce, ciphertext=excluded.ciphertext,
                     updated_at=excluded.updated_at,
                     base_url=excluded.base_url, settings=excluded.settings""",
                (
                    owner,
                    selected_provider,
                    selected_model,
                    nonce,
                    ciphertext,
                    int(time.time()),
                    selected_url,
                    stored_settings,
                ),
            )
        self._secure_mode()

    def get(self, user_id: Any) -> dict[str, Any] | None:
        owner = self._owner(user_id)
        key = self._require_key()
        with self._db() as db:
            row = db.execute(
                """SELECT provider, model, base_url, settings, nonce, ciphertext
                   FROM credentials WHERE user_id=?""",
                (owner,),
            ).fetchone()
        if not row:
            return None
        provider, model, base_url, raw_settings, nonce, ciphertext = row
        if provider not in PROVIDERS:
            raise ValueError("stored provider is not supported")
        endpoint = validate_public_endpoint(
            str(base_url or ""), required=not PROVIDERS[provider]["base_url"]
        )
        try:
            secret = AESGCM(key).decrypt(
                bytes(nonce), bytes(ciphertext), self._aad(owner, provider)
            ).decode("utf-8")
        except (InvalidTag, UnicodeError) as exc:
            raise VaultUnavailable("stored BYOK credential could not be decrypted") from exc
        return {
            "provider": provider,
            "model": str(model),
            "api_key": secret,
            "base_url": endpoint,
            "settings": _load_settings(raw_settings),
        }

    def status(self, user_id: Any) -> dict[str, str] | None:
        credential = self.get(user_id)
        if credential is None:
            return None
        provider = credential["provider"]
        settings = effective_settings(credential.get("settings"))
        return {
            "provider": provider,
            "provider_label": PROVIDERS[provider]["label"],
            "model": credential["model"],
            "masked_key": "••••" + credential["api_key"][-4:],
            "base_url": effective_endpoint(provider, credential.get("base_url") or ""),
            "modalities": format_modalities(settings),
            "generation": format_generation(settings),
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


def make_request_provider(bot: Any, credential: dict[str, Any]):
    """Build an uncached, single-credential client for one authenticated user."""
    from dataclasses import replace

    from maxwell_core.providers.models import ProviderPolicy

    provider_id = credential["provider"]
    if provider_id not in PROVIDERS:
        raise ValueError("select a supported provider")
    base_url = effective_endpoint(provider_id, str(credential.get("base_url") or ""))
    if not base_url:
        raise ValueError("enter a public HTTPS endpoint")
    base_url = validate_public_endpoint(base_url, required=True)
    settings = effective_settings(credential.get("settings"))
    max_tokens = int(settings["max_tokens"])
    client = bot._make_chat_provider(
        name=f"byok:{provider_id}",
        base_url=base_url,
        model=credential["model"],
        max_tokens=max_tokens,
        temperature=float(settings["temperature"]),
        api_key=credential["api_key"],
        disable_reasoning=not bool(settings["reasoning"]),
        reasoning_effort=str(settings["effort"] or ""),
        enable_audio_input=bool(settings["audio"]),
        retry_attempts=1,
        empty_response_retries=0,
        policy=ProviderPolicy(
            sensitive_credentials=True, public_network_only=True,
            allow_provider_fallback=False, max_request_seconds=300,
            max_response_bytes=2 * 1024 * 1024, max_output_tokens=max_tokens,
        ),
    )
    client.capabilities = replace(
        client.capabilities,
        text=True,
        vision=bool(settings["vision"]),
        audio=bool(settings["audio"]),
        native_tools=bool(settings["tools"]),
        reasoning=bool(settings["reasoning"]),
        context_window=settings["context"],
    )
    client.enable_audio_input = bool(settings["audio"])
    client.available = True
    return client


__all__ = [
    "CredentialVault",
    "PROVIDERS",
    "VaultUnavailable",
    "effective_endpoint",
    "effective_settings",
    "format_generation",
    "format_modalities",
    "make_request_provider",
    "parse_model_support",
    "validate_public_endpoint",
]
