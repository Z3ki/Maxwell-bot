"""Official Discord bot transport.

Maxwell logs in with a bot token from the Discord Developer Portal.
Self-bot / user-token support is gone: no invite-join, no captcha solving,
no dual user+bot connections.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

logger = logging.getLogger(__name__)

BOT_USER_AGENT = "DiscordBot (https://github.com/Z3ki/Maxwell-bot, 1.0)"
_DISCORD_API = "https://discord.com/api/v10"


def _strip_bot_prefix(token: str) -> str:
    text = (token or "").strip()
    if text.lower().startswith("bot "):
        return text[4:].strip()
    return text


def bot_intents():
    """Privileged + standard intents an official Maxwell bot needs.

    Enable Message Content, Server Members, and Presence in the Developer
    Portal or IDENTIFY will close the gateway. Returns None only if this
    discord.py build has no Intents type (should not happen on official 2.x).
    """
    import discord

    intents_cls = getattr(discord, "Intents", None)
    if intents_cls is None:
        return None
    try:
        intents = intents_cls.default()
    except Exception:
        try:
            intents = intents_cls.all()
        except Exception:
            return None
    for name in (
        "message_content",
        "members",
        "presences",
        "moderation",
        "voice_states",
        "guilds",
        "guild_messages",
        "dm_messages",
        "guild_reactions",
        "dm_reactions",
        "typing",
        "emojis_and_stickers",
        "guild_scheduled_events",
        "auto_moderation_configuration",
        "auto_moderation_execution",
    ):
        if hasattr(intents, name):
            try:
                setattr(intents, name, True)
            except Exception:
                pass
    return intents


def account_ids(bot: Any) -> set[int]:
    """Discord snowflakes that are this Maxwell process."""
    ids: set[int] = set()
    stored = getattr(bot, "_account_ids", None)
    if stored:
        for x in stored:
            if x is not None:
                try:
                    ids.add(int(x))
                except (TypeError, ValueError):
                    pass
    user = getattr(bot, "user", None)
    uid = getattr(user, "id", None)
    if uid is not None:
        try:
            ids.add(int(uid))
        except (TypeError, ValueError):
            pass
    return ids


def configured_bot_token(bot_token: str | None, legacy_token: str | None) -> str:
    """Prefer DISCORD_BOT_TOKEN; accept a leftover DISCORD_TOKEN as alias."""
    primary = _strip_bot_prefix(bot_token or "")
    if primary:
        return primary
    return _strip_bot_prefix(legacy_token or "")


async def clear_application_commands(
    token: str,
    *,
    application_id: str | int | None = None,
    guild_ids: Iterable[int] | None = None,
) -> dict[str, int]:
    """Overwrite registered slash/app commands with an empty list.

    Leftover commands (from another bot on the same application) stay on
    Discord until something PUTs a new set. Maxwell does not use slash
    commands, so startup wipes them.
    """
    import aiohttp

    token = _strip_bot_prefix(token)
    removed = {"global": 0, "guild": 0}
    headers = {
        "Authorization": f"Bot {token}",
        "User-Agent": BOT_USER_AGENT,
        "Content-Type": "application/json",
    }
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        app_id = str(application_id or "").strip()
        if not app_id:
            async with session.get(f"{_DISCORD_API}/oauth2/applications/@me") as resp:
                data = await resp.json() if resp.status == 200 else {}
                app_id = str((data or {}).get("id") or "")
        if not app_id:
            logger.warning("Could not resolve application id; slash commands not cleared")
            return removed
        async with session.get(f"{_DISCORD_API}/applications/{app_id}/commands") as resp:
            current = await resp.json() if resp.status == 200 else []
        removed["global"] = len(current) if isinstance(current, list) else 0
        async with session.put(
            f"{_DISCORD_API}/applications/{app_id}/commands", json=[]
        ) as resp:
            if resp.status not in {200, 201}:
                body = await resp.text()
                logger.warning(
                    "Failed to clear global slash commands: HTTP %s %s",
                    resp.status,
                    body[:200],
                )
            else:
                logger.info("Cleared %s global slash command(s)", removed["global"])
        for gid in guild_ids or ():
            async with session.get(
                f"{_DISCORD_API}/applications/{app_id}/guilds/{gid}/commands"
            ) as resp:
                current = await resp.json() if resp.status == 200 else []
            n = len(current) if isinstance(current, list) else 0
            async with session.put(
                f"{_DISCORD_API}/applications/{app_id}/guilds/{gid}/commands",
                json=[],
            ) as resp:
                if resp.status in {200, 201}:
                    removed["guild"] += n
    return removed
