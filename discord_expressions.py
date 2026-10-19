"""Live, scoped Discord expression catalogs and channel send permissions."""

from __future__ import annotations

import asyncio
import logging
import re

import discord

logger = logging.getLogger(__name__)
REFRESH_SECONDS = 300
EMOJI_MARKUP_RE = re.compile(r"<a?:([A-Za-z0-9_]{2,32}):(\d+)>")


def custom_emoji_id(value) -> str | None:
    """Discord identifies custom reactions by ID, not name or animation flag."""
    raw = str(value or "").strip()
    match = EMOJI_MARKUP_RE.fullmatch(raw) or re.fullmatch(
        r"([A-Za-z0-9_]{2,32}):(\d+)", raw
    )
    if match:
        return match.group(2)
    if raw.isascii() and raw.isdecimal():
        return raw
    return None


def social_result_is_terminal(result) -> bool:
    text = str(result or "").strip()
    return text.startswith(
        (
            "Tool react: __REACTION_SENT__",
            "Tool send_gif: __GIF_SENT__",
        )
    )


def channel_permissions(message):
    channel = getattr(message, "channel", None)
    guild = getattr(message, "guild", None)
    member = getattr(guild, "me", None)
    getter = getattr(channel, "permissions_for", None)
    if member is None or not callable(getter):
        return None
    return getter(member)


def mark_send_unavailable(bot, message):
    if bot is None:
        return
    ids = getattr(bot, "_send_unavailable_requests", None)
    if ids is None:
        ids = bot._send_unavailable_requests = set()
    if len(ids) >= 1000:
        ids.clear()
    ids.add(str(getattr(message, "id", "")))


def cannot_send(message, bot=None) -> bool:
    # Interactions use a webhook, including private replies in restricted rooms.
    if (
        getattr(message, "user_install", False)
        or getattr(getattr(message, "channel", None), "_session", None) is not None
    ):
        return False
    if str(getattr(message, "id", "")) in (
        getattr(bot, "_send_unavailable_requests", None) or ()
    ):
        return True
    perms = channel_permissions(message)
    channel = getattr(message, "channel", None)
    thread = isinstance(channel, discord.Thread) or getattr(channel, "type", None) in {
        discord.ChannelType.public_thread,
        discord.ChannelType.private_thread,
        discord.ChannelType.news_thread,
    }
    send = "send_messages_in_threads" if thread else "send_messages"
    return any(getattr(perms, name, None) is False for name in ("view_channel", send))


def usable(emoji) -> bool:
    if getattr(emoji, "available", True) is False:
        return False
    check = getattr(emoji, "is_usable", None)
    return not callable(check) or check()


def sync_guild(bot, guild, *, emojis=None, stickers=None):
    gid = str(guild.id)
    bot._guild_emojis[gid] = {
        emoji.name.lower(): str(emoji)
        for emoji in (getattr(guild, "emojis", ()) if emojis is None else emojis)
        if usable(emoji)
    }
    bot._guild_stickers[gid] = {
        sticker.name.lower(): sticker.name
        for sticker in (
            getattr(guild, "stickers", ()) if stickers is None else stickers
        )
        if getattr(sticker, "available", True)
    }


def available_emojis(bot, guild) -> dict[str, str]:
    # Application-owned emojis belong to this bot and work across its rooms.
    # Other servers' emojis never enter this catalog. Local names take priority.
    result = dict(getattr(bot, "_application_emojis", {}) or {})
    if guild is not None:
        result.update(
            {e.name.lower(): str(e) for e in getattr(guild, "emojis", ()) if usable(e)}
        )
    return result


def available_emoji_codes(bot, guild) -> dict[str, str]:
    # Keep both assets when an application and guild emoji share a name.
    codes = list((getattr(bot, "_application_emojis", {}) or {}).values())
    if guild is not None:
        codes.extend(str(e) for e in getattr(guild, "emojis", ()) if usable(e))
    return {eid: code for code in codes if (eid := custom_emoji_id(code)) is not None}


