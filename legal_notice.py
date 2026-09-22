"""Send each Discord user a one-time, nonblocking legal notice by DM."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from utils import FileLock, _atomic_json_write_sync, _load_json_safe

logger = logging.getLogger(__name__)

NOTICE_FILE = "legal_notice.json"
LEGACY_CONSENT_FILE = "tos_consent.json"
DEFAULT_PUBLIC_BASE = "https://maxwell.z3ki.dev"


def legal_urls(bot: Any | None = None) -> tuple[str, str]:
    cfg = getattr(bot, "config", None) if bot is not None else None
    base = str(
        getattr(cfg, "MAXWELL_PUBLIC_BASE_URL", "") or DEFAULT_PUBLIC_BASE
    ).rstrip("/")
    return f"{base}/terms/", f"{base}/privacy/"


def _data_dir(bot: Any) -> Path:
    return Path(getattr(getattr(bot, "config", None), "DATA_DIR", "data"))


def _read_users(path: Path) -> dict[str, Any]:
    data = _load_json_safe(path, default=lambda: {"users": {}})
    users = data.get("users") if isinstance(data, dict) else None
    return users if isinstance(users, dict) else {}


def load(bot: Any) -> None:
    """Load sent notices and import old agreements without re-messaging users."""
    users: dict[str, float] = {}
    try:
        for uid, row in _read_users(_data_dir(bot) / LEGACY_CONSENT_FILE).items():
            if str(uid).strip() and row:
                users[str(uid)] = 0.0
        for uid, row in _read_users(_data_dir(bot) / NOTICE_FILE).items():
            if str(uid).strip() and row:
                try:
                    users[str(uid)] = (
                        float(row.get("sent_at", 0)) if isinstance(row, dict) else 0.0
                    )
                except (TypeError, ValueError):
                    users[str(uid)] = 0.0
    except Exception:
        logger.exception("Failed to load legal notice records")
    bot._legal_notice_users = users
    bot._legal_notice_lock = asyncio.Lock()
    logger.info("Loaded %d legal notice record(s)", len(users))


def has_notice(bot: Any, user_id: Any) -> bool:
    return str(user_id or "") in (getattr(bot, "_legal_notice_users", None) or {})


def _save(bot: Any) -> None:
    path = _data_dir(bot) / NOTICE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(path, timeout=10.0):
        existing = _read_users(path)
        for uid, sent_at in bot._legal_notice_users.items():
            existing[str(uid)] = {"sent_at": sent_at}
        _atomic_json_write_sync(path, {"users": existing})


async def notify_user(bot: Any, user: Any) -> None:
    """Try one DM; a failed delivery never prevents the user's action."""
    if user is None or getattr(user, "bot", False):
        return
    uid = str(getattr(user, "id", "") or "").strip()
    if not uid:
        return
    users = getattr(bot, "_legal_notice_users", None)
    if not isinstance(users, dict):
        return
    lock = getattr(bot, "_legal_notice_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        bot._legal_notice_lock = lock
    async with lock:
        if uid in users:
            return
        terms, privacy = legal_urls(bot)
        send = getattr(user, "send", None)
        try:
            if not callable(send):
                raise TypeError("Discord user has no DM send method")
            await send(
                "Hi! Here are Maxwell's [Terms of Service]"
                f"({terms}) and [Privacy Policy]({privacy}). "
                "You can keep using Maxwell as usual."
            )
        except Exception:
            logger.warning("Could not DM legal notice to user %s", uid, exc_info=True)
            return
        users[uid] = time.time()
        try:
            _save(bot)
        except Exception:
            logger.exception("Failed to save legal notice delivery for user %s", uid)


def directed_interaction(bot: Any, message: Any) -> bool:
    """True for DMs, mentions, commands, and user-install messages."""
    if getattr(message, "user_install", False):
        return True
    channel = getattr(message, "channel", None)
    try:
        import discord
        if isinstance(channel, discord.DMChannel):
            return True
    except ImportError:
        pass
    if getattr(message, "guild", None) is None and getattr(channel, "recipient", None):
        return True
    directly = getattr(bot, "_directly_addressed", None)
    if callable(directly) and directly(message):
        return True
    content = str(getattr(message, "content", "") or "")
    prefix = str(getattr(bot, "command_prefix", ",") or ",")
    return bool(prefix and content.startswith(prefix))


def install(bot: Any) -> None:
    load(bot)
