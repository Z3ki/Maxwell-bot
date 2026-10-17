"""Small per-user preference store used by Discord app commands."""

from __future__ import annotations

import copy
import json
import threading
import time

from privacy import ConsentStore, STORAGE_VERSION
from pathlib import Path
from typing import Any

from utils import FileLock, _atomic_json_write_sync

_LOCK = threading.RLock()
_DEFAULTS: dict[str, Any] = {
    "mode": "ask",
    "web": "auto",
    "detail": "quick",
    "context": 25,
    "language": "",
    "visibility": "public",
    # on: store this person's message content. off: do not store it.
    "tracking": "off",
}
_ALLOWED_DEFAULTS = frozenset(_DEFAULTS)
_PERSONALITY_LIMIT = 800


class UserPreferenceStore:
    """JSON-backed personal preferences, isolated by Discord user ID."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.consents = ConsentStore(self.path.parent / "privacy.sqlite3")
        self._cache: dict[str, Any] | None = None
        self._cache_signature: tuple[int, int, int, int] | None = None

    def _read(self, *, for_write: bool = False) -> dict[str, Any]:
        try:
            stat = self.path.stat()
            signature = (stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)
            if self._cache is not None and signature == self._cache_signature:
                return self._cache
            value = json.loads(self.path.read_text("utf-8"))
        except (OSError, TypeError, ValueError) as exc:
            self._cache = None
            self._cache_signature = None
            if for_write and not isinstance(exc, FileNotFoundError):
                raise ValueError(
                    "Cannot read preferences safely; refusing to overwrite the file"
                ) from exc
            return {"users": {}, "unavailable": not isinstance(exc, FileNotFoundError)}
        users = value.get("users", {}) if isinstance(value, dict) else None
        if not isinstance(users, dict):
            if for_write:
                raise ValueError(
                    "Invalid preferences format; refusing to overwrite the file"
                )
            return {"users": {}, "unavailable": True}
        self._cache = {"users": users}
        self._cache_signature = signature
        return self._cache

    def _write(self, root: dict[str, Any]) -> None:
        # A failed atomic replacement must never leave an unpersisted choice
        # in cache. Other instances detect successful replacements by stat.
        self._cache = None
        self._cache_signature = None
        _atomic_json_write_sync(self.path, root)

    def get(self, user_id: Any) -> dict[str, Any]:
        uid = str(user_id or "").strip()
        if not uid:
            return {"defaults": dict(_DEFAULTS), "personality": ""}
        with _LOCK:
            root = self._read()
            row = root["users"].get(uid)
            row = row if isinstance(row, dict) else {}
            defaults = dict(_DEFAULTS)
            if root.get("unavailable"):
                # Do not publish a reply, read channel context, or store
                # message content when an unreadable file prevents us from
                # honoring a saved opt-out.
                defaults.update(visibility="private", context=0, tracking="off")
            raw_defaults = row.get("defaults")
            if isinstance(raw_defaults, dict):
                defaults.update(
                    {
                        key: copy.deepcopy(value)
                        for key, value in raw_defaults.items()
                        if key in _ALLOWED_DEFAULTS
                    }
                )
            # Historical default-on is not proof of an affirmative storage choice.
            # Preserve every opt-out; require new evidence before retaining content.
            if (
                defaults.get("tracking") == "on"
                and row.get("storage_choice_version") != STORAGE_VERSION
            ):
                defaults["tracking"] = "off"
            personality = str(row.get("personality") or "")[:_PERSONALITY_LIMIT]
            return {"defaults": defaults, "personality": personality}

    def set_default(self, user_id: Any, key: str, value: Any) -> None:
        uid = str(user_id or "").strip()
        key = str(key or "").strip()
        if not uid or key not in _ALLOWED_DEFAULTS:
            raise ValueError("invalid user preference")
        with _LOCK, FileLock(self.path, timeout=5):
            root = copy.deepcopy(self._read(for_write=True))
            row = root["users"].get(uid)
            row = row if isinstance(row, dict) else {}
            defaults = row.get("defaults")
            if not isinstance(defaults, dict):
                defaults = {}
            if key == "tracking":
                if value not in {"on", "off"}:
                    raise ValueError("invalid storage choice")
                if value == "on":
                    self.consents.record(
                        uid,
                        "history",
                        STORAGE_VERSION,
                        "optional_storage",
                        affirmative=True,
                    )
                else:
                    self.consents.withdraw(uid, "history")
                    row["deleted_at"] = time.time()
                row["storage_choice_version"] = STORAGE_VERSION
                row["storage_choice_at"] = time.time()
            defaults[key] = value
            row["defaults"] = defaults
            root["users"][uid] = row
            self._write(root)

    def reset_default(self, user_id: Any, key: str) -> None:
        uid = str(user_id or "").strip()
        key = str(key or "").strip()
        if not uid or key not in _ALLOWED_DEFAULTS:
            raise ValueError("invalid user preference")
        if key == "tracking":
            # Reset never grants optional storage or erases a privacy opt-out.
            self.set_default(uid, "tracking", "off")
            return
        with _LOCK, FileLock(self.path, timeout=5):
            root = copy.deepcopy(self._read(for_write=True))
            row = root["users"].get(uid)
            if not isinstance(row, dict):
                return
            defaults = row.get("defaults")
            if isinstance(defaults, dict):
                defaults.pop(key, None)
                if defaults:
                    row["defaults"] = defaults
                else:
                    row.pop("defaults", None)
            if row:
                root["users"][uid] = row
            else:
                root["users"].pop(uid, None)
            self._write(root)

    def set_personality(self, user_id: Any, value: str | None) -> str:
        uid = str(user_id or "").strip()
        text = str(value or "").strip()
        if not uid:
            raise ValueError("user ID is required")
        if len(text) > _PERSONALITY_LIMIT:
            raise ValueError(
                f"personal style is limited to {_PERSONALITY_LIMIT} characters"
            )
        with _LOCK, FileLock(self.path, timeout=5):
            root = copy.deepcopy(self._read(for_write=True))
            row = root["users"].get(uid)
            row = row if isinstance(row, dict) else {}
            if text:
                row["personality"] = text
                root["users"][uid] = row
            else:
                row.pop("personality", None)
                if row:
                    root["users"][uid] = row
                else:
                    root["users"].pop(uid, None)
            self._write(root)
        return text

    def update(self, user_id: Any, defaults: dict, personality: str) -> None:
        """Save a dashboard form atomically, sharing the Discord writer lock."""
        uid = str(user_id or "").strip()
        if not uid.isdigit() or not defaults.keys() <= _ALLOWED_DEFAULTS:
            raise ValueError("invalid user preference")
        if not isinstance(personality, str) or len(personality) > _PERSONALITY_LIMIT:
            raise ValueError("personal style is limited to 800 characters")
        with _LOCK, FileLock(self.path, timeout=5):
            root = copy.deepcopy(self._read(for_write=True))
            row = root["users"].get(uid)
            row = dict(row) if isinstance(row, dict) else {}
            saved = row.get("defaults")
            saved = dict(saved) if isinstance(saved, dict) else {}
            if "tracking" in defaults:
                choice = defaults["tracking"]
                if choice not in {"on", "off"}:
                    raise ValueError("invalid storage choice")
                if choice == "on":
                    self.consents.record(
                        uid,
                        "history",
                        STORAGE_VERSION,
                        "optional_storage",
                        affirmative=True,
                    )
                else:
                    self.consents.withdraw(uid, "history")
                    row["deleted_at"] = time.time()
                row["storage_choice_version"] = STORAGE_VERSION
                row["storage_choice_at"] = time.time()
            saved.update(defaults)
            row.update(defaults=saved, personality=personality.strip())
            root["users"][uid] = row
            self._write(root)

    def delete_personal(self, user_id):
        uid = str(user_id)
        with _LOCK, FileLock(self.path, timeout=5):
            root = copy.deepcopy(self._read(for_write=True))
            root["users"][uid] = {
                "defaults": {"tracking": "off"},
                "deleted_at": time.time(),
            }
            self._write(root)

    def request_allowed(self, user_id, started_at):
        with _LOCK:
            root = self._read()
            if root.get("unavailable"):
                return False
            row = root["users"].get(str(user_id), {})
            return float(started_at) > float(row.get("deleted_at") or 0)

    def content_allowed(self, user_id, started_at=None):
        uid = str(user_id)
        if self.get(uid)["defaults"]["tracking"] != "on":
            return False
        with _LOCK:
            row = self._read()["users"].get(uid, {})
            return not (
                started_at and float(started_at) <= float(row.get("deleted_at") or 0)
            )


def tracks_message_content(store: Any, user_id: Any) -> bool:
    """False when this person opted out, or their choice cannot be read.

    A missing or unreadable store fails closed for personal content.
    System and bot ids are not personal opt-outs.
    """
    uid = str(user_id or "").strip()
    if not uid.isdigit():
        return True
    if store is None:
        return False
    try:
        row = store.get(uid)
    except Exception:
        return False
    defaults = row.get("defaults") if isinstance(row, dict) else None
    if not isinstance(defaults, dict):
        return False
    return str(defaults.get("tracking", "off")) == "on"


def bind_message_tracking(bot: Any, store: Any) -> None:
    """Attach the opt-out check to the live history store, if it has one."""
    memory = getattr(bot, "memory", None)
    if memory is None:
        return

    def allowed(user_id: str, _store: Any = store) -> bool:
        return tracks_message_content(_store, user_id)

    memory.preferences = store
    memory.tracks_message_content = allowed


__all__ = ["UserPreferenceStore", "bind_message_tracking", "tracks_message_content"]
