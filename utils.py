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

    2026-07-21: also fsync the parent directory after os.replace. On
    Linux, after a crash between os.replace and the next sync, the
    directory entry for `path` may not be persisted even though the
    inode is on disk. On reboot, the file is "gone" from the
    directory listing — load_from_disk quietly returns {}. That was
    a silent memory-wipe trigger. fsync'ing the parent dir closes
    the gap.
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
        _fsync_dir(path.parent)
    finally:
        if fd >= 0:
            with contextlib.suppress(OSError):
                os.close(fd)
        if os.path.exists(tmp):
            with contextlib.suppress(OSError):
                os.unlink(tmp)


def _atomic_text_write_sync(path: Path, text: str):
    """Atomic text write: temp file -> fsync -> rename.

    Same fd ownership handling as _atomic_json_write_sync. 2026-07-21:
    also fsync the parent directory (see _atomic_json_write_sync).
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
        _fsync_dir(path.parent)
    finally:
        if fd >= 0:
            with contextlib.suppress(OSError):
                os.close(fd)
        if os.path.exists(tmp):
            with contextlib.suppress(OSError):
                os.unlink(tmp)


def _fsync_dir(dir_path: Path) -> None:
    """fsync a directory. Best-effort; not all filesystems support it."""
    try:
        dfd = os.open(str(dir_path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dfd)
    except OSError:
        pass
    finally:
        with contextlib.suppress(OSError):
            os.close(dfd)


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


def render_discord_context_text(message: Any, content: str | None = None, known_users: dict | None = None) -> str:
    """Make Discord tokens readable for prompts/logged context without mutating the real message.
    known_users: optional {user_id: display_name} from conversation history to resolve pings.
    """
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
        if user is None and known_users and user_id in known_users:
            name = known_users[user_id]
            return f"@{name}({user_id})"
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


# --- Cross-process file locking (Linux fcntl; best-effort elsewhere) ---
# Used to reduce lost-update races on shared JSONs between bot and api processes
# (bot_commands.json, autonomy state, rem state, etc.). Not a full DB, but
# makes the existing read-modify-write pattern much safer.
try:
    import fcntl
except ImportError:
    fcntl = None  # type: ignore


class FileLockTimeout(TimeoutError):
    """Raised when an exclusive FileLock cannot be acquired within timeout."""


class FileLock:
    """Exclusive file lock using a *sidecar* lock file + fcntl.flock.

    The lock is held on ``{path}.lock``, never on the data file itself. That
    matters because callers use atomic ``os.replace`` on the data path — locking
    the data inode was broken (replace swaps the inode out from under flock).

    On timeout this raises ``FileLockTimeout`` (fail closed) instead of
    proceeding unlocked. Without fcntl it still serializes best-effort via the
    sidecar fd but cannot enforce cross-process exclusion.
    Usage:
        with FileLock(path):
            data = json.loads(path.read_text() or '[]')
            ... mutate ...
            _atomic_json_write_sync(path, data)
    """

    def __init__(self, path: Path | str, timeout: float = 30.0, *, fail_open: bool = False):
        self.path = Path(path)
        self.lock_path = Path(str(self.path) + ".lock")
        self.timeout = timeout
        self.fail_open = fail_open
        self._fd = None
        self._locked = False

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        if fcntl is not None:
            import time as _time
            deadline = _time.time() + self.timeout
            while True:
                try:
                    fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self._locked = True
                    break
                except BlockingIOError:
                    if _time.time() > deadline:
                        if self.fail_open:
                            logger.warning(
                                f"FileLock timeout on {self.lock_path}; proceeding without exclusive lock"
                            )
                            break
                        with contextlib.suppress(Exception):
                            os.close(self._fd)
                        self._fd = None
                        raise FileLockTimeout(
                            f"FileLock timeout after {self.timeout}s on {self.lock_path}"
                        )
                    _time.sleep(0.05)
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._fd is not None:
            if fcntl is not None and self._locked:
                with contextlib.suppress(Exception):
                    fcntl.flock(self._fd, fcntl.LOCK_UN)
            with contextlib.suppress(Exception):
                os.close(self._fd)
        self._fd = None
        self._locked = False
        return False


def _with_file_lock(path: Path | str, func, timeout: float = 30.0):
    """Helper to run func() while holding an exclusive lock on path.

    func receives no args and should do the read-modify-(atomic)write.
    """
    with FileLock(path, timeout=timeout):
        return func()
