"""Hosted-service Terms of Service gate.

The Maxwell software is MIT. These terms apply to the hosted instance
(the public Discord bot and maxwell.z3ki.dev). First directed interaction
must accept the current TOS/privacy version before Maxwell runs a turn.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from utils import FileLock, _atomic_json_write_sync, _load_json_safe

logger = logging.getLogger(__name__)

TOS_VERSION = "1"
CONSENT_FILE = "tos_consent.json"
OFFER_COOLDOWN_SECONDS = 300.0
CUSTOM_AGREE = "maxwell:tos:agree"
CUSTOM_DECLINE = "maxwell:tos:decline"
DEFAULT_PUBLIC_BASE = "https://maxwell.z3ki.dev"

_REASON = "tos_required"
AGREE_PHRASES = frozenset({"agree", "i agree", "i accept"})
DECLINE_PHRASES = frozenset({"no thanks", "disagree", "i decline"})


def legal_urls(bot: Any | None = None) -> tuple[str, str]:
    cfg = getattr(bot, "config", None) if bot is not None else None
    base = str(
        getattr(cfg, "MAXWELL_PUBLIC_BASE_URL", "") or DEFAULT_PUBLIC_BASE
    ).rstrip("/")
    if not base:
        base = DEFAULT_PUBLIC_BASE
    return f"{base}/terms/", f"{base}/privacy/"


def _path(bot: Any) -> Path:
    data_dir = getattr(getattr(bot, "config", None), "DATA_DIR", "data")
    return Path(data_dir) / CONSENT_FILE


def _blank() -> dict[str, Any]:
    return {"version": TOS_VERSION, "users": {}}


def _read(path: Path) -> dict[str, Any]:
    data = _load_json_safe(path, default=_blank)
    if not isinstance(data, dict):
        return _blank()
    users = data.get("users")
    if not isinstance(users, dict):
        users = {}
    return {"version": str(data.get("version") or TOS_VERSION), "users": users}


def load(bot: Any) -> None:
    path = _path(bot)
    try:
        data = _read(path) if path.exists() else _blank()
    except Exception:
        logger.exception("Failed to load TOS consent")
        data = _blank()
    users: dict[str, str] = {}
    raw = data.get("users") or {}
    if isinstance(raw, dict):
        for uid, row in raw.items():
            key = str(uid or "").strip()
            if not key:
                continue
            if isinstance(row, dict):
                users[key] = str(row.get("version") or "")
            elif row:
                users[key] = TOS_VERSION
    bot._tos_users = users
    if not isinstance(getattr(bot, "_tos_offered_at", None), dict):
        bot._tos_offered_at = {}
    logger.info("Loaded %d TOS consent record(s)", len(users))


def _save(bot: Any) -> None:
    path = _path(bot)
    users = getattr(bot, "_tos_users", None) or {}
    existing_users: dict[str, Any] = {}
    if path.exists():
        raw_users = _read(path).get("users") or {}
        if isinstance(raw_users, dict):
            existing_users = raw_users
    now = time.time()
    out: dict[str, dict[str, Any]] = {}
    for uid, ver in users.items():
        version = str(ver)
        prev = existing_users.get(uid)
        agreed_at = now
        if isinstance(prev, dict) and str(prev.get("version") or "") == version:
            try:
                agreed_at = float(prev.get("agreed_at") or now)
            except (TypeError, ValueError):
                agreed_at = now
        out[str(uid)] = {"version": version, "agreed_at": agreed_at}
    path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(path, timeout=10.0):
        _atomic_json_write_sync(path, {"version": TOS_VERSION, "users": out})


def has_agreed(bot: Any, user_id: Any) -> bool:
    uid = str(user_id or "").strip()
    if not uid:
        return False
    users = getattr(bot, "_tos_users", None) or {}
    return str(users.get(uid) or "") == TOS_VERSION


def record_agreement(bot: Any, user_id: Any) -> None:
    uid = str(user_id or "").strip()
    if not uid:
        return
    users = getattr(bot, "_tos_users", None)
    if not isinstance(users, dict):
        users = {}
        bot._tos_users = users
    users[uid] = TOS_VERSION
    try:
        _save(bot)
    except Exception:
        logger.exception("Failed to save TOS consent for %s", uid)


def needs_consent(bot: Any, user_id: Any) -> bool:
    uid = str(user_id or "").strip()
    if not uid:
        return False
    if not isinstance(getattr(bot, "_tos_users", None), dict):
        return False
    is_admin = getattr(bot, "_is_admin", None)
    if callable(is_admin) and is_admin(uid):
        return False
    return not has_agreed(bot, uid)


def directed_for_consent(bot: Any, message: Any) -> bool:
    """True when this inbound would be a first-use interaction, not room chatter."""
    if getattr(message, "user_install", False):
        return True
    channel = getattr(message, "channel", None)
    try:
        import discord

        if isinstance(channel, discord.DMChannel):
            return True
    except Exception:
        pass
    if getattr(message, "guild", None) is None and getattr(channel, "recipient", None):
        return True
    directly = getattr(bot, "_directly_addressed", None)
    if callable(directly) and directly(message):
        return True
    content = str(getattr(message, "content", "") or "")
    prefix = str(getattr(bot, "command_prefix", ",") or ",")
    return bool(prefix and content.startswith(prefix))


def _tos_embed(bot: Any):
    import discord

    terms, privacy = legal_urls(bot)
    embed = discord.Embed(
        title="Maxwell is in very early public testing",
        description=(
            "Before I can talk to you, you have to agree to the hosted-service "
            "Terms and Privacy Policy.\n\n"
            "This copy of Maxwell is **open source (MIT)** — you can run your "
            "own. These terms are for **this hosted bot**, which is unstable, "
            "may lose data, may grow usage limits, and may later cost a small "
            "subscription (around US$3, not a promise).\n\n"
            f"[Terms of Service]({terms})\n"
            f"[Privacy Policy]({privacy})\n\n"
            "Click **Agree**, or reply `agree`. Click **No thanks** (or reply "
            "`no thanks`) and I will ignore you."
        ),
        colour=0xE4E4E8,
    )
    embed.set_footer(text=f"TOS v{TOS_VERSION} · early public testing")
    return embed


def tos_view():
    try:
        import discord
        from discord.ui import Button, View
    except Exception:
        logger.info("discord.ui unavailable; TOS prompt will use text agree")
        return None
    view = View(timeout=None)
    view.add_item(
        Button(
            label="Agree",
            style=discord.ButtonStyle.success,
            custom_id=CUSTOM_AGREE,
        )
    )
    view.add_item(
        Button(
            label="No thanks",
            style=discord.ButtonStyle.secondary,
            custom_id=CUSTOM_DECLINE,
        )
    )
    return view


async def _send(destination: Any, **kwargs) -> None:
    send = getattr(destination, "send", None)
    if callable(send):
        await send(**kwargs)


async def offer(bot: Any, destination: Any) -> None:
    terms, privacy = legal_urls(bot)
    kwargs: dict[str, Any] = {
        "content": (
            "Maxwell is in very early public testing. "
            f"Read the Terms ({terms}) and Privacy Policy ({privacy}). "
            "Click Agree or reply `agree`."
        )
    }
    try:
        kwargs["embed"] = _tos_embed(bot)
    except Exception:
        logger.exception("TOS embed failed")
    try:
        view = tos_view()
    except Exception:
        logger.exception("TOS view failed")
        view = None
    if view is not None:
        kwargs["view"] = view
    await _send(destination, **kwargs)


def _normalized_reply(bot: Any, message: Any) -> str:
    text = str(getattr(message, "content", "") or "").strip().lower()
    prefix = str(getattr(bot, "command_prefix", ",") or ",")
    if prefix and text.startswith(prefix):
        text = text[len(prefix) :].strip()
    return text


async def gate_message(bot: Any, message: Any) -> str | None:
    """Block a turn until the author has agreed. None means continue."""
    author = getattr(message, "author", None)
    if author is None or getattr(author, "bot", False):
        return
    uid = getattr(author, "id", None)
    if not needs_consent(bot, uid):
        return
    channel = getattr(message, "channel", None)
    reply = _normalized_reply(bot, message)
    if reply in AGREE_PHRASES:
        record_agreement(bot, uid)
        try:
            await _send(
                channel,
                content=(
                    "You're in. Maxwell is still in very early public testing — "
                    "expect bugs, limits, and the occasional fire. Send your "
                    "message again."
                ),
            )
        except Exception:
            logger.exception("TOS agree ack failed for %s", uid)
        return _REASON
    if reply in DECLINE_PHRASES:
        try:
            await _send(
                channel,
                content=(
                    "Okay. I won't process your messages on this hosted instance. "
                    "The code stays open source if you want to run your own copy."
                ),
            )
        except Exception:
            logger.exception("TOS decline ack failed for %s", uid)
        return _REASON
    now = time.monotonic()
    offered = getattr(bot, "_tos_offered_at", None)
    if not isinstance(offered, dict):
        offered = {}
        bot._tos_offered_at = offered
    key = str(uid)
    last = offered.get(key)
    if last is not None and now - float(last) < OFFER_COOLDOWN_SECONDS:
        return _REASON
    offered[key] = now
    try:
        await offer(bot, channel)
    except Exception:
        logger.exception("Failed to send TOS prompt to %s", key)
        terms, privacy = legal_urls(bot)
        try:
            await _send(
                channel,
                content=(
                    "Maxwell is in very early public testing. Reply `agree` "
                    f"to the Terms ({terms}) and Privacy ({privacy}) before "
                    "I can talk to you."
                ),
            )
        except Exception:
            logger.exception("TOS text fallback failed for %s", key)
    return _REASON


async def handle_interaction(bot: Any, interaction: Any) -> bool:
    data = getattr(interaction, "data", None)
    if isinstance(data, dict):
        custom_id = str(data.get("custom_id") or "")
    else:
        custom_id = str(getattr(interaction, "custom_id", "") or "")
    if custom_id not in {CUSTOM_AGREE, CUSTOM_DECLINE}:
        return False
    user = getattr(interaction, "user", None) or getattr(interaction, "author", None)
    uid = getattr(user, "id", None)
    response = getattr(interaction, "response", None)
    send = getattr(response, "send_message", None) if response is not None else None
    if custom_id == CUSTOM_AGREE:
        record_agreement(bot, uid)
        text = (
            "You're in. Maxwell is still in very early public testing — expect "
            "bugs, limits, and the occasional fire. Send your message again."
        )
    else:
        text = (
            "Okay. I won't process your messages on this hosted instance. "
            "The code stays open source if you want to run your own copy."
        )
    if callable(send):
        try:
            await send(text, ephemeral=True)
        except Exception:
            logger.exception("TOS interaction reply failed")
    return True


def install(bot: Any) -> None:
    load(bot)
    add_view = getattr(bot, "add_view", None)
    view = tos_view()
    if (
        callable(add_view)
        and view is not None
        and not getattr(bot, "_tos_view_registered", False)
    ):
        try:
            add_view(view)
            bot._tos_view_registered = True
        except Exception:
            logger.exception("Failed to register TOS persistent view")


__all__ = [
    "TOS_VERSION",
    "CUSTOM_AGREE",
    "CUSTOM_DECLINE",
    "directed_for_consent",
    "gate_message",
    "handle_interaction",
    "has_agreed",
    "install",
    "legal_urls",
    "load",
    "needs_consent",
    "record_agreement",
]
