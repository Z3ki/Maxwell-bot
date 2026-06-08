"""Shared utility functions used across Maxwell modules.

Don't duplicate these in other files. Import from here.
"""

import contextlib
import json
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Discord mention regexes — single source of truth
USER_MENTION_RE = re.compile(r"<@!?(\d+)>")
CHANNEL_MENTION_RE = re.compile(r"<#(\d+)>")
ROLE_MENTION_RE = re.compile(r"<@&(\d+)>")


def _atomic_json_write_sync(path: Path, data):
    """Atomic JSON write: temp file -> fsync -> rename.

    Correctly handles fd ownership: os.fdopen takes ownership of the fd,
    so we set fd = -1 afterward to prevent double-close in the finally block.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fd = -1  # fdopen took ownership — don't double-close
            json.dump(data, f, indent=2, ensure_ascii=False, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if fd >= 0:
            with contextlib.suppress(OSError):
                os.close(fd)
        if os.path.exists(tmp):
            os.unlink(tmp)


def _atomic_text_write_sync(path: Path, text: str):
    """Atomic text write: temp file -> fsync -> rename.

    Same fd ownership handling as _atomic_json_write_sync.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fd = -1  # fdopen took ownership — don't double-close
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if fd >= 0:
            with contextlib.suppress(OSError):
                os.close(fd)
        if os.path.exists(tmp):
            os.unlink(tmp)


def _coerce_utc_datetime(value) -> datetime | None:
    """Normalize any datetime-like value to UTC datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _discord_display_name(obj: Any) -> str:
    """Get display name from a Discord user/member object."""
    return str(
        getattr(obj, "display_name", None)
        or getattr(obj, "name", None)
        or getattr(obj, "id", "unknown")
    )


def _discord_id(obj: Any) -> str:
    """Get string ID from a Discord object."""
    return str(getattr(obj, "id", "unknown"))


def render_discord_context_text(message: Any, content: str | None = None) -> str:
    """Make Discord tokens readable for prompts/logged context without mutating the real message."""
    text = str(
        content if content is not None else (getattr(message, "content", "") or "")
    )
    if not text:
        return text

    guild = getattr(message, "guild", None)
    users = {
        _discord_id(user): user for user in list(getattr(message, "mentions", []) or [])
    }
    channels = {
        _discord_id(ch): ch
        for ch in list(getattr(message, "channel_mentions", []) or [])
    }
    roles = {
        _discord_id(role): role
        for role in list(getattr(message, "role_mentions", []) or [])
    }

    def replace_user(match: re.Match) -> str:
        user_id = match.group(1)
        user = users.get(user_id)
        if user is None and guild is not None:
            user = guild.get_member(int(user_id))
        if user is None:
            return f"@unknown-user({user_id})"
        return f"@{_discord_display_name(user)}({user_id})"

    def replace_channel(match: re.Match) -> str:
        channel_id = match.group(1)
        channel = channels.get(channel_id)
        if channel is None and guild is not None:
            channel = guild.get_channel(int(channel_id))
        if channel is None:
            return f"#unknown-channel({channel_id})"
        return f"#{getattr(channel, 'name', channel_id)}({channel_id})"

    def replace_role(match: re.Match) -> str:
        role_id = match.group(1)
        role = roles.get(role_id)
        if role is None and guild is not None:
            role = guild.get_role(int(role_id))
        if role is None:
            return f"@unknown-role({role_id})"
        return f"@{getattr(role, 'name', role_id)}({role_id})"

    text = USER_MENTION_RE.sub(replace_user, text)
    text = CHANNEL_MENTION_RE.sub(replace_channel, text)
    text = ROLE_MENTION_RE.sub(replace_role, text)
    return text


# Alias for autonomy.py compatibility
_render_discord_context_text = render_discord_context_text
