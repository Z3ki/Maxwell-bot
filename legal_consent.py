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


def _tos_embed(bot: Any, *, state: str = "offer"):
    import discord

    terms, privacy = legal_urls(bot)
    if state == "accepted":
        embed = discord.Embed(
            title="You're in — hosted Maxwell is free",
            description=(
                "**Free during Public Alpha.** No card, no sub. Expect bugs, "
                "downtime, lost memory, and usage limits if this gets expensive. "
                "A small paid plan (around US$3) might show up later — not a "
                "promise. Self-host stays MIT-free forever.\n\n"
                "Send your message again."
            ),
            colour=0x3BA55D,
        )
        embed.set_footer(text=f"TOS v{TOS_VERSION} · Public Alpha · free")
        return embed
    if state == "declined":
        embed = discord.Embed(
            title="Okay, sitting this out",
            description=(
                "I won't process your messages on this hosted instance. "
                "The code stays open source if you want to run your own copy."
            ),
            colour=0x4A4A56,
        )
        embed.set_footer(text=f"TOS v{TOS_VERSION} · Public Alpha")
        return embed
    embed = discord.Embed(
        title="Maxwell is free right now",
        description=(
            "Public Alpha. Agree to the hosted Terms and Privacy before I can "
            "talk to you.\n\n"
            "**It's free.** No payment, no extra account. Usage limits may "
            "appear if the hosted copy gets expensive. A small subscription "
            "(around US$3) might come later — not a promise, not a price. "
            "Self-host is always free (MIT).\n\n"
            f"[Terms of Service]({terms})\n"
            f"[Privacy Policy]({privacy})\n\n"
            "Click **Agree**, or reply `agree`. **No thanks** and I ignore you."
        ),
        colour=0xE4E4E8,
    )
    embed.set_footer(text=f"TOS v{TOS_VERSION} · Public Alpha · free")
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
            label="Agree — it's free",
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


def _remember_prompt(bot: Any, user_id: Any, sent: Any) -> None:
    if sent is None or user_id is None:
        return
    prompts = getattr(bot, "_tos_prompts", None)
    if not isinstance(prompts, dict):
        prompts = {}
        bot._tos_prompts = prompts
    prompts[str(user_id)] = sent


def _forget_prompt(bot: Any, user_id: Any) -> Any:
    prompts = getattr(bot, "_tos_prompts", None)
    if not isinstance(prompts, dict):
        return None
    return prompts.pop(str(user_id), None)


def _result_copy(*, accepted: bool) -> tuple[str, str]:
    if accepted:
        return (
            "You're in. Hosted Maxwell is **free** during Public Alpha — "
            "send your message again.",
            "accepted",
        )
    return (
        "Okay. I won't process your messages on this hosted instance.",
        "declined",
    )


async def _edit_prompt(target: Any, *, content: str, embed: Any = None) -> bool:
    if target is None:
        return False
    edit = getattr(target, "edit", None)
    if not callable(edit):
        return False
    payload: dict[str, Any] = {"content": content, "view": None}
    if embed is not None:
        payload["embed"] = embed
    try:
        await edit(**payload)
        return True
    except TypeError:
        payload.pop("view", None)
        try:
            await edit(**payload)
            return True
        except Exception:
            logger.exception("TOS prompt edit failed")
            return False
    except Exception:
        logger.exception("TOS prompt edit failed")
        return False


async def _settle_prompt(
    bot: Any,
    uid: Any,
    *,
    accepted: bool,
    interaction: Any = None,
    fallback_destination: Any = None,
    reply_to: Any = None,
) -> None:
    content, state = _result_copy(accepted=accepted)
    embed = None
    try:
        embed = _tos_embed(bot, state=state)
    except Exception:
        logger.exception("TOS result embed failed")
    prompt = _forget_prompt(bot, uid)
    updated = False
    response = getattr(interaction, "response", None) if interaction is not None else None
    edit_message = getattr(response, "edit_message", None) if response is not None else None
    if callable(edit_message):
        payload: dict[str, Any] = {"content": content, "view": None}
        if embed is not None:
            payload["embed"] = embed
        try:
            await edit_message(**payload)
            updated = True
        except TypeError:
            payload.pop("view", None)
            try:
                await edit_message(**payload)
                updated = True
            except Exception:
                logger.exception("TOS interaction edit failed")
        except Exception:
            logger.exception("TOS interaction edit failed")
    if not updated:
        target = getattr(interaction, "message", None) if interaction is not None else None
        updated = await _edit_prompt(target or prompt, content=content, embed=embed)
    if updated:
        return
    send = getattr(response, "send_message", None) if response is not None else None
    if callable(send):
        try:
            await send(content, ephemeral=True)
            return
        except Exception:
            logger.exception("TOS interaction fallback send failed")
    if fallback_destination is not None:
        try:
            kwargs: dict[str, Any] = {"content": content}
            if embed is not None:
                kwargs["embed"] = embed
            await _send(fallback_destination, reply_to=reply_to, **kwargs)
        except Exception:
            logger.exception("TOS settle fallback failed for %s", uid)


async def _send(destination: Any, *, reply_to: Any = None, **kwargs) -> Any:
    if reply_to is not None:
        reply = getattr(reply_to, "reply", None)
        if callable(reply):
            try:
                return await reply(**kwargs)
            except Exception:
                logger.exception("TOS reply failed; falling back to channel send")
    send = getattr(destination, "send", None)
    if callable(send):
        if reply_to is not None:
            kwargs.setdefault("reference", reply_to)
            kwargs.setdefault("mention_author", True)
        try:
            return await send(**kwargs)
        except Exception:
            kwargs.pop("reference", None)
            kwargs.pop("mention_author", None)
            return await send(**kwargs)
    return None


async def offer(
    bot: Any,
    destination: Any,
    *,
    reply_to: Any = None,
    user_id: Any = None,
) -> None:
    terms, privacy = legal_urls(bot)
    kwargs: dict[str, Any] = {
        "content": (
            "Hosted Maxwell is **free** during Public Alpha. "
            f"Read the Terms ({terms}) and Privacy Policy ({privacy}), "
            "then click Agree — or reply `agree`."
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
    sent = await _send(destination, reply_to=reply_to, **kwargs)
    uid = user_id
    if uid is None and reply_to is not None:
        uid = getattr(getattr(reply_to, "author", None), "id", None)
    _remember_prompt(bot, uid, sent)


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
        await _settle_prompt(
            bot, uid, accepted=True, fallback_destination=channel, reply_to=message
        )
        return _REASON
    if reply in DECLINE_PHRASES:
        await _settle_prompt(
            bot, uid, accepted=False, fallback_destination=channel, reply_to=message
        )
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
        await offer(bot, channel, reply_to=message, user_id=uid)
    except Exception:
        logger.exception("Failed to send TOS prompt to %s", key)
        terms, privacy = legal_urls(bot)
        try:
            sent = await _send(
                channel,
                reply_to=message,
                content=(
                    "Hosted Maxwell is **free** during Public Alpha. Reply "
                    f"`agree` to the Terms ({terms}) and Privacy ({privacy}) "
                    "before I can talk to you."
                ),
            )
            _remember_prompt(bot, uid, sent)
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
    accepted = custom_id == CUSTOM_AGREE
    if accepted:
        record_agreement(bot, uid)
    await _settle_prompt(bot, uid, accepted=accepted, interaction=interaction)
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
