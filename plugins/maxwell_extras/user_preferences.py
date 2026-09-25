"""Small per-user preference store used by Discord app commands."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from utils import _atomic_json_write_sync

_LOCK = threading.RLock()
_DEFAULTS: dict[str, Any] = {
    "mode": "ask",
    "web": "auto",
    "detail": "balanced",
    "context": 25,
    "language": "",
}
_ALLOWED_DEFAULTS = frozenset(_DEFAULTS)
_PERSONALITY_LIMIT = 800


class UserPreferenceStore:
    """JSON-backed personal preferences, isolated by Discord user ID."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _read(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text("utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return {"users": {}}
        users = value.get("users") if isinstance(value, dict) else None
        return {"users": users} if isinstance(users, dict) else {"users": {}}

    def get(self, user_id: Any) -> dict[str, Any]:
        uid = str(user_id or "").strip()
        if not uid:
            return {"defaults": dict(_DEFAULTS), "personality": ""}
        with _LOCK:
            root = self._read()
            row = root["users"].get(uid)
            row = row if isinstance(row, dict) else {}
            defaults = dict(_DEFAULTS)
            raw_defaults = row.get("defaults")
            if isinstance(raw_defaults, dict):
                defaults.update(
                    {
                        key: value
                        for key, value in raw_defaults.items()
                        if key in _ALLOWED_DEFAULTS
                    }
                )
            personality = str(row.get("personality") or "")[:_PERSONALITY_LIMIT]
            return {"defaults": defaults, "personality": personality}

    def set_default(self, user_id: Any, key: str, value: Any) -> None:
        uid = str(user_id or "").strip()
        key = str(key or "").strip()
        if not uid or key not in _ALLOWED_DEFAULTS:
            raise ValueError("invalid user preference")
        with _LOCK:
            root = self._read()
            row = root["users"].get(uid)
            row = row if isinstance(row, dict) else {}
            defaults = row.get("defaults")
            if not isinstance(defaults, dict):
                defaults = {}
            defaults[key] = value
            row["defaults"] = defaults
            root["users"][uid] = row
            _atomic_json_write_sync(self.path, root)

    def reset_default(self, user_id: Any, key: str) -> None:
        uid = str(user_id or "").strip()
        key = str(key or "").strip()
        if not uid or key not in _ALLOWED_DEFAULTS:
            raise ValueError("invalid user preference")
        with _LOCK:
            root = self._read()
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
            _atomic_json_write_sync(self.path, root)

    def set_personality(self, user_id: Any, value: str | None) -> str:
        uid = str(user_id or "").strip()
        text = str(value or "").strip()
        if not uid:
            raise ValueError("user ID is required")
        if len(text) > _PERSONALITY_LIMIT:
            raise ValueError(f"personal style is limited to {_PERSONALITY_LIMIT} characters")
        with _LOCK:
            root = self._read()
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
            _atomic_json_write_sync(self.path, root)
        return text


__all__ = ["UserPreferenceStore"]
