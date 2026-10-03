"""Resolve Discord media without confusing attachments with message IDs.

Signed CDN links can be stale while the underlying message still exists.
Refresh only through the request's source channel; a bot's access to some
other private channel is not authorization for this requester to read it.
"""

from __future__ import annotations

import asyncio
import re
from urllib.parse import unquote, urlparse

_CDN_HOSTS = {"cdn.discordapp.com", "media.discordapp.net"}
_MESSAGE_HOSTS = {
    "discord.com",
    "ptb.discord.com",
    "canary.discord.com",
    "discordapp.com",
}
_ATTACHMENT_PATH = re.compile(r"^/(?:ephemeral-)?attachments/(\d+)/(\d+)/(.+)$")
_MESSAGE_PATH = re.compile(r"^/channels/(\d+|@me)/(\d+)/(\d+)/?$")
REFRESH_TIMEOUT = 4.0
REFRESH_HISTORY_LIMIT = 25


def clean_media_url(value: str) -> str:
    """Remove chat wrappers without dropping or decoding signed parameters."""
    url = str(value or "").strip().strip("<>`\"'")
    for wrapper in ("**", "__", "~~"):
        if url.startswith(wrapper) and url.endswith(wrapper):
            url = url[len(wrapper) : -len(wrapper)]
    # Decode only ampersand entities; unescape() on an entire query can turn
    # legitimate parameters such as &not= into a different string.
    url = re.sub(r"&(?:amp|#0*38|#x0*26);", "&", url, flags=re.I)
    try:
        if (urlparse(url).hostname or "").lower() in _CDN_HOSTS:
            url = url.rstrip("`*.,;!\"'")
    except ValueError:
        pass
    return url


def media_urls_in_text(content: str) -> list[str]:
    urls = []
    for raw in re.findall(r"https?://[^\s<>`\"']+", str(content or "")):
        url = clean_media_url(raw.rstrip(".,;!?)"))
        if url not in urls:
            urls.append(url)
    return urls


def discord_attachment_key(url: str) -> tuple[str, str, str] | None:
    try:
        parsed = urlparse(clean_media_url(url))
        valid = (
            parsed.scheme == "https"
            and parsed.hostname in _CDN_HOSTS
            and not parsed.username
            and parsed.port in (None, 443)
        )
    except ValueError:
        return None
    if not valid:
        return None
    match = _ATTACHMENT_PATH.fullmatch(parsed.path)
    return (match[1], match[2], unquote(match[3])) if match else None


def discord_message_link(url: str) -> tuple[str, str, str] | None:
    try:
        parsed = urlparse(clean_media_url(url))
        valid = (
            parsed.scheme == "https"
            and parsed.hostname in _MESSAGE_HOSTS
            and not parsed.username
            and parsed.port in (None, 443)
        )
    except ValueError:
        return None
    if not valid:
        return None
    match = _MESSAGE_PATH.fullmatch(parsed.path)
    return match.groups() if match else None


def _source_channel(message):
    interaction = getattr(message, "interaction", None)
    return getattr(interaction, "channel", None) or getattr(message, "channel", None)


async def resolve_discord_message(message, url: str):
    """Resolve a message link only inside the channel supplied by Discord."""
    link = discord_message_link(url)
    channel = _source_channel(message)
    if not link or str(getattr(channel, "id", "")) != link[1]:
        return None
    interaction = getattr(message, "interaction", None)
    guild = getattr(interaction, "guild", None) or getattr(message, "guild", None)
    guild_id = getattr(interaction, "guild_id", None) or getattr(guild, "id", None)
    if link[0] != (str(guild_id) if guild_id else "@me"):
        return None
    fetch = getattr(channel, "fetch_message", None)
    if not callable(fetch):
        return None
    try:
        return await asyncio.wait_for(fetch(int(link[2])), timeout=REFRESH_TIMEOUT)
    except Exception:
        return None


def _matching_urls(message, key):
    from utils import iter_message_payloads

    for payload in iter_message_payloads(message):
        for attachment in getattr(payload, "attachments", None) or []:
            for name in ("url", "proxy_url"):
                url = clean_media_url(getattr(attachment, name, "") or "")
                if discord_attachment_key(url) == key:
                    yield url
        for embed in getattr(payload, "embeds", None) or []:
            for name in ("image", "thumbnail", "video"):
                url = clean_media_url(
                    getattr(getattr(embed, name, None), "url", "") or ""
                )
                if discord_attachment_key(url) == key:
                    yield url
        for url in media_urls_in_text(getattr(payload, "content", "") or ""):
            if discord_attachment_key(url) == key:
                yield url


async def refresh_discord_media_url(bot, message, url: str) -> str | None:
    """One bounded refresh from known source messages or 25 recent rows.

    The second ID in /attachments/channel/ID/file is an ATTACHMENT ID. Never
    fetch_message(ID) with it. Match it against payloads instead.
    """
    key = discord_attachment_key(url)
    channel = _source_channel(message)
    if not key or str(getattr(channel, "id", "")) != key[0]:
        return None
    candidates = [message]
    parent = message
    for _depth in range(6):
        parent = getattr(getattr(parent, "reference", None), "resolved", None)
        if parent is None or any(parent is item for item in candidates):
            break
        candidates.append(parent)
    candidates.extend((getattr(bot, "_message_snapshots", None) or {}).values())
    seen = set()
    try:
        async with asyncio.timeout(REFRESH_TIMEOUT):
            fetch = getattr(channel, "fetch_message", None)
            fetched = 0
            for candidate in candidates:
                if getattr(candidate, "user_install", False):
                    # An app adapter's snowflake belongs to an interaction,
                    # not a fetchable message. Its selected reply parent and
                    # the bounded live history can still provide fresh URLs.
                    continue
                cid = getattr(
                    getattr(candidate, "channel", None), "id", None
                ) or getattr(candidate, "channel_id", None)
                if cid is not None and str(cid) != key[0]:
                    continue
                mid = str(getattr(candidate, "id", "") or "")
                if (
                    not mid.isdigit()
                    or mid in seen
                    or not any(_matching_urls(candidate, key))
                ):
                    continue
                seen.add(mid)
                if callable(fetch) and fetched < 2:
                    fetched += 1
                    try:
                        fresh = await fetch(int(mid))
                    except Exception:
                        continue
                    for refreshed in _matching_urls(fresh, key):
                        if refreshed != url:
                            return refreshed
            history = getattr(channel, "history", None)
            if callable(history):
                async for fresh in history(limit=REFRESH_HISTORY_LIMIT):
                    for refreshed in _matching_urls(fresh, key):
                        if refreshed != url:
                            return refreshed
    except Exception:
        return None
    return None
