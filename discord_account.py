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
_USER_INSTALL_OAUTH_PARAMS = {
    "scopes": ["applications.commands"],
    "permissions": "0",
}


def user_install_integration_config(existing: dict | None) -> dict[str, Any]:
    """Guild config stays as-is; add USER_INSTALL if Discord does not have it."""
    config: dict[str, Any] = {}
    for key, value in (existing or {}).items():
        config[str(key)] = value
    if "0" not in config:
        config["0"] = {}
    current = config.get("1")
    params = current.get("oauth2_install_params") if isinstance(current, dict) else None
    if isinstance(params, dict) and "applications.commands" in (params.get("scopes") or []):
        return config
    config["1"] = {"oauth2_install_params": dict(_USER_INSTALL_OAUTH_PARAMS)}
    return config


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


async def ensure_user_install_context(token: str) -> bool:
    """Enable Discord USER_INSTALL so Add to my apps is not rejected.

    OAuth ``integration_type=1`` fails with "installation type not supported"
    unless the application has a user-install integration config.
    """
    import aiohttp

    token = _strip_bot_prefix(token)
    headers = {
        "Authorization": f"Bot {token}",
        "User-Agent": BOT_USER_AGENT,
        "Content-Type": "application/json",
    }
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.get(f"{_DISCORD_API}/applications/@me") as resp:
            if resp.status != 200:
                body = await resp.text()
                logger.warning(
                    "Could not read application install contexts: HTTP %s %s",
                    resp.status,
                    body[:200],
                )
                return False
            data = await resp.json()
        existing = data.get("integration_types_config") if isinstance(data, dict) else None
        desired = user_install_integration_config(
            existing if isinstance(existing, dict) else None
        )
        if existing == desired:
            return True
        async with session.patch(
            f"{_DISCORD_API}/applications/@me",
            json={"integration_types_config": desired},
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                logger.warning(
                    "Failed to enable user-install context: HTTP %s %s",
                    resp.status,
                    body[:200],
                )
                return False
            logger.info("Enabled Discord user-install (Add to my apps)")
            return True


async def sync_application_commands(
    token: str,
    commands: list[dict[str, Any]] | None = None,
    *,
    application_id: str | int | None = None,
    guild_ids: Iterable[int] | None = None,
) -> dict[str, int]:
    """PUT the global application-command set; wipe leftover guild commands."""
    import aiohttp

    token = _strip_bot_prefix(token)
    payload = list(commands or [])
    result = {"global": 0, "guild": 0}
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
            logger.warning("Could not resolve application id; slash commands not synced")
            return result
        async with session.put(
            f"{_DISCORD_API}/applications/{app_id}/commands", json=payload
        ) as resp:
            if resp.status not in {200, 201}:
                body = await resp.text()
                logger.warning(
                    "Failed to sync global slash commands: HTTP %s %s",
                    resp.status,
                    body[:200],
                )
            else:
                result["global"] = len(payload)
                logger.info("Synced %s global slash command(s)", result["global"])
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
                    result["guild"] += n
    return result


async def clear_application_commands(
    token: str,
    *,
    application_id: str | int | None = None,
    guild_ids: Iterable[int] | None = None,
) -> dict[str, int]:
    """Overwrite registered slash/app commands with an empty list."""
    return await sync_application_commands(
        token,
        [],
        application_id=application_id,
        guild_ids=guild_ids,
    )