def resolve_custom_emoji(bot, guild, value) -> str | None:
    raw = str(value or "").strip()
    if eid := custom_emoji_id(raw):
        return available_emoji_codes(bot, guild).get(eid)
    return available_emojis(bot, guild).get(raw.strip(":").lower())


def emoji_image_url(emoji_id: str) -> str:
    # Discord can serve every upload format as WebP. GIF conversion can fail
    # for animated WebP/AVIF uploads; a still frame is enough for the grid.
    return f"https://cdn.discordapp.com/emojis/{emoji_id}.webp"


def available_stickers(bot, guild) -> dict[str, object]:
    result = dict(getattr(bot, "_standard_stickers", {}) or {})
    if guild is not None:
        result.update(
            {
                s.name.lower(): s
                for s in getattr(guild, "stickers", ())
                if getattr(s, "available", True)
            }
        )
    return result


def extract_stickers(bot, text: str, guild) -> tuple[str, list]:
    # Explicit markers only. Bare brackets such as [Help] or [100] stay text
    # even when an official sticker pack uses that word.
    catalog = available_stickers(bot, guild)
    found = []

    def replace(match):
        name = (
            next(group for group in match.groups() if group is not None).strip().lower()
        )
        sticker = catalog.get(name)
        if sticker is None and name.isascii() and name.isdecimal():
            sticker = next(
                (s for s in catalog.values() if str(getattr(s, "id", "")) == name),
                None,
            )
        if sticker is None or len(found) >= 3:
            return match.group(0)
        if sticker not in found:
            found.append(sticker)
        return ""

    cleaned = re.sub(
        r"\[STICKER\s*(?:\(\s*([^)]*?)\s*\)|:\s*([^\]]+)|\s+([^\]]+))\]",
        replace,
        text or "",
        flags=re.IGNORECASE,
    )
    return cleaned.strip(), found


async def refresh_global_expressions(bot):
    # Independent endpoints: keep the last good snapshot on transient failures.
    for method, attribute in (
        ("fetch_application_emojis", "_application_emojis"),
        ("fetch_premium_sticker_packs", "_standard_stickers"),
    ):
        fetch = getattr(bot, method, None)
        if not callable(fetch):
            continue
        try:
            # asyncio.timeout instead of wait_for: the 3.11 wait_for has a
            # cancellation race that can leave the cancelled refresh task
            # running forever, hanging anything that awaits the task (like a
            # shutdown or this module's own cancellable-task contract).
            async with asyncio.timeout(20):
                data = await fetch()
            if attribute == "_application_emojis":
                bot._application_emojis = {
                    e.name.lower(): str(e)
                    for e in data
                    if getattr(e, "available", True)
                }
            else:
                catalog = {}
                for pack in data:
                    pack_name = str(getattr(pack, "name", "") or "").lower()
                    for sticker in pack.stickers:
                        if not getattr(sticker, "available", True):
                            continue
                        name = sticker.name.lower()
                        catalog.setdefault(name, sticker)
                        if pack_name:
                            catalog[f"{pack_name}/{name}"] = sticker
                bot._standard_stickers = catalog
        except (
            discord.HTTPException,
            asyncio.TimeoutError,
            discord.MissingApplicationID,
        ) as exc:
            logger.warning(
                "Expression refresh %s failed: %s", method, type(exc).__name__
            )


def start_expression_refresh(bot):
    task = getattr(bot, "_expression_refresh_task", None)
    if task is not None and not task.done():
        return

    async def refresh_loop():
        while not bot.is_closed():
            try:
                await refresh_global_expressions(bot)
            except asyncio.CancelledError:
                raise
            except Exception:
                # refresh_global_expressions only catches a narrow tuple; a
                # transient OSError escaping here would end the task, and
                # expressions would never refresh again for the process.
                logger.warning("expression refresh loop failed", exc_info=True)
            await asyncio.sleep(REFRESH_SECONDS)

    bot._expression_refresh_task = bot._track_task(asyncio.create_task(refresh_loop()))
