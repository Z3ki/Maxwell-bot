"""Tools for Maxwell Bot

All tools return a result string for the LLM. They do NOT send errors
to the Discord channel — errors are returned as strings so the LLM can
generate a natural response. Only success outputs (images, DMs) are
sent directly to their target.
"""

import ipaddress
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any, cast
import socket
import tempfile

import asyncio
import base64
import discord
import aiohttp
import aiofiles
import logging
import random
from datetime import datetime, timezone, timedelta
from discord import Message, File, Activity, Status
from io import BytesIO
from urllib.parse import parse_qs, urlparse
from tools import Tool
from ddgs import DDGS as _DDGS
from utils import _atomic_json_write_sync  # single source of truth, fd-safe

logger = logging.getLogger(__name__)

# Owner IDs come from env var only — no hardcoded defaults to leak in open-source.
OWNER_IDS = {
    item.strip()
    for item in os.environ.get("MAXWELL_OWNER_IDS", "").split(",")
    if item.strip()
}


TTS_LANGUAGE_ALIASES = {
    "en": "english",
    "en-us": "english",
    "english": "english",
    "us": "english",
    "es": "spanish",
    "es-us": "spanish",
    "es-es": "spanish",
    "spanish": "spanish",
    "espanol": "spanish",
    "español": "spanish",
    "spanish_jason_angry": "spanish",
    "jason_es": "spanish",
}
TTS_RIVA_DEFAULTS = {
    "english": ("Magpie-Multilingual.EN-US.Jason.Angry", "en-US"),
    "spanish": ("Magpie-Multilingual.ES-US.Jason.Angry", "es-US"),
}

_SHARED_SESSION: aiohttp.ClientSession | None = None
_SESSION_LOCK = asyncio.Lock()


def _tts_language_key(
    language: str | None = None, lang: str | None = None, **kwargs
) -> str:
    requested = (
        str(
            language
            or lang
            or kwargs.get("language")
            or kwargs.get("lang")
            or "english"
        )
        .strip()
        .lower()
    )
    return TTS_LANGUAGE_ALIASES.get(requested, "english")


def _tts_riva_voice_config(language_key: str) -> tuple[str, str]:
    voice_env = "TTS_RIVA_VOICE_ES" if language_key == "spanish" else "TTS_RIVA_VOICE"
    lang_env = (
        "TTS_RIVA_LANGUAGE_ES" if language_key == "spanish" else "TTS_RIVA_LANGUAGE"
    )
    default_voice, default_code = TTS_RIVA_DEFAULTS.get(
        language_key, TTS_RIVA_DEFAULTS["english"]
    )
    return os.environ.get(voice_env, default_voice), os.environ.get(
        lang_env, default_code
    )


def _is_safe_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


class _SafeResolver:
    """Resolver that blocks private/internal addresses at request time."""

    def __init__(self):
        self._resolver = aiohttp.resolver.DefaultResolver()

    async def resolve(
        self, host, port=0, family: socket.AddressFamily = socket.AF_UNSPEC
    ):
        results = await self._resolver.resolve(host, port, family)
        for item in results:
            if not _is_safe_ip(item["host"]):
                raise OSError(f"blocked unsafe resolved address for {host}")
        return results

    async def close(self):
        await self._resolver.close()


async def _get_shared_session() -> aiohttp.ClientSession:
    global _SHARED_SESSION
    async with _SESSION_LOCK:
        if _SHARED_SESSION is None or _SHARED_SESSION.closed:
            connector = aiohttp.TCPConnector(
                resolver=cast(Any, _SafeResolver()), limit=30, limit_per_host=5
            )
            _SHARED_SESSION = aiohttp.ClientSession(connector=connector)
        return _SHARED_SESSION


async def close_shared_session():
    if _SHARED_SESSION and not _SHARED_SESSION.closed:
        await _SHARED_SESSION.close()


async def _read_response_limited(
    response: aiohttp.ClientResponse, max_bytes: int
) -> bytes:
    content_length = response.headers.get("Content-Length")
    if content_length:
        try:
            if int(content_length) > max_bytes:
                raise ValueError(f"response too large (max {max_bytes} bytes)")
        except ValueError as exc:
            if "response too large" in str(exc):
                raise
    chunks = []
    total = 0
    async for chunk in response.content.iter_chunked(64 * 1024):
        total += len(chunk)
        if total > max_bytes:
            raise ValueError(f"response too large (max {max_bytes} bytes)")
        chunks.append(chunk)
    return b"".join(chunks)


def _is_safe_url(url: str) -> bool:
    """Block SSRF: no private/loopback/link-local/localhost IPs."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        hostname = parsed.hostname
        if not hostname:
            return False
        # Block localhost names
        if hostname.lower() in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
            return False
        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            return True
        return _is_safe_ip(hostname)
    except Exception:
        return False


def _clean_discord_name(value: str | None, *, max_len: int = 100) -> str:
    text = str(value or "").strip()
    text = re.sub(r"[\r\n\t]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text[:max_len].strip()


def _clean_channel_name(value: str | None) -> str:
    text = _clean_discord_name(value, max_len=100).lower()
    text = re.sub(r"\s+", "-", text).strip("-")
    return text[:100]


# _atomic_json_write_sync imported from utils.py (fd-safe, single source of truth)


async def _resolve_guild(bot, message: Message, guild_id: str | None = None):
    if guild_id:
        try:
            gid = int(str(guild_id).strip())
        except (TypeError, ValueError):
            return None, f"Error: invalid guild_id: {guild_id}"
        guild = bot.get_guild(gid)
        if not guild:
            return None, f"Error: I am not in server {guild_id} or it is not cached"
        return guild, ""
    if getattr(message, "guild", None):
        return message.guild, ""
    return None, "Error: guild_id is required when using this from DMs or group chats"


def _guild_me(guild):
    return getattr(guild, "me", None) or getattr(guild, "self_member", None)


def _admin_caps(guild) -> tuple[set[str], str]:
    me = _guild_me(guild)
    if not me:
        return set(), "bot member is not cached"
    perms = getattr(me, "guild_permissions", None)
    if not perms:
        return set(), "permissions are not cached"
    caps = set()
    if getattr(perms, "administrator", False):
        caps.update(
            {
                "administrator",
                "manage_channels",
                "manage_roles",
                "manage_guild",
                "manage_messages",
                "kick_members",
                "ban_members",
            }
        )
    else:
        for name in (
            "manage_channels",
            "manage_roles",
            "manage_guild",
            "manage_messages",
            "kick_members",
            "ban_members",
        ):
            if getattr(perms, name, False):
                caps.add(name)
    return caps, ""


def _has_guild_cap(guild, cap: str) -> bool:
    caps, _reason = _admin_caps(guild)
    return "administrator" in caps or cap in caps


def _channel_label(channel) -> str:
    name = getattr(channel, "name", None) or str(getattr(channel, "id", "unknown"))
    return f"#{name} ({getattr(channel, 'id', '?')})"


class ImageGeneratorTool(Tool):
    """Fast image generation using NVIDIA Flux"""

    def get_description(self):
        return (
            "Generate an AI image (~5s) — the DEFAULT image tool. "
            "Params: prompt (required). Posts the image to chat with a CDN URL you can reuse in sites."
        )

    async def execute(
        self, message: Message, prompt: str | None = None, **kwargs
    ) -> str:
        if not prompt:
            return "Error: prompt parameter is required"
        if not self.bot.config.NVIDIA_API_KEY:
            return "Error: image generation is not configured (missing NVIDIA_API_KEY)"
        return await self._nvidia_generate(message, prompt)

    async def _nvidia_generate(self, message: Message, prompt: str) -> str:
        api_key = self.bot.config.NVIDIA_API_KEY
        api_url = self.bot.config.NVIDIA_IMAGE_URL
        payload = {
            "prompt": prompt,
            "mode": "base",
            "cfg_scale": 3.5,
            "width": 1024,
            "height": 1024,
            "seed": random.randint(0, 1000000),
            "steps": 20,
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        session = await _get_shared_session()
        max_retries = 3
        last_error = None
        for attempt in range(max_retries):
            try:
                async with session.post(
                    api_url,
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=120),
                ) as response:
                    if response.status == 429:
                        wait_time = (attempt + 1) * 10
                        logger.warning(
                            f"NVIDIA image rate limited, retry {attempt + 1}/{max_retries}"
                        )
                        await asyncio.sleep(wait_time)
                        continue
                    if 500 <= response.status < 600:
                        error_text = await response.text()
                        logger.warning(
                            f"NVIDIA image server error {response.status}, retry {attempt + 1}/{max_retries}: {error_text[:200]}"
                        )
                        wait_time = (attempt + 1) * 15
                        await asyncio.sleep(wait_time)
                        continue
                    if response.status != 200:
                        error_text = await response.text()
                        logger.error(
                            f"NVIDIA image error: {response.status} - {error_text[:500]}"
                        )
                        last_error = f"Error generating image: API returned status {response.status}. Try again later."
                        break
                    data = await response.json()
                    if "artifacts" not in data or not data["artifacts"]:
                        logger.error(
                            f"NVIDIA image response missing artifacts: {list(data.keys())}"
                        )
                        last_error = "Error: No image data in response"
                        break
                    artifact = data["artifacts"][0]
                    image_b64 = artifact.get("base64")
                    finish_reason = artifact.get("finishReason")
                    if finish_reason != "SUCCESS" or not image_b64:
                        logger.error(
                            f"NVIDIA image artifact issue: finishReason={finish_reason}, base64_present={bool(image_b64)}"
                        )
                        if finish_reason == "CONTENT_FILTERED":
                            last_error = "Error: Image was filtered by safety guardrails. Try a different prompt."
                        else:
                            last_error = "Error: No base64 image data in response"
                        break
                    image_bytes = base64.b64decode(image_b64)
                    logger.info(
                        f"NVIDIA image generated successfully, size: {len(image_bytes)} bytes"
                    )
                    # Send to Discord so the model can SEE it in chat
                    file = File(BytesIO(image_bytes), filename="generated_image.png")
                    sent_msg = None
                    try:
                        sent_msg = await message.channel.send(file=file)
                    except discord.Forbidden:
                        logger.warning(
                            f"Cannot send image in {message.channel.id} — missing permissions"
                        )
                        return "Error: Cannot send image — missing permissions"
                    # Grab the Discord CDN URL
                    cdn_url = None
                    if sent_msg and sent_msg.attachments:
                        cdn_url = sent_msg.attachments[0].url
                    await self.bot.memory.add_to_channel_memory(
                        str(message.channel.id),
                        {
                            "author": "Tool",
                            "content": f"Generated image: {prompt[:200]}",
                            "is_tool": True,
                        },
                    )
                    result = f"Image sent to chat: {prompt[:100]}"
                    if cdn_url:
                        result += f"\nImage URL: {cdn_url}"
                    result += "\nLook at the image you just posted. If it looks good, mention the URL or use it for the site. "
                    result += "If it looks bad, call image_generator again with an improved prompt."
                    return result
            except asyncio.TimeoutError:
                logger.warning(
                    f"NVIDIA image timeout, attempt {attempt + 1}/{max_retries}"
                )
                if attempt < max_retries - 1:
                    await asyncio.sleep(5)
                    continue
                last_error = "Error: Image generation timed out after retries"
                break
            except Exception as e:
                logger.error(f"NVIDIA image generation error: {e}")
                last_error = f"Error generating image: {e}"
                break
        if last_error:
            return last_error
        return "Error: Image generation failed after retries"


class HDImageGeneratorTool(Tool):
    """HD image generation using GPT-Image-2 (slower, better quality)"""

    def get_description(self):
        return (
            "Generate an HD AI image (~40s). Use ONLY when the user explicitly asks for high quality/HD/HQ. "
            "Params: prompt (required), size (optional, e.g. '1024x1024'). Returns a Discord CDN URL for sites."
        )

    async def execute(
        self,
        message: Message,
        prompt: str | None = None,
        size: str = "1024x1024",
        **kwargs,
    ) -> str:
        if not prompt:
            return "Error: prompt parameter is required"

        api_url = getattr(self.bot.config, "GPT_IMAGE_URL", "")
        api_key = getattr(self.bot.config, "GPT_IMAGE_API_KEY", "")
        if not api_url or not api_key:
            return "Error: HD image generation is not configured (missing GPT_IMAGE_URL or GPT_IMAGE_API_KEY)"

        payload = {
            "model": "gpt-image-2",
            "prompt": prompt,
            "size": size,
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        session = await _get_shared_session()
        image_url = None
        revised_prompt = None

        try:
            async with session.post(
                api_url,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=120),
            ) as response:
                if response.status != 200:
                    error_text = await response.text()
                    logger.error(
                        f"HD image API error: {response.status} - {error_text[:500]}"
                    )
                    return f"Error generating HD image: API returned status {response.status}"
                data = await response.json()
                if "data" not in data or not data["data"]:
                    logger.error(f"HD image response missing data: {list(data.keys())}")
                    return "Error: No image data in HD response"
                item = data["data"][0]
                image_url = item.get("url")
                revised_prompt = item.get("revised_prompt")
                if not image_url:
                    return "Error: No image URL in HD response"
        except asyncio.TimeoutError:
            logger.warning("HD image generation timed out")
            return "Error: HD image generation timed out after 120s"
        except Exception as e:
            logger.error(f"HD image generation request error: {e}")
            return f"Error generating HD image: {e}"

        if not _is_safe_url(image_url):
            return "Error: HD image service returned an unsafe image URL"

        # Fetch the actual PNG from the returned URL
        try:
            async with session.get(
                image_url,
                timeout=aiohttp.ClientTimeout(total=30),
                allow_redirects=False,
            ) as img_resp:
                if img_resp.status != 200:
                    logger.error(
                        f"HD image download error: {img_resp.status} for {image_url}"
                    )
                    return (
                        f"Error: Could not download HD image (status {img_resp.status})"
                    )
                image_bytes = await _read_response_limited(img_resp, 25 * 1024 * 1024)
        except asyncio.TimeoutError:
            logger.warning(f"HD image download timed out for {image_url}")
            return "Error: Timed out downloading HD image"
        except Exception as e:
            logger.error(f"HD image download error: {e}")
            return f"Error downloading HD image: {e}"

        # Upload to Discord and grab the CDN URL
        file = File(BytesIO(image_bytes), filename="hd_generated_image.png")
        sent_msg = None
        try:
            sent_msg = await message.channel.send(file=file)
        except discord.Forbidden:
            logger.warning(
                f"Cannot send HD image in {message.channel.id} — missing permissions"
            )
            return "Error: Cannot send HD image — missing permissions"

        # Grab the Discord CDN URL from the attachment
        cdn_url = None
        if sent_msg and sent_msg.attachments:
            cdn_url = sent_msg.attachments[0].url

        await self.bot.memory.add_to_channel_memory(
            str(message.channel.id),
            {
                "author": "Tool",
                "content": f"Generated HD image: {revised_prompt or prompt[:200]}",
                "is_tool": True,
            },
        )
        result = f"HD image generated successfully: {(revised_prompt or prompt)[:100]}"
        if cdn_url:
            result += f"\nImage URL: {cdn_url}"
            result += "\nUse this URL directly in HTML <img> tags."
        return result


class MemoryTool(Tool):
    """Manage long-term persistent memories"""

    def get_description(self):
        return (
            "Manage long-term memories (persist across all conversations). "
            "Only store important facts/preferences/context — no logs or trivia. "
            "Actions: add (content), edit (memory_id, content), remove (memory_id)."
        )

    async def execute(
        self,
        message: Message,
        action: str | None = None,
        content: str | None = None,
        memory_id: str | None = None,
        **kwargs,
    ) -> str:
        if not action:
            return "Error: action is required (add/edit/remove)"

        action = action.lower()

        if action == "add":
            if not content:
                return "Error: content is required for add"
            new_id = await self.bot.memory.add_long_term_memory(content)
            logger.info(f"Added long-term memory #{new_id}")
            return f"Memory #{new_id} saved successfully"

        elif action == "edit":
            if not memory_id or not content:
                return "Error: memory_id and content are required for edit"
            found = await self.bot.memory.edit_long_term_memory(memory_id, content)
            if found:
                return f"Memory #{memory_id} updated successfully"
            return f"Error: Memory #{memory_id} not found"

        elif action == "remove":
            if not memory_id:
                return "Error: memory_id is required for remove"
            found = await self.bot.memory.remove_long_term_memory(memory_id)
            if found:
                return f"Memory #{memory_id} removed successfully"
            return f"Error: Memory #{memory_id} not found"

        return f"Error: Unknown action '{action}'. Use add/edit/remove"


class ReactTool(Tool):
    """React to a message with an emoji"""

    _CUSTOM_EMOJI_RE = re.compile(
        r"^<(?P<animated>a?):(?P<name>[A-Za-z0-9_]{2,32}):(?P<id>\d{15,25})>$"
    )
    _BROKEN_CUSTOM_EMOJI_RE = re.compile(r"^<a?:(?P<name>[A-Za-z0-9_]{2,32}):?>$")
    _ALIAS_RE = re.compile(r"^:(?P<name>[A-Za-z0-9_]{2,32}):$")
    _BARE_CUSTOM_NAME_RE = re.compile(r"^[A-Za-z0-9_]{2,32}$")

    def get_description(self):
        return (
            "React to the current message with an emoji. "
            "Standard emoji: 👍 🐱 🔥. Custom emoji: use an available guild emoji name like dave, or a full <:name:id> emoji. "
            "Params: emoji (required)."
        )

    def _available_custom_emoji_hint(self, guild_id: str | None) -> str:
        if not guild_id:
            return "No guild custom emojis are available here; use a normal Unicode emoji like 👍."
        names = sorted((self.bot._guild_emojis.get(guild_id, {}) or {}).keys())[:20]
        if not names:
            return "This guild has no custom emojis cached; use a normal Unicode emoji like 👍."
        return "Available custom emoji names: " + ", ".join(names)

    async def execute(
        self, message: Message, emoji: str | None = None, **kwargs
    ) -> str:
        if not emoji:
            return "Error: emoji parameter is required"

        raw = str(emoji).strip()
        if not raw:
            return "Error: emoji parameter is required"

        guild = message.guild
        guild_id = str(guild.id) if guild else None
        guild_emojis = self.bot._guild_emojis.get(guild_id, {}) if guild_id else {}

        full_match = self._CUSTOM_EMOJI_RE.match(raw)
        if full_match and guild:
            emoji_id = int(full_match.group("id"))
            for e in guild.emojis:
                if int(e.id) == emoji_id:
                    try:
                        await message.add_reaction(e)
                        return f"Reacted with {e}"
                    except discord.HTTPException as ex:
                        return f"Error: Could not add reaction — {ex}"
            # Full emoji strings can still be valid if Discord lets this bot use
            # the emoji cross-guild. Try it, but don't try malformed nonsense.
            try:
                await message.add_reaction(raw)
                return f"Reacted with {raw}"
            except discord.NotFound:
                return f"Error: Emoji '{raw}' not found or invalid"
            except discord.HTTPException as e:
                return f"Error: Could not add reaction — {e}"

        alias_match = self._ALIAS_RE.match(raw)
        broken_match = self._BROKEN_CUSTOM_EMOJI_RE.match(raw)
        custom_name_match = alias_match if alias_match is not None else broken_match
        if custom_name_match is not None:
            lookup = custom_name_match.group("name").lower()
        else:
            lookup = raw.lower()

        if guild and self._BARE_CUSTOM_NAME_RE.match(lookup):
            if lookup in guild_emojis:
                for e in guild.emojis:
                    if e.name.lower() == lookup:
                        try:
                            await message.add_reaction(e)
                            return f"Reacted with {e}"
                        except discord.HTTPException as ex:
                            return f"Error: Could not add reaction — {ex}"
            if (
                alias_match
                or broken_match
                or raw == lookup
                or self._BARE_CUSTOM_NAME_RE.match(raw)
            ):
                # Discord treats unknown custom names as a 400. Returning a local
                # error keeps the LLM from faceplanting into Unknown Emoji loops.
                return f"Error: custom emoji '{lookup}' is not available in this guild. {self._available_custom_emoji_hint(guild_id)}"

        if (
            alias_match
            or broken_match
            or (not guild and self._BARE_CUSTOM_NAME_RE.match(lookup))
        ):
            return f"Error: custom emoji '{lookup}' is not available here. {self._available_custom_emoji_hint(guild_id)}"

        # Fallback: Unicode emoji or another Discord-supported reaction string.
        try:
            await message.add_reaction(raw)
            return f"Reacted with {raw}"
        except discord.NotFound:
            return f"Error: Emoji '{raw}' not found or invalid"
        except discord.HTTPException as e:
            return f"Error: Could not add reaction — {e}"


class EditMessageTool(Tool):
    """Edit one of the bot's own messages"""

    def get_description(self):
        return "Edit your own message. Params: message_id (required), content (required, new text)."

    async def execute(
        self,
        message: Message,
        message_id: str | None = None,
        content: str | None = None,
        **kwargs,
    ) -> str:
        if not message_id or not content:
            return "Error: message_id and content are required"
        try:
            msg = await message.channel.fetch_message(int(message_id))
            if msg.author.id != self.bot.user.id:
                return "Error: I can only edit my own messages"
            await msg.edit(content=content)
            return f"Message {message_id} edited successfully"
        except discord.NotFound:
            return f"Error: Message {message_id} not found"
        except discord.Forbidden:
            return "Error: I don't have permission to edit that message"
        except Exception as e:
            return f"Error editing message: {e}"


class DeleteMessageTool(Tool):
    """Delete one of the bot's own messages"""

    def get_description(self):
        return "Delete your own message. Params: message_id (required)."

    async def execute(
        self, message: Message, message_id: str | None = None, **kwargs
    ) -> str:
        if not message_id:
            return "Error: message_id is required"
        try:
            msg = await message.channel.fetch_message(int(message_id))
            if msg.author.id != self.bot.user.id:
                return "Error: I can only delete my own messages"
            await msg.delete()
            return f"Message {message_id} deleted"
        except discord.NotFound:
            return f"Error: Message {message_id} not found"
        except discord.Forbidden:
            return "Error: I don't have permission to delete that message"
        except Exception as e:
            return f"Error deleting message: {e}"


class ChangePresenceTool(Tool):
    """Change bot online status"""

    def get_description(self):
        return "Set your online availability/status dot. Params: status (online/idle/dnd/invisible). Use set_activity for the visible custom status text."

    async def execute(self, message: Message, status: str = "online", **kwargs) -> str:
        valid = ["online", "idle", "dnd", "invisible"]
        if status not in valid:
            return f"Error: status must be one of {', '.join(valid)}"
        status_obj = getattr(Status, status, Status.online)
        activities = self.bot._build_activities()
        await self.bot.change_presence(
            status=status_obj,
            activities=activities,
            edit_settings=bool(self.bot._custom_status),
        )
        return f"Status set to {status}"


class SetActivityTool(Tool):
    """Set bot activity/custom status"""

    def get_description(self):
        return (
            "Set your activity or custom status message (the visible text under your name). Use this whenever a user asks you to change/update your status, activity, vibe, or what you are doing. "
            "Params: type (playing/watching/listening/competing/custom), text (the status text), "
            "elapsed (optional, show time played, e.g. '2h 30m' or '45m'). "
            "Use type='custom' for a plain status message like 'chilling'. "
            "Setting a game activity keeps your custom status intact. "
            "Call with text='' to clear."
        )

    def _parse_elapsed(self, elapsed: str) -> int:
        import re as _re

        total_ms = 0
        for match in _re.finditer(r"(\d+)\s*(h|m|s|d)", elapsed.lower()):
            val = int(match.group(1))
            unit = match.group(2)
            if unit == "d":
                total_ms += val * 86400000
            elif unit == "h":
                total_ms += val * 3600000
            elif unit == "m":
                total_ms += val * 60000
            elif unit == "s":
                total_ms += val * 1000
        if total_ms == 0:
            try:
                total_ms = int(elapsed) * 60000
            except ValueError:
                total_ms = 0
        return total_ms

    async def execute(
        self,
        message: Message,
        type: str | None = None,
        text: str | None = None,
        elapsed: str | None = None,
        **kwargs,
    ) -> str:
        activity_type = (type or "custom").lower()

        if not text:
            if activity_type == "custom":
                self.bot._custom_status = None
            else:
                self.bot._current_game = None
            activities = self.bot._build_activities()
            if not activities:
                await self.bot.change_presence(activity=None, edit_settings=True)
            else:
                await self.bot.change_presence(
                    activities=activities, edit_settings=bool(self.bot._custom_status)
                )
            return "Cleared"

        if activity_type == "custom":
            self.bot._custom_status = discord.CustomActivity(name=text, state=text)
        elif activity_type in ("playing", "watching", "listening", "competing"):
            act_kwargs = {
                "type": getattr(discord.ActivityType, activity_type),
                "name": text,
            }
            if elapsed:
                ms = self._parse_elapsed(elapsed)
                if ms > 0:
                    start_time = datetime.now(timezone.utc) - timedelta(milliseconds=ms)
                    act_kwargs["timestamps"] = discord.ActivityTimestamps(
                        start=start_time
                    )
            self.bot._current_game = Activity(**act_kwargs)
        else:
            return "Error: type must be playing/watching/listening/competing/custom"

        activities = self.bot._build_activities()
        await self.bot.change_presence(
            activities=activities, edit_settings=bool(self.bot._custom_status)
        )
        if activity_type == "custom":
            return f"Custom status set: {text}"
        elapsed_str = f" ({elapsed} elapsed)" if elapsed else ""
        return f"Activity set: {activity_type} {text}{elapsed_str}"


class CreatePollTool(Tool):
    """Create a poll in the channel"""

    def get_description(self):
        return (
            "Create a poll. Params: question (required), options (required, comma-separated, e.g. 'Yes,No,Maybe'), "
            "duration_hours (optional, default 24)."
        )

    async def execute(
        self,
        message: Message,
        question: str | None = None,
        options: str | None = None,
        duration_hours: str = "24",
        **kwargs,
    ) -> str:
        if not question or not options:
            return "Error: question and options are required"
        try:
            option_list = [o.strip() for o in options.split(",") if o.strip()]
            if len(option_list) < 2:
                return "Error: Need at least 2 options for a poll"
            if len(option_list) > 10:
                return "Error: Maximum 10 options allowed"

            import datetime

            hours = int(duration_hours)
            if hours < 1 or hours > 168:
                return "Error: duration_hours must be between 1 and 168"
            poll = discord.Poll(
                question=question,
                duration=datetime.timedelta(hours=hours),
            )
            for opt in option_list:
                poll.add_answer(text=opt)

            await message.channel.send(poll=poll)
            return f"Poll created: '{question}' with options: {', '.join(option_list)}"
        except ValueError:
            return "Error: duration_hours must be a number"
        except Exception as e:
            return f"Error creating poll: {e}"


class CreateInviteTool(Tool):
    """Create an invite link for the server"""

    def get_description(self):
        return (
            "Create a server invite link. Only works in servers. "
            "Params: max_uses (optional, default 1), max_age (optional, seconds, default 86400)."
        )

    async def execute(
        self, message: Message, max_uses: str = "1", max_age: str = "86400", **kwargs
    ) -> str:
        if not self.bot or not self.bot._is_admin(message.author.id):
            return "Error: create_invite is admin-only"
        if not message.guild:
            return "Error: Cannot create invites in DMs"
        try:
            uses = int(max_uses)
            age = int(max_age)
            if uses < 1 or uses > 100:
                return "Error: max_uses must be between 1 and 100"
            if age < 0 or age > 604800:
                return "Error: max_age must be between 0 and 604800 seconds"
            channel = cast(Any, message.channel)
            if not hasattr(channel, "create_invite"):
                return "Error: Cannot create invites from this channel type"
            invite = await channel.create_invite(max_uses=uses, max_age=age)
            return (
                f"Invite created: {invite.url} (max uses: {uses}, expires in: {age}s)"
            )
        except discord.Forbidden:
            return "Error: I don't have permission to create invites here"
        except ValueError:
            return "Error: max_uses and max_age must be numbers"
        except Exception as e:
            return f"Error creating invite: {e}"


class LookupUserTool(Tool):
    """Look up information about a Discord user"""

    def get_description(self):
        return "Look up a Discord user by ID or mention. Params: user_id (required, numeric ID or @mention). Returns name, creation date, avatar."

    async def execute(
        self, message: Message, user_id: str | None = None, **kwargs
    ) -> str:
        if not user_id:
            return "Error: user_id is required"
        # Strip mention syntax like <@123456> or <@!123456>
        cleaned = re.sub(r"[^0-9]", "", str(user_id))
        if not cleaned:
            return f"Error: Could not extract a numeric user ID from '{user_id}'"
        try:
            user = await self.bot.fetch_user(int(cleaned))
            if not user:
                return f"Error: User {user_id} not found"
            created = (
                user.created_at.strftime("%Y-%m-%d") if user.created_at else "unknown"
            )
            info = (
                f"Name: {user.display_name} (@{user.name})\n"
                f"ID: {user.id}\n"
                f"Created: {created}\n"
                f"Bot: {user.bot}\n"
                f"Avatar: {getattr(user.display_avatar, 'url', 'none') if hasattr(user, 'display_avatar') else getattr(user, 'avatar_url', 'none')}"
            )
            return info
        except discord.NotFound:
            return f"Error: User {user_id} not found"
        except ValueError:
            return f"Error: Invalid user_id: {user_id}"
        except Exception as e:
            return f"Error looking up user: {e}"


class SearchMessagesTool(Tool):
    """Search for messages in the server"""

    def get_description(self):
        return "Search messages in this server. Params: query (required), limit (optional, default 5)."

    async def execute(
        self, message: Message, query: str | None = None, limit: str = "5", **kwargs
    ) -> str:
        if not query:
            return "Error: query is required"
        if not message.guild:
            return "Error: Cannot search in DMs"
        try:
            search_limit = max(1, min(int(limit), 25))
            results = []
            async for msg in message.guild.search(content=query, limit=search_limit):
                snippet = msg.content[:150] + ("..." if len(msg.content) > 150 else "")
                results.append(f"[{msg.id}] {msg.author.display_name}: {snippet}")
            if not results:
                return f"No messages found matching '{query}'"
            return "Search results:\n" + "\n".join(results)
        except discord.Forbidden:
            return "Error: I don't have permission to search in this server"
        except Exception as e:
            return f"Error searching messages: {e}"


class SetNicknameTool(Tool):
    """Change the bot's own nickname in the server"""

    def get_description(self):
        return "Change your nickname in this server. Params: nickname (required, 'reset' to remove)."

    async def execute(
        self, message: Message, nickname: str | None = None, **kwargs
    ) -> str:
        if not nickname:
            return "Error: nickname is required"
        if not message.guild:
            return "Error: Cannot set nickname in DMs"
        try:
            nick = None if nickname.lower() == "reset" else nickname
            me = getattr(message.guild, "me", None)
            if me is None:
                return "Error: bot member is not cached"
            await me.edit(nick=nick)
            if nick:
                return f"Nickname changed to '{nickname}'"
            return "Nickname removed"
        except discord.Forbidden:
            return "Error: I don't have permission to change my nickname here"
        except Exception as e:
            return f"Error setting nickname: {e}"


class ForwardMessageTool(Tool):
    """Forward a message to another channel"""

    def get_description(self):
        return "Forward a message to another channel. Params: message_id (required), channel_id (required)."

    async def execute(
        self,
        message: Message,
        message_id: str | None = None,
        channel_id: str | None = None,
        **kwargs,
    ) -> str:
        if not message_id or not channel_id:
            return "Error: message_id and channel_id are required"
        if not getattr(self.bot, "_is_admin", lambda _uid: False)(
            getattr(message.author, "id", "")
        ):
            return "Error: forward_message is admin-only"
        try:
            dest = self.bot.get_channel(int(channel_id))
            if not dest:
                dest = await self.bot.fetch_channel(int(channel_id))
            if not dest:
                return f"Error: Channel {channel_id} not found"

            orig = await message.channel.fetch_message(int(message_id))
            if not orig:
                return f"Error: Message {message_id} not found"
            src_guild = getattr(message.channel, "guild", None)
            dest_guild = getattr(dest, "guild", None)
            if (
                src_guild
                and dest_guild
                and getattr(src_guild, "id", None) != getattr(dest_guild, "id", None)
            ):
                return "Error: refusing to forward across servers"

            await orig.forward(dest)
            channel_name = getattr(dest, "name", channel_id)
            guild_name = (
                getattr(dest.guild, "name", "DM") if hasattr(dest, "guild") else "DM"
            )
            return f"Forwarded message {message_id} to #{channel_name} in {guild_name}"
        except discord.NotFound:
            return "Error: Message or channel not found"
        except discord.Forbidden:
            return "Error: I don't have permission to forward messages"
        except Exception as e:
            return f"Error forwarding message: {e}"


class TypingTool(Tool):
    """Trigger typing indicator in the channel"""

    def get_description(self):
        return "Trigger typing indicator. No params."

    async def execute(self, message: Message, **kwargs) -> str:
        try:
            async with message.channel.typing():
                pass
            return "Triggered typing indicator"
        except Exception as e:
            return f"Error triggering typing: {e}"


class ListServersTool(Tool):
    """List all servers and group chats the bot is in"""

    def get_description(self):
        return "List your servers and group chats. No params."

    async def execute(self, message: Message, **kwargs) -> str:
        lines = []
        if self.bot.guilds:
            lines.append(f"Servers ({len(self.bot.guilds)}):")
            for guild in self.bot.guilds[:20]:
                lines.append(f"  • {guild.name} (ID: {guild.id})")
            if len(self.bot.guilds) > 20:
                lines.append(f"  ... and {len(self.bot.guilds) - 20} more")

        group_channels = [
            ch
            for ch in self.bot.private_channels
            if isinstance(ch, discord.GroupChannel)
        ]
        if group_channels:
            lines.append(f"\nGroup chats ({len(group_channels)}):")
            for gc in group_channels[:10]:
                lines.append(f"  • {gc.name or 'Unnamed'} (ID: {gc.id})")

        if not lines:
            return "You're not in any servers or group chats."
        return "\n".join(lines)


class ListAdminServersTool(Tool):
    """List servers where Maxwell has useful admin permissions."""

    def get_description(self):
        return (
            "List servers where you have usable admin/mod permissions, especially manage_channels. "
            "Use this before trying server admin actions. No params."
        )

    async def execute(self, message: Message, **kwargs) -> str:
        rows = []
        for guild in getattr(self.bot, "guilds", []) or []:
            caps, reason = _admin_caps(guild)
            if not caps:
                continue
            channels = list(getattr(guild, "channels", []) or [])
            cats = [ch for ch in channels if isinstance(ch, discord.CategoryChannel)]
            text = [ch for ch in channels if isinstance(ch, discord.TextChannel)]
            voice = [ch for ch in channels if isinstance(ch, discord.VoiceChannel)]
            cap_text = ", ".join(sorted(caps)) if caps else reason
            rows.append(
                f"{guild.name} (ID: {guild.id}) | caps: {cap_text} | "
                f"categories: {len(cats)} text: {len(text)} voice: {len(voice)}"
            )
        if not rows:
            return "No servers with cached admin/manage permissions. Don't try admin tools until this lists a target."
        return "Servers with usable admin tools:\n" + "\n".join(rows[:30])


class CreateCategoryTool(Tool):
    """Create a Discord category channel."""

    def get_description(self):
        return (
            "Create a Discord category (the separator/group that channels sit under). Requires manage_channels. "
            "Params: name (required), guild_id (optional unless not in that server), position (optional). "
            "Use list_admin_servers first to pick a server where manage_channels is available."
        )

    async def execute(
        self,
        message: Message,
        name: str | None = None,
        guild_id: str | None = None,
        position: str | None = None,
        **kwargs,
    ) -> str:
        if self.bot and not self.bot._is_admin(message.author.id):
            return "Error: create_category is admin-only"
        clean = _clean_discord_name(name)
        if not clean:
            return "Error: name is required"
        guild, error = await _resolve_guild(self.bot, message, guild_id)
        if error:
            return error
        if guild is None:
            return "Error: guild is unavailable"
        guild = cast(Any, guild)
        if not _has_guild_cap(guild, "manage_channels"):
            return f"Error: I do not have manage_channels/admin in {guild.name}. Run list_admin_servers first."
        try:
            category = await guild.create_category(
                clean, reason=f"Maxwell admin tool requested by {message.author}"
            )
            if position is not None:
                try:
                    await category.edit(
                        position=max(0, int(position)),
                        reason="Maxwell admin tool position update",
                    )
                except (TypeError, ValueError):
                    return f"Created category {category.name} ({category.id}), but position was invalid"
            return f"Created category {category.name} ({category.id}) in {guild.name}"
        except discord.Forbidden:
            return f"Error: Discord denied creating category in {guild.name}; missing manage_channels or role hierarchy issue"
        except Exception as e:
            return f"Error creating category: {e}"


class CreateChannelTool(Tool):
    """Create text or voice channels."""

    def get_description(self):
        return (
            "Create a Discord text or voice channel. Requires manage_channels. "
            "Params: name (required), kind/type (text or voice, default text), guild_id (optional), "
            "category_id or category_name (optional), topic (text only, optional), nsfw (optional), slowmode_seconds (optional). "
            "Use create_category first when the user wants a new channel group/section."
        )

    def _find_category(
        self, guild, category_id: str | None = None, category_name: str | None = None
    ):
        if category_id:
            try:
                cid = int(str(category_id).strip())
            except (TypeError, ValueError):
                return None, f"Error: invalid category_id: {category_id}"
            category = discord.utils.get(getattr(guild, "categories", []) or [], id=cid)
            if not category:
                return None, f"Error: category {category_id} not found in {guild.name}"
            return category, ""
        if category_name:
            wanted = str(category_name).strip().lower()
            matches = [
                cat
                for cat in (getattr(guild, "categories", []) or [])
                if cat.name.lower() == wanted
            ]
            if not matches:
                return (
                    None,
                    f"Error: category named '{category_name}' not found in {guild.name}",
                )
            if len(matches) > 1:
                return (
                    None,
                    f"Error: multiple categories named '{category_name}', use category_id",
                )
            return matches[0], ""
        return None, ""

    async def execute(
        self,
        message: Message,
        name: str | None = None,
        kind: str | None = None,
        type: str | None = None,
        guild_id: str | None = None,
        category_id: str | None = None,
        category_name: str | None = None,
        topic: str | None = None,
        nsfw: str = "false",
        slowmode_seconds: str = "0",
        **kwargs,
    ) -> str:
        if self.bot and not self.bot._is_admin(message.author.id):
            return "Error: create_channel is admin-only"
        clean = _clean_channel_name(name)
        if not clean:
            return "Error: name is required"
        guild, error = await _resolve_guild(self.bot, message, guild_id)
        if error:
            return error
        if guild is None:
            return "Error: guild is unavailable"
        guild = cast(Any, guild)
        if not _has_guild_cap(guild, "manage_channels"):
            return f"Error: I do not have manage_channels/admin in {guild.name}. Run list_admin_servers first."
        category, error = self._find_category(guild, category_id, category_name)
        category = cast(Any, category)
        if error:
            return error
        channel_kind = str(kind or type or "text").strip().lower()
        try:
            if channel_kind in {"voice", "vc"}:
                channel = await guild.create_voice_channel(
                    clean,
                    category=category,
                    reason=f"Maxwell admin tool requested by {message.author}",
                )
            elif channel_kind in {"text", "chat"}:
                try:
                    slowmode = max(0, min(int(slowmode_seconds or 0), 21600))
                except (TypeError, ValueError):
                    slowmode = 0
                channel = await guild.create_text_channel(
                    clean,
                    category=category,
                    topic=str(topic or "")[:1024],
                    nsfw=str(nsfw).lower() in {"1", "true", "yes", "on"},
                    slowmode_delay=slowmode,
                    reason=f"Maxwell admin tool requested by {message.author}",
                )
            else:
                return "Error: kind/type must be text or voice"
            where = f" under {category.name}" if category else ""
            return f"Created {channel_kind} channel {_channel_label(channel)} in {guild.name}{where}"
        except discord.Forbidden:
            return f"Error: Discord denied creating channel in {guild.name}; missing manage_channels or role hierarchy issue"
        except Exception as e:
            return f"Error creating channel: {e}"


class EditChannelTool(Tool):
    """Rename/move/update basic channel settings."""

    def get_description(self):
        return (
            "Edit a Discord channel. Requires manage_channels. Params: channel_id (required), "
            "name (optional), category_id or category_name (optional), topic (text only, optional), slowmode_seconds (text only, optional), nsfw (text only, optional)."
        )

    async def execute(
        self,
        message: Message,
        channel_id: str | None = None,
        name: str | None = None,
        category_id: str | None = None,
        category_name: str | None = None,
        topic: str | None = None,
        slowmode_seconds: str | None = None,
        nsfw: str | None = None,
        **kwargs,
    ) -> str:
        if self.bot and not self.bot._is_admin(message.author.id):
            return "Error: edit_channel is admin-only"
        if not channel_id:
            return "Error: channel_id is required"
        try:
            channel = self.bot.get_channel(
                int(channel_id)
            ) or await self.bot.fetch_channel(int(channel_id))
        except (TypeError, ValueError):
            return f"Error: invalid channel_id: {channel_id}"
        except Exception as e:
            return f"Error finding channel: {e}"
        guild = getattr(channel, "guild", None)
        if not guild:
            return "Error: channel is not in a server"
        if not _has_guild_cap(guild, "manage_channels"):
            return f"Error: I do not have manage_channels/admin in {guild.name}. Run list_admin_servers first."
        updates = {}
        if name:
            clean = (
                _clean_channel_name(name)
                if not isinstance(channel, discord.CategoryChannel)
                else _clean_discord_name(name)
            )
            if clean:
                updates["name"] = clean
        if category_id or category_name:
            category, error = CreateChannelTool(self.bot)._find_category(
                guild, category_id, category_name
            )
            if error:
                return error
            updates["category"] = category
        if isinstance(channel, discord.TextChannel):
            if topic is not None:
                updates["topic"] = str(topic)[:1024]
            if slowmode_seconds is not None:
                try:
                    updates["slowmode_delay"] = max(
                        0, min(int(slowmode_seconds), 21600)
                    )
                except (TypeError, ValueError):
                    return "Error: slowmode_seconds must be a number"
            if nsfw is not None:
                updates["nsfw"] = str(nsfw).lower() in {"1", "true", "yes", "on"}
        elif topic is not None or slowmode_seconds is not None or nsfw is not None:
            return (
                "Error: topic, slowmode_seconds, and nsfw only apply to text channels"
            )
        if not updates:
            return "Error: provide at least one edit field"
        try:
            await channel.edit(
                **updates, reason=f"Maxwell admin tool requested by {message.author}"
            )
            return f"Edited {_channel_label(channel)} in {guild.name}: {', '.join(sorted(updates))}"
        except discord.Forbidden:
            return f"Error: Discord denied editing {_channel_label(channel)}; missing manage_channels or role hierarchy issue"
        except Exception as e:
            return f"Error editing channel: {e}"


class DeleteChannelTool(Tool):
    """Delete a Discord channel with name confirmation."""

    def get_description(self):
        return (
            "Delete a Discord channel or category. Dangerous. Requires manage_channels. "
            "Params: channel_id (required), confirm_name (required and must exactly match the channel/category name)."
        )

    async def execute(
        self,
        message: Message,
        channel_id: str | None = None,
        confirm_name: str | None = None,
        **kwargs,
    ) -> str:
        if self.bot and not self.bot._is_admin(message.author.id):
            return "Error: delete_channel is admin-only"
        if not channel_id or not confirm_name:
            return "Error: channel_id and confirm_name are required"
        try:
            channel = self.bot.get_channel(
                int(channel_id)
            ) or await self.bot.fetch_channel(int(channel_id))
        except (TypeError, ValueError):
            return f"Error: invalid channel_id: {channel_id}"
        except Exception as e:
            return f"Error finding channel: {e}"
        guild = getattr(channel, "guild", None)
        if not guild:
            return "Error: channel is not in a server"
        if not _has_guild_cap(guild, "manage_channels"):
            return f"Error: I do not have manage_channels/admin in {guild.name}. Run list_admin_servers first."
        actual = getattr(channel, "name", "")
        if str(confirm_name) != actual:
            return f"Error: confirm_name must exactly match '{actual}'"
        try:
            label = _channel_label(channel)
            await channel.delete(
                reason=f"Maxwell admin tool requested by {message.author}"
            )
            return f"Deleted {label} from {guild.name}"
        except discord.Forbidden:
            return f"Error: Discord denied deleting {_channel_label(channel)}; missing manage_channels or role hierarchy issue"
        except Exception as e:
            return f"Error deleting channel: {e}"


class ChangeAvatarTool(Tool):
    """Change the bot's own profile picture"""

    def get_description(self):
        return "Change your profile picture. 30-min cooldown. Params: url (required, direct image URL jpg/png/gif/webp)."

    async def execute(self, message: Message, url: str | None = None, **kwargs) -> str:
        if not url:
            return "Error: url is required"

        if not _is_safe_url(url):
            return "Error: Cannot fetch from private/internal URLs"

        cooldown = 1800  # 30 minutes

        if self.bot._last_avatar_change:
            elapsed = (
                datetime.now(timezone.utc).timestamp() - self.bot._last_avatar_change
            )
            if elapsed < cooldown:
                remaining = int(cooldown - elapsed)
                return f"Error: Avatar on cooldown. Wait {remaining} more seconds."

        try:
            session = await _get_shared_session()
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=30), allow_redirects=False
            ) as resp:
                if resp.status != 200:
                    return f"Error: Could not download image (status {resp.status})"
                content_type = resp.headers.get("Content-Type", "")
                if content_type and not content_type.startswith("image/"):
                    return "Error: URL did not return an image"
                image_bytes = await _read_response_limited(resp, 10 * 1024 * 1024)

            await self.bot.user.edit(avatar=image_bytes)
            self.bot._last_avatar_change = datetime.now(timezone.utc).timestamp()
            return "Avatar changed successfully"
        except discord.HTTPException as e:
            return f"Error changing avatar: {e}"
        except Exception as e:
            return f"Error: {e}"


class CreateSiteTool(Tool):
    """Create a temporary website under the configured public /bot path."""

    MAX_CONTENT_SIZE = 300000

    def __init__(self, bot):
        super().__init__(bot)
        self.base_dir = getattr(bot.config, "MAXWELL_SITE_DIR", "public/bot")
        self.base_url = (
            getattr(
                bot.config, "MAXWELL_PUBLIC_BASE_URL", "https://maxwell.example.com"
            ).rstrip("/")
            + "/bot"
        )

    def get_description(self):
        return (
            f"Create a temporary website at {self.base_url}/<name>. Auto-deletes after 24h. "
            "Params: name (short slug, lowercase/numbers/hyphens), title (headline), "
            "body (FULL HTML document — write complete <!DOCTYPE html> pages with all styles/JS inline. "
            "Written as-is to file, no template wrapping), encoding (optional: text or base64; use base64 for exact full HTML). "
            "Only generate images with image_generator if the site NEEDS images (visual showcase, portfolio, etc). "
            "Plain text/CSS sites do NOT need images. If you do generate images, use the returned Discord CDN URL in <img> tags."
        )

    async def execute(
        self,
        message: Message,
        name: str | None = None,
        title: str | None = None,
        body: str | None = None,
        encoding: str = "text",
        images: str | None = None,
        **kwargs,
    ) -> str:
        if self.bot and not self.bot._is_admin(message.author.id):
            return "Error: create_site is admin-only"
        if not name or not title or body is None:
            missing = []
            if not name:
                missing.append("name")
            if not title:
                missing.append("title")
            if body is None:
                missing.append("body")
            return f"Error: missing required params — {', '.join(missing)}. All three (name, title, body) are needed to create a site."

        mode = str(encoding or "text").strip().lower()
        if mode in {"base64", "b64"}:
            try:
                body = base64.b64decode(str(body), validate=True).decode("utf-8")
            except Exception as e:
                return f"Error: could not decode base64 site body: {e}"
        elif mode not in {"text", "utf8", "utf-8"}:
            return "Error: encoding must be text or base64"

        # Sanitize name
        slug = re.sub(r"[^a-z0-9-]", "-", name.lower().strip())[:30].strip("-")
        if not slug or len(slug) < 2:
            return "Error: name must be at least 2 valid characters"

        user_id = str(message.author.id)
        if hasattr(self.bot, "_load_sites"):
            self.bot._load_sites(quiet=True)
        sites = self.bot._sites

        if len(body) > self.MAX_CONTENT_SIZE:
            return f"Error: content too long ({len(body)} chars, max {self.MAX_CONTENT_SIZE})"

        site_dir = os.path.join(self.base_dir, slug)
        try:
            os.makedirs(site_dir, exist_ok=True)

            # Copy images into site's images/ directory
            image_urls = []
            missing_images = []
            if images:
                try:
                    image_list = (
                        json.loads(images) if isinstance(images, str) else images
                    )
                    if not isinstance(image_list, list):
                        image_list = [image_list]
                except json.JSONDecodeError:
                    # Might be comma-separated paths
                    image_list = [
                        {"path": p.strip()} for p in images.split(",") if p.strip()
                    ]

                img_dir = os.path.join(site_dir, "images")
                os.makedirs(img_dir, exist_ok=True)
                for entry in image_list:
                    if isinstance(entry, str):
                        entry = {"path": entry}
                    src_path = entry.get("path", "")
                    if not src_path or not os.path.isfile(src_path):
                        missing_images.append(src_path or "(empty path)")
                        logger.warning(f"Site image not found: {src_path}")
                        continue
                    filename = entry.get("filename") or os.path.basename(src_path)
                    # Sanitize filename — only allow safe chars
                    filename = re.sub(r"[^a-zA-Z0-9._-]", "_", filename)
                    if not filename:
                        continue
                    dest = os.path.join(img_dir, filename)
                    try:
                        shutil.copy2(src_path, dest)
                        public_url = f"{self.base_url}/{slug}/images/{filename}"
                        image_urls.append(public_url)
                        logger.info(f"Copied site image {src_path} -> {dest}")
                    except Exception as e:
                        logger.warning(f"Failed to copy image {src_path}: {e}")

            index_path = os.path.join(site_dir, "index.html")
            # Inject CSP meta tag to mitigate XSS from arbitrary HTML
            csp_meta = (
                '<meta http-equiv="Content-Security-Policy" '
                "content=\"default-src 'self'; script-src 'self'; "
                "style-src 'unsafe-inline'; img-src * data:; connect-src 'self'\">"
            )
            if "<head" in body.lower():
                body = re.sub(
                    r"(<head[^>]*>)",
                    r"\1\n" + csp_meta,
                    body,
                    count=1,
                    flags=re.IGNORECASE,
                )
            elif "<html" in body.lower():
                body = re.sub(
                    r"(<html[^>]*>)",
                    r"\1\n<head>" + csp_meta + "</head>",
                    body,
                    count=1,
                    flags=re.IGNORECASE,
                )
            else:
                body = "<head>" + csp_meta + "</head>\n" + body
            async with aiofiles.open(index_path, "w", encoding="utf-8") as f:
                await f.write(body)

            sites[slug] = {
                "user_id": user_id,
                "user_name": message.author.display_name,
                "created_at": datetime.now(timezone.utc).timestamp(),
                "title": title,
                "path": site_dir,
            }
            await self._save_sites()
            result = f"Site created: {self.base_url}/{slug}/"
            if image_urls:
                result += f"\nEmbedded images ({len(image_urls)}):\n" + "\n".join(
                    f"  - {url}" for url in image_urls
                )
            if missing_images:
                result += (
                    f"\nWARNING: {len(missing_images)} image(s) NOT found on disk and skipped: "
                    + ", ".join(missing_images)
                )
            return result
        except Exception as e:
            logger.error(f"Failed to create site {slug}: {e}")
            return f"Error creating site: {e}"

    async def _save_sites(self):
        try:
            path = Path(self.bot.config.DATA_DIR) / "sites.json"
            await asyncio.to_thread(_atomic_json_write_sync, path, self.bot._sites)
            if hasattr(self.bot, "_sites_mtime"):
                self.bot._sites_mtime = path.stat().st_mtime
        except Exception as e:
            logger.error(f"Failed to save sites: {e}")
            raise


class ListSitesTool(Tool):
    """List your active temporary sites"""

    def get_description(self):
        return "List your active temporary websites. No params."

    async def execute(self, message: Message, **kwargs) -> str:
        user_id = str(message.author.id)
        if hasattr(self.bot, "_load_sites"):
            self.bot._load_sites(quiet=True)
        sites = self.bot._sites
        user_sites = {k: v for k, v in sites.items() if v.get("user_id") == user_id}

        if not user_sites:
            return "You don't have any active sites."

        lines = []
        now = datetime.now(timezone.utc).timestamp()
        for slug, data in user_sites.items():
            created = data.get("created_at", 0)
            age = now - created
            remaining = max(0, 86400 - age)
            hours = int(remaining // 3600)
            mins = int((remaining % 3600) // 60)
            title = data.get("title", "untitled")
            base_url = getattr(
                self.bot.config,
                "MAXWELL_PUBLIC_BASE_URL",
                "https://maxwell.example.com",
            ).rstrip("/")
            lines.append(
                f"  • {base_url}/bot/{slug}/ — '{title}' ({hours}h {mins}m left)"
            )
        return "Your active sites:\n" + "\n".join(lines)


class WebSearchTool(Tool):
    """Search the web using DuckDuckGo"""

    def get_description(self):
        return (
            "Search the web. Use proactively for factual/recent info you're not 100% certain about. "
            "Don't search for casual conversation. Params: query (required), max_results (optional, default 5, max 10)."
        )

    async def execute(
        self,
        message: Message,
        query: str | None = None,
        max_results: str = "5",
        **kwargs,
    ) -> str:
        if not query:
            return "Error: query is required"

        try:
            limit = max(1, min(int(max_results), 10))
        except (ValueError, TypeError):
            limit = 5

        try:
            loop = asyncio.get_running_loop()
            results = await loop.run_in_executor(
                None, lambda: list(_DDGS().text(query, max_results=limit))
            )

            if not results:
                return f"No results found for '{query}'"

            lines = []
            for i, r in enumerate(results, 1):
                title = r.get("title", "No title")
                href = r.get("href", "")
                body = r.get("body", "")[:200]
                lines.append(f"{i}. {title}\n   {href}\n   {body}")
            return "\n\n".join(lines)
        except Exception as e:
            logger.error(f"Web search error: {e}")
            return f"Error searching: {e}"


class SendMessageTool(Tool):
    """Send a reply to the current message with Discord markdown formatting."""

    def get_description(self):
        return (
            "Send a message to the current chat. Prefer this for final user-facing output. "
            "Content supports Discord markdown: **bold**, *italic*, `code`, ```code blocks```, > quotes, bullet lists. "
            "Params: content (required), reply (optional bool, default true)."
        )

    @staticmethod
    def _chunks(text: str, limit: int = 1900) -> list[str]:
        # Discord hard-fails over 2000 chars. Keep this dumb and reliable; fancy
        # code-fence stitching lives in bot.py, but tools must not explode.
        chunks = []
        remaining = text
        while remaining:
            if len(remaining) <= limit:
                chunks.append(remaining)
                break
            cut = remaining.rfind("\n", 0, limit)
            if cut < limit // 2:
                cut = limit
            chunks.append(remaining[:cut].rstrip())
            remaining = remaining[cut:].lstrip()
        return chunks or [""]

    async def execute(
        self, message: Message, content: str | None = None, reply: bool = True, **kwargs
    ) -> str:
        text = str(content or "").strip()
        if not text:
            return "Error: content is required"
        try:
            chunks = self._chunks(text)
            use_reply = str(reply).lower() not in {"0", "false", "no", "off"}
            for i, chunk in enumerate(chunks):
                if i == 0 and use_reply:
                    await message.reply(chunk)
                else:
                    await message.channel.send(chunk)
                if len(chunks) > 1:
                    await asyncio.sleep(0.2)
            return f"__MESSAGE_SENT__ Sent {len(text)} chars in {len(chunks)} chunk(s)"
        except discord.Forbidden:
            return "Error: missing permissions to send message"
        except Exception as e:
            return f"Error sending message: {e}"


class ReasoningLogTool(Tool):
    """Capture inspectable reasoning/decision metadata for dashboards."""

    def get_description(self):
        return (
            "Record a short reasoning trace before send_message/no_response. "
            "thoughts: one plain-English sentence only, no XML or JSON. "
            "intent: short label. decision: short label. "
            "confidence: optional low/medium/high. "
            "All values must be plain text. This does not reply to users."
        )

    _NESTED_TAG_RE = re.compile(
        r"</?(?:thoughts|intent|decision|confidence|assumptions|evidence|alternatives|risks|tool_plan|response_plan|data)\b[^>]*>",
        re.IGNORECASE,
    )

    @staticmethod
    def _sanitize_payload(raw: dict) -> dict:
        payload = {"thoughts": str(raw.get("thoughts", "")).strip()}
        payload.update({k: v for k, v in raw.items() if k != "thoughts"})
        thoughts = payload.get("thoughts", "")
        if "<" in thoughts and ">" in thoughts:
            extracted = {}
            for tag in ("intent", "decision", "confidence"):
                m = re.search(
                    rf"<{tag}>(.*?)</{tag}>", thoughts, re.IGNORECASE | re.DOTALL
                )
                if m:
                    extracted[tag] = m.group(1).strip()
            thoughts = ReasoningLogTool._NESTED_TAG_RE.sub("", thoughts).strip()
            if not thoughts:
                thoughts = " (no plain-text thoughts provided)"
            payload["thoughts"] = thoughts
            for k, v in extracted.items():
                payload.setdefault(k, v)
        for key in ("thoughts", "intent", "decision"):
            val = payload.get(key)
            if isinstance(val, str) and len(val) > 500:
                payload[key] = val[:497] + "..."
        payload.setdefault("intent", payload.get("decision", "reply"))
        payload.setdefault("confidence", str(payload.get("confidence") or ""))
        return payload

    async def execute(self, message: Message, **kwargs) -> str:
        try:
            payload = self._sanitize_payload(dict(kwargs or {}))
            await self.bot._record_llm_trace(message, payload)
            return "__REASONING_RECORDED__"
        except Exception as e:
            return f"Error recording reasoning: {e}"


class NoResponseTool(Tool):
    """Silently skip sending any reply to the current message"""

    def get_description(self):
        return (
            "Skip replying to this message entirely. Use this when the user message is not useful to engage with "
            "(e.g., spam, baiting, pure annoyance, or low-effort fillers like 'idc') or when you truly have nothing to add."
        )

    async def execute(self, message: Message, **kwargs) -> str:
        return "__NO_RESPONSE__"


class SendFileTool(Tool):
    """Create and send an arbitrary file attachment"""

    MAX_SIZE = 25 * 1024 * 1024

    def get_description(self):
        return (
            "Create a file with any filename/extension and send it as an attachment. "
            "Use this for .txt, .py, .json, .html, binary files, etc. "
            "For code/HTML/JSON or exact file bytes, prefer encoding=base64 so markup/backticks are preserved exactly. "
            "Params: filename (required), content (required), encoding (optional: text or base64; default text)."
        )

    async def execute(
        self,
        message: Message,
        filename: str | None = None,
        content: str | None = None,
        encoding: str = "text",
        **kwargs,
    ) -> str:
        if self.bot and not self.bot._is_admin(message.author.id):
            return "Error: send_file is admin-only"
        if not filename or not str(filename).strip():
            return "Error: filename is required"
        if content is None:
            return "Error: content is required"

        safe_name = Path(str(filename).strip()).name
        if not safe_name or safe_name in {".", ".."}:
            return "Error: invalid filename"

        mode = str(encoding or "text").strip().lower()
        try:
            if mode in {"base64", "b64"}:
                blob = base64.b64decode(str(content), validate=True)
            elif mode in {"text", "utf8", "utf-8"}:
                blob = str(content).encode("utf-8")
            else:
                return "Error: encoding must be text or base64"
        except Exception as e:
            return f"Error: could not decode file content: {e}"

        if len(blob) > self.MAX_SIZE:
            return f"Error: file is too large (max {self.MAX_SIZE // 1024 // 1024} MB)"

        file = File(BytesIO(blob), filename=safe_name)
        try:
            await message.reply(file=file)
        except discord.Forbidden:
            return "Error: no permission to send files here"
        except discord.HTTPException as e:
            return f"Error sending file: {e}"
        except Exception as e:
            return f"Error sending file: {e}"

        return f"__FILE_SENT__ Sent file: {safe_name} ({len(blob)} bytes)"


class ShellTool(Tool):
    """Execute shell commands in the dedicated Docker sandbox."""

    CONTAINER_NAME = "maxwell-shell"
    IMAGE_NAME = "maxwell-shell"
    DOCKERFILE_DIR = os.path.join(os.path.dirname(__file__), "docker")
    MAX_OUTPUT = 8000
    TIMEOUT = 60

    def get_description(self):
        return (
            "Run a shell command in the isolated maxwell-shell Docker sandbox with bash -lc. Output sent directly to chat. "
            "Params: command (required), files (optional: comma-separated file paths or JSON array to send as attachments after the command runs, "
            "e.g. 'output.png' or '[\"report.pdf\", \"data.csv\"]'. Files are copied from the container's /home/maxwell directory and sent to chat)."
        )

    async def _run_docker(self, *args: str, timeout: int = 30):
        proc = await asyncio.create_subprocess_exec(
            "docker",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        return await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        ), proc.returncode

    async def _ensure_container(self):
        try:
            (stdout, _stderr), code = await self._run_docker(
                "inspect", "-f", "{{.State.Running}}", self.CONTAINER_NAME, timeout=10
            )
            if code == 0:
                if stdout.decode(errors="replace").strip().lower() == "true":
                    return
                (_stdout, stderr), start_code = await self._run_docker(
                    "start", self.CONTAINER_NAME, timeout=15
                )
                if start_code == 0:
                    return
                raise RuntimeError(
                    stderr.decode(errors="replace").strip() or "docker start failed"
                )
        except FileNotFoundError:
            raise RuntimeError("docker is not installed or not on PATH")
        except asyncio.TimeoutError:
            raise RuntimeError("docker did not respond while checking sandbox")

        (_stdout, stderr), build_code = await self._run_docker(
            "build", "-t", self.IMAGE_NAME, self.DOCKERFILE_DIR, timeout=180
        )
        if build_code != 0:
            raise RuntimeError(
                stderr.decode(errors="replace").strip() or "docker build failed"
            )

        (_stdout, stderr), run_code = await self._run_docker(
            "run",
            "-d",
            "--name",
            self.CONTAINER_NAME,
            "--memory",
            "2g",
            "--cpus",
            "1.0",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "128",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=64m",
            "-v",
            f"{os.path.join(os.path.dirname(__file__), 'shelldocker')}:/home/maxwell:rw",
            self.IMAGE_NAME,
            timeout=30,
        )
        if run_code != 0:
            raise RuntimeError(
                stderr.decode(errors="replace").strip() or "docker run failed"
            )

    def _normalize_command(self, command: str | None) -> str:
        raw = str(command or "").strip()
        if not raw:
            return ""

        # If the model leaked a tool call payload, try to recover a literal command from backticks.
        if "<tool:" in raw.lower():
            m = re.search(r"`([^`]+)`", raw)
            if m:
                return m.group(1).strip()
            return ""
        return raw

    async def _run_shell_command(self, command: str):
        await self._ensure_container()
        sanitized = self._normalize_command(command)
        if not sanitized:
            raise RuntimeError("empty command")
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "exec",
            "--workdir",
            "/home/maxwell",
            "--user",
            "maxwell",
            self.CONTAINER_NAME,
            "bash",
            "-lc",
            sanitized,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=self.TIMEOUT
        )
        return stdout, stderr, proc.returncode

    async def execute(
        self,
        message: Message,
        command: str | None = None,
        files: str | None = None,
        **kwargs,
    ) -> str:
        normalized = self._normalize_command(command)
        if not normalized:
            return "Error: command is required (tool-call markup was detected or command was empty)"

        author_id = str(message.author.id)
        if not (
            self.bot._is_admin(author_id) or author_id in self.bot._shell_whitelist
        ):
            return "Error: You do not have permission to use the shell tool. Ask an admin to whitelist you with `,shell <user_id>`."

        try:
            stdout, stderr, exit_code = await self._run_shell_command(normalized)
        except asyncio.TimeoutError:
            text = f"$ {normalized}\n\u23f1 Timed out after {self.TIMEOUT}s"
            await message.channel.send(f"```ansi\n{text}\n```")
            return f"__SHELL_SENT__\n{text}"
        except Exception as e:
            text = f"$ {normalized}\n\u274c Error: {e}"
            await message.channel.send(f"```ansi\n{text}\n```")
            return f"__SHELL_SENT__\n{text}"

        out = stdout.decode(errors="replace")
        err = stderr.decode(errors="replace")
        combined = ""
        if out.strip():
            combined += out.strip()
        if err.strip():
            if combined:
                combined += "\n"
            combined += f"[stderr] {err.strip()}"
        if exit_code != 0:
            combined += f"\n[exit code: {exit_code}]"

        if len(combined) > self.MAX_OUTPUT:
            combined = combined[: self.MAX_OUTPUT] + "\n... (truncated)"

        text = f"$ {normalized}\n{combined}"
        chunks = []
        remaining = text
        while remaining:
            if len(remaining) <= 1990:
                chunks.append(remaining)
                break
            header = f"$ {normalized}\n"
            cut = remaining.rfind("\n", 0, 1990)
            if cut <= len(header):
                cut = 1990
            chunks.append(remaining[:cut])
            remaining = remaining[cut:].lstrip("\n")

        for chunk in chunks:
            await message.channel.send(f"```ansi\n{chunk}\n```")
            if len(chunks) > 1:
                await asyncio.sleep(0.3)

        result = f"__SHELL_SENT__\n{text}"

        # Send requested files from the container
        if files:
            file_paths = self._parse_file_list(files)
            sent_files = []
            for fpath in file_paths:
                sent = await self._send_container_file(message, fpath)
                if sent:
                    sent_files.append(sent)
            if sent_files:
                result += f"\nSent files: {', '.join(sent_files)}"

        return result

    @staticmethod
    def _parse_file_list(files: str) -> list[str]:
        raw = str(files or "").strip()
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(f).strip() for f in parsed if str(f).strip()]
            if isinstance(parsed, str):
                return [parsed.strip()] if parsed.strip() else []
        except (json.JSONDecodeError, ValueError):
            pass
        # Fall back to comma-separated
        return [f.strip() for f in raw.split(",") if f.strip()]

    async def _send_container_file(self, message: Message, rel_path: str) -> str | None:
        """Copy a file out of the container and send it to Discord. Returns filename on success."""
        # Sanitize — no path traversal escapes from /home/maxwell
        clean = rel_path.lstrip("/")
        if ".." in clean:
            logger.warning(f"Shell file send blocked — path traversal: {rel_path}")
            return None

        container_path = f"/home/maxwell/{clean}"
        tmp_dir = tempfile.mkdtemp(prefix="maxwell_shell_")
        local_path = os.path.join(tmp_dir, os.path.basename(clean))

        try:
            (_stdout, stderr), code = await self._run_docker(
                "cp", f"{self.CONTAINER_NAME}:{container_path}", local_path, timeout=15
            )
            if code != 0:
                logger.warning(
                    f"docker cp failed for {container_path}: {stderr.decode(errors='replace')}"
                )
                return None

            if not os.path.isfile(local_path):
                logger.warning(f"File not found after docker cp: {local_path}")
                return None

            file_size = os.path.getsize(local_path)
            if file_size > 25 * 1024 * 1024:
                logger.warning(f"Shell file too large to send: {file_size} bytes")
                return None

            filename = os.path.basename(clean)
            await message.channel.send(file=File(local_path, filename=filename))
            logger.info(f"Sent shell file: {filename} ({file_size} bytes)")
            return filename
        except asyncio.TimeoutError:
            logger.warning(f"docker cp timed out for {container_path}")
            return None
        except Exception as e:
            logger.warning(f"Failed to send shell file {rel_path}: {e}")
            return None
        finally:
            try:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            except Exception:
                pass


class FetchUrlTool(Tool):
    """Fetch and extract text content from a URL"""

    MAX_CONTENT = 15000
    MAX_BYTES = 1024 * 1024

    def get_description(self):
        return (
            "Fetch a URL and return readable text. Handles HTML, JSON, plain text. "
            "Params: url (required), max_length (optional, default 15000)."
        )

    async def execute(
        self,
        message: Message,
        url: str | None = None,
        max_length: str = "15000",
        **kwargs,
    ) -> str:
        if not url:
            return "Error: url is required"

        if not _is_safe_url(url):
            return "Error: Cannot fetch from private/internal URLs"

        try:
            max_len = max(1, min(int(max_length), self.MAX_CONTENT))
        except (ValueError, TypeError):
            max_len = self.MAX_CONTENT

        try:
            session = await _get_shared_session()
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=30), allow_redirects=False
            ) as resp:
                if resp.status != 200:
                    return f"Error: HTTP {resp.status}"
                content_type = resp.headers.get("Content-Type", "")
                raw = await _read_response_limited(resp, self.MAX_BYTES)
        except asyncio.TimeoutError:
            return f"Error: timed out fetching {url}"
        except Exception as e:
            return f"Error fetching URL: {e}"

        try:
            if "json" in content_type or url.endswith(".json"):
                text = raw.decode(errors="replace")
                import json as _json

                try:
                    text = _json.dumps(_json.loads(text), indent=2, ensure_ascii=False)
                except Exception:
                    pass
            elif (
                "html" in content_type
                or "<html" in raw[:500].decode(errors="replace").lower()
            ):
                html_text = raw.decode(errors="replace")
                text = html_text
                for tag in [
                    "script",
                    "style",
                    "noscript",
                    "header",
                    "footer",
                    "nav",
                    "aside",
                ]:
                    text = re.sub(
                        rf"<{tag}[^>]*>.*?</{tag}>",
                        "",
                        text,
                        flags=re.DOTALL | re.IGNORECASE,
                    )
                text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
                text = re.sub(
                    r"</?(?:p|div|li|h[1-6]|tr|blockquote)[^>]*>",
                    "\n",
                    text,
                    flags=re.IGNORECASE,
                )
                text = re.sub(r"<[^>]+>", "", text)
                text = re.sub(r"&nbsp;", " ", text)
                text = re.sub(r"&amp;", "&", text)
                text = re.sub(r"&lt;", "<", text)
                text = re.sub(r"&gt;", ">", text)
                text = re.sub(r"&quot;", '"', text)
                text = re.sub(r"&#\d+;", "", text)
                text = re.sub(r"\n{3,}", "\n\n", text)
                text = re.sub(r"[ \t]+", " ", text)
            else:
                text = raw.decode(errors="replace")
        except Exception as e:
            return f"Error parsing content: {e}"

        text = text.strip()
        if len(text) > max_len:
            text = text[:max_len] + "\n... (truncated)"

        return text


class YouTubeTool(Tool):
    """Fetch YouTube transcripts and optional timestamp frames."""

    MAX_TRANSCRIPT_CHARS = 20000
    MAX_FRAMES = 6
    YOUTUBE_HOST_RE = re.compile(r"(^|\.)(youtube\.com|youtu\.be|youtube-nocookie\.com)$", re.I)

    def get_description(self):
        return (
            "Fetch a YouTube video's transcript/captions and optionally extract still frames at timestamps. "
            "Extracted frames are attached to the model for inspection, not posted to chat. "
            "Use this for YouTube links instead of fetch_url. Params: url (required), timestamps (optional comma-separated seconds or mm:ss/hh:mm:ss), "
            "max_transcript_chars (optional, default 12000, max 20000), lang (optional, default en)."
        )

    @classmethod
    def _is_youtube_url(cls, url: str) -> bool:
        try:
            parsed = urlparse(url)
            return parsed.scheme in {"http", "https"} and bool(
                parsed.hostname and cls.YOUTUBE_HOST_RE.search(parsed.hostname)
            )
        except Exception:
            return False

    @classmethod
    def _extract_youtube_url(cls, raw: str) -> str:
        text = str(raw or "").strip()
        if "<" in text and ">" in text:
            text = re.sub(r"</?param\b[^>]*>", "", text, flags=re.IGNORECASE).strip()
            text = re.sub(r"</?(?:url|tool:youtube|youtube)\b[^>]*>", "", text, flags=re.IGNORECASE).strip()
        match = re.search(
            r"https?://(?:www\.)?(?:youtube\.com|youtu\.be|youtube-nocookie\.com)/[^\s<>\"']+",
            text,
            re.IGNORECASE,
        )
        return match.group(0).rstrip(".,)]") if match else text

    @staticmethod
    def _video_id(url: str) -> str:
        try:
            parsed = urlparse(url)
            host = (parsed.hostname or "").lower()
            if host.endswith("youtu.be"):
                return parsed.path.strip("/").split("/", 1)[0]
            query_id = parse_qs(parsed.query).get("v", [""])[0]
            if query_id:
                return query_id
            match = re.search(r"/(?:embed|shorts|live)/([A-Za-z0-9_-]{6,})", parsed.path)
            return match.group(1) if match else ""
        except Exception:
            return ""

    @staticmethod
    def _parse_timestamp(value: str) -> float | None:
        text = str(value or "").strip().lower()
        if not text:
            return None
        text = text.removeprefix("t=")
        if re.fullmatch(r"\d+(?:\.\d+)?s?", text):
            return float(text.rstrip("s"))
        parts = text.split(":")
        if not 1 <= len(parts) <= 3:
            return None
        try:
            nums = [float(p) for p in parts]
        except ValueError:
            return None
        seconds = 0.0
        for n in nums:
            seconds = seconds * 60 + n
        return seconds

    @classmethod
    def _parse_timestamps(cls, raw: str | None) -> list[float]:
        if not raw:
            return []
        out = []
        for part in re.split(r"[,\n]+", str(raw)):
            ts = cls._parse_timestamp(part)
            if ts is not None and ts >= 0:
                out.append(ts)
            if len(out) >= cls.MAX_FRAMES:
                break
        return out

    @staticmethod
    def _format_ts(seconds: float) -> str:
        total = max(0, int(seconds))
        h, rem = divmod(total, 3600)
        m, s = divmod(rem, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

    async def _run_cmd(self, args: list[str], timeout: int = 60) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return 124, "", f"timed out after {timeout}s"
        return (
            proc.returncode or 0,
            stdout.decode("utf-8", "replace"),
            stderr.decode("utf-8", "replace"),
        )

    @staticmethod
    def _strip_vtt(raw: str) -> str:
        lines = []
        seen = set()
        for line in raw.splitlines():
            text = line.strip()
            if not text or text == "WEBVTT" or text.startswith(("Kind:", "Language:")):
                continue
            if "-->" in text or re.fullmatch(r"\d+", text):
                continue
            text = re.sub(r"<[^>]+>", "", text)
            text = re.sub(r"&amp;", "&", text)
            text = re.sub(r"&lt;", "<", text)
            text = re.sub(r"&gt;", ">", text)
            text = re.sub(r"\s+", " ", text).strip()
            if text and text not in seen:
                seen.add(text)
                lines.append(text)
        return "\n".join(lines)

    async def _download_transcript(self, url: str, lang: str, tmp: Path) -> str:
        direct = await self._download_timedtext(url, lang)
        if direct:
            return direct
        if not shutil.which("yt-dlp"):
            return ""
        out_tpl = str(tmp / "subs.%(ext)s")
        args = [
            "yt-dlp",
            "--skip-download",
            "--write-subs",
            "--write-auto-subs",
            "--sub-langs",
            f"{lang}.*,{lang},en.*",
            "--sub-format",
            "vtt",
            "-o",
            out_tpl,
            url,
        ]
        _code, _stdout, _stderr = await self._run_cmd(args, timeout=60)
        candidates = sorted(tmp.glob("subs*.vtt"), key=lambda p: p.stat().st_size, reverse=True)
        if not candidates:
            return ""
        return self._strip_vtt(candidates[0].read_text(encoding="utf-8", errors="replace"))

    async def _download_timedtext(self, url: str, lang: str) -> str:
        video_id = self._video_id(url)
        if not video_id:
            return ""
        session = await _get_shared_session()
        langs = [lang, "en"] if lang != "en" else ["en"]
        for lang_code in langs:
            for params in (
                {"v": video_id, "lang": lang_code, "fmt": "json3"},
                {"v": video_id, "lang": lang_code, "fmt": "srv3"},
            ):
                try:
                    async with session.get(
                        "https://www.youtube.com/api/timedtext",
                        params=params,
                        timeout=aiohttp.ClientTimeout(total=15),
                    ) as resp:
                        if resp.status != 200:
                            continue
                        raw = await _read_response_limited(resp, 2 * 1024 * 1024)
                except Exception:
                    continue
                text = raw.decode("utf-8", "replace").strip()
                if not text:
                    continue
                if params.get("fmt") == "json3":
                    try:
                        data = json.loads(text)
                        events = data.get("events", []) if isinstance(data, dict) else []
                        lines = []
                        for event in events:
                            segs = event.get("segs") if isinstance(event, dict) else None
                            if not isinstance(segs, list):
                                continue
                            line = "".join(str(seg.get("utf8", "")) for seg in segs if isinstance(seg, dict))
                            line = re.sub(r"\s+", " ", line).strip()
                            if line:
                                lines.append(line)
                        if lines:
                            return "\n".join(lines)
                    except Exception:
                        pass
                else:
                    text = re.sub(r"<[^>]+>", " ", text)
                    text = re.sub(r"\s+", " ", text).strip()
                    if text:
                        return text
        return ""

    async def _video_info(self, url: str) -> dict:
        fallback: dict = {}
        try:
            session = await _get_shared_session()
            async with session.get(
                "https://www.youtube.com/oembed",
                params={"url": url, "format": "json"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    raw = await _read_response_limited(resp, 256 * 1024)
                    data = json.loads(raw.decode("utf-8", "replace"))
                    if isinstance(data, dict):
                        fallback = {
                            "title": data.get("title"),
                            "uploader": data.get("author_name"),
                        }
        except Exception:
            fallback = {}
        if not shutil.which("yt-dlp"):
            return fallback
        code, stdout, _stderr = await self._run_cmd(
            ["yt-dlp", "--dump-json", "--no-playlist", url], timeout=45
        )
        if code != 0 or not stdout.strip():
            return fallback
        try:
            info = json.loads(stdout)
            if isinstance(info, dict):
                return {**fallback, **info}
            return fallback
        except json.JSONDecodeError:
            return fallback

    async def _extract_frames(self, url: str, timestamps: list[float], tmp: Path) -> list[str]:
        if not timestamps or not shutil.which("ffmpeg") or not shutil.which("yt-dlp"):
            return []
        code, stream_url, stderr = await self._run_cmd(
            [
                "yt-dlp",
                "-g",
                "--no-playlist",
                "-f",
                "bestvideo[height<=720]/best[height<=720]/best",
                url,
            ],
            timeout=45,
        )
        if code != 0 or not stream_url.strip():
            return [f"frame extraction unavailable: {stderr.strip()[:180] or 'no stream url'}"]
        video_url = stream_url.strip().splitlines()[0]
        sent = []
        for i, ts in enumerate(timestamps[: self.MAX_FRAMES], 1):
            frame_path = tmp / f"youtube_frame_{i}_{int(ts)}s.jpg"
            args = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                str(ts),
                "-i",
                video_url,
                "-frames:v",
                "1",
                "-q:v",
                "3",
                "-y",
                str(frame_path),
            ]
            code, _stdout, stderr = await self._run_cmd(args, timeout=40)
            if code != 0 or not frame_path.exists():
                sent.append(f"{self._format_ts(ts)} frame failed: {stderr.strip()[:120]}")
                continue
            try:
                encoded = base64.b64encode(frame_path.read_bytes()).decode("ascii")
                sent.append(
                    f"frame at {self._format_ts(ts)} attached for visual inspection\n"
                    f"__IMAGE_B64__{encoded}__END_IMAGE_B64__"
                )
            except Exception as e:
                sent.append(f"{self._format_ts(ts)} read failed: {e}")
        return sent

    async def execute(
        self,
        message: Message,
        url: str | None = None,
        timestamps: str | None = None,
        max_transcript_chars: str = "12000",
        lang: str = "en",
        **kwargs,
    ) -> str:
        if not url:
            return "Error: url is required"
        url = self._extract_youtube_url(url)
        if not self._is_youtube_url(url):
            return "Error: expected a YouTube URL"
        try:
            max_chars = max(1000, min(int(max_transcript_chars), self.MAX_TRANSCRIPT_CHARS))
        except (TypeError, ValueError):
            max_chars = 12000
        lang = re.sub(r"[^A-Za-z0-9_.-]", "", str(lang or "en"))[:20] or "en"
        requested_ts = self._parse_timestamps(timestamps)
        with tempfile.TemporaryDirectory(prefix="maxwell_yt_") as tmpdir:
            tmp = Path(tmpdir)
            info_task = asyncio.create_task(self._video_info(url))
            transcript = await self._download_transcript(url, lang, tmp)
            info = await info_task
            frame_results = await self._extract_frames(url, requested_ts, tmp)

        title = str(info.get("title") or "YouTube video")
        uploader = str(info.get("uploader") or info.get("channel") or "unknown")
        duration = info.get("duration")
        duration_text = self._format_ts(float(duration)) if isinstance(duration, (int, float)) else "unknown"
        parts = [f"Title: {title}", f"Channel: {uploader}", f"Duration: {duration_text}"]
        if transcript:
            if len(transcript) > max_chars:
                transcript = transcript[:max_chars] + "\n... (transcript truncated)"
            parts.append("Transcript:\n" + transcript)
        else:
            parts.append("Transcript: unavailable (no captions found or yt-dlp could not fetch them).")
        if requested_ts:
            parts.append("Frames: " + ("; ".join(frame_results) if frame_results else "requested but unavailable"))
        return "\n\n".join(parts)


class SendMemeTool(Tool):
    """Send a random meme from Reddit"""

    MEME_API = "https://meme-api.com/gimme"
    MAX_SIZE = 25 * 1024 * 1024

    def get_description(self):
        return (
            "Send a random meme from Reddit. Params: subreddit (optional, e.g. 'me_irl', 'dankmemes'). "
            "No params = random from r/memes."
        )

    async def execute(
        self, message: Message, subreddit: str | None = None, **kwargs
    ) -> str:
        url = self.MEME_API
        if subreddit:
            sub = subreddit.strip().removeprefix("r/")
            if not re.fullmatch(r"[A-Za-z0-9_]{2,21}", sub):
                return "Error: invalid subreddit name"
            url = f"{self.MEME_API}/{sub}"

        try:
            session = await _get_shared_session()
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    return f"Error: meme API returned {resp.status}"
                data = await resp.json()
        except Exception as e:
            return f"Error fetching meme: {e}"

        meme_url = data.get("url")
        title = data.get("title", "meme")
        sub = data.get("subreddit", "memes")
        ups = data.get("ups", 0)
        nsfw = data.get("nsfw", False)

        if nsfw:
            return "Error: got an NSFW meme, skipping"

        if not meme_url:
            return "Error: no meme URL in response"

        if not _is_safe_url(meme_url):
            return "Error: meme API returned an unsafe media URL"

        try:
            async with session.get(
                meme_url, timeout=aiohttp.ClientTimeout(total=30), allow_redirects=False
            ) as img_resp:
                if img_resp.status != 200:
                    return f"Error: could not download meme image ({img_resp.status})"
                img_bytes = await _read_response_limited(img_resp, self.MAX_SIZE)
        except Exception as e:
            return f"Error downloading meme: {e}"

        filename = meme_url.rsplit("/", 1)[-1].split("?")[0] or "meme.png"
        ext = os.path.splitext(filename)[1].lower()
        if ext not in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp4", ".webm"):
            filename += ".png"

        file = File(BytesIO(img_bytes), filename=filename)
        try:
            await message.reply(file=file)
        except discord.Forbidden:
            return "Error: no permission to send files here"
        except discord.HTTPException as e:
            return f"Error sending meme: {e}"

        return f'__MEME_SENT__ Sent meme: "{title}" from r/{sub} ({ups} upvotes)'


class SendMediaTool(Tool):
    """Send an image/video from a URL as a Discord attachment"""

    MAX_SIZE = 25 * 1024 * 1024

    def get_description(self):
        return (
            "Send an image/video URL as a Discord attachment. "
            "Params: url (required, direct link to media file)."
        )

    async def execute(self, message: Message, url: str | None = None, **kwargs) -> str:
        if not url:
            return "Error: url is required"

        if not _is_safe_url(url):
            return "Error: Cannot fetch from private/internal URLs"

        try:
            session = await _get_shared_session()
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=30), allow_redirects=False
            ) as resp:
                if resp.status != 200:
                    return f"Error: HTTP {resp.status}"
                media_bytes = await _read_response_limited(resp, self.MAX_SIZE)
        except asyncio.TimeoutError:
            return f"Error: timed out downloading {url}"
        except Exception as e:
            return f"Error downloading: {e}"

        filename = url.rsplit("/", 1)[-1].split("?")[0] or "media"
        ext = os.path.splitext(filename)[1].lower()
        if ext not in (
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
            ".webp",
            ".mp4",
            ".webm",
            ".weba",
            ".mp3",
        ):
            filename += ".png"

        file = File(BytesIO(media_bytes), filename=filename)
        try:
            await message.reply(file=file)
        except discord.Forbidden:
            return "Error: no permission to send files here"
        except discord.HTTPException as e:
            return f"Error sending media: {e}"

        return f"__MEDIA_SENT__ Sent media: {filename}"


# KiloTool removed — it was a host-level RCE escape hatch that bypassed
# the Docker sandbox. One prompt injection and the LLM owns your box.


class TtsTool(Tool):
    """Text to Speech generator tool"""

    def get_description(self):
        return (
            "Convert a text response into a speech voice message and send it to the triggering channel. "
            "Params: text (required string), language/lang (optional: english or spanish)."
        )

    async def execute(
        self,
        message: Message,
        text: str | None = None,
        language: str | None = None,
        lang: str | None = None,
        **kwargs,
    ) -> str:
        if not text or not text.strip():
            return "Error: text parameter is required"

        language_key = _tts_language_key(language, lang, **kwargs)
        lang_is_spanish = language_key == "spanish"

        import wave
        import os
        import discord

        # Determine API Key and Setup File
        bot_config = getattr(getattr(self, "bot", None), "config", None)
        nvidia_api_key = os.environ.get("NVIDIA_API_KEY", "") or getattr(
            bot_config, "NVIDIA_API_KEY", ""
        )
        filename = f"tts_{message.id}.wav"
        voice_filename = f"tts_{message.id}.ogg"

        try:
            # Try NVIDIA Riva TTS
            if not nvidia_api_key:
                raise RuntimeError("NVIDIA_API_KEY is not configured")

            import riva.client
            from riva.client.proto import riva_audio_pb2

            function_id = os.environ.get(
                "TTS_RIVA_FUNCTION_ID", "877104f7-e885-42b9-8de8-f6e4c6303969"
            )
            auth = riva.client.Auth(
                use_ssl=True,
                uri="grpc.nvcf.nvidia.com:443",
                metadata_args=[
                    ["function-id", function_id],
                    ["authorization", f"Bearer {nvidia_api_key}"],
                ],
                options=cast(
                    Any,
                    [
                        ("grpc.max_receive_message_length", 64 * 1024 * 1024),
                        ("grpc.max_send_message_length", 64 * 1024 * 1024),
                    ],
                ),
            )
            service = riva.client.SpeechSynthesisService(auth)

            tts_voice_name, tts_language_code = _tts_riva_voice_config(language_key)

            # Use gRPC service synchronously (run in executor since it is synchronous gRPC)
            def run_riva():
                return service.synthesize(
                    text=text,
                    voice_name=tts_voice_name,
                    language_code=tts_language_code,
                    sample_rate_hz=44100,
                    encoding=cast(Any, riva_audio_pb2).AudioEncoding.LINEAR_PCM,
                )

            loop = asyncio.get_running_loop()
            resp = await loop.run_in_executor(None, run_riva)
            logger.info(
                f"Riva TTS synthesized audio with voice={tts_voice_name!r}, language={tts_language_code!r}"
            )

            # Save the WAV file
            with wave.open(filename, "wb") as out_f:
                out_f.setnchannels(1)
                out_f.setsampwidth(2)
                out_f.setframerate(44100)
                out_f.writeframesraw(getattr(resp, "audio"))

        except Exception as e:
            logger.warning(f"Riva TTS synthesis failed: {e}. Falling back to gTTS.")
            # Fallback to local basic gTTS
            try:
                from gtts import gTTS

                def run_gtts():
                    tts = gTTS(text=text, lang="es" if lang_is_spanish else "en")
                    tts.save(filename)

                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, run_gtts)
                logger.warning(
                    "TTS used gTTS fallback; voice selection/emotion is unavailable in fallback audio"
                )
            except Exception as fallback_err:
                return f"Error: Riva TTS failed ({e}) and fallback gTTS failed ({fallback_err})"

        async def make_voice_ogg(source: str) -> str:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                source,
                "-vn",
                "-ac",
                "1",
                "-ar",
                "48000",
                "-c:a",
                "libopus",
                "-b:a",
                "32k",
                voice_filename,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                logger.warning("TTS OGG conversion timed out")
                return source
            if proc.returncode == 0 and os.path.exists(voice_filename):
                return voice_filename
            logger.warning(
                f"Failed to convert TTS to voice OGG: {stderr.decode(errors='replace')[-300:]}"
            )
            return source

        async def get_audio_duration(source: str) -> float:
            proc = await asyncio.create_subprocess_exec(
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                source,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return 1.0
            if proc.returncode != 0:
                return 1.0
            try:
                return max(0.1, float(stdout.decode().strip()))
            except ValueError:
                return 1.0

        async def make_waveform(source: str) -> str:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                source,
                "-f",
                "s16le",
                "-ac",
                "1",
                "-ar",
                "8000",
                "pipe:1",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return base64.b64encode(bytes([128] * 256)).decode("ascii")
            if proc.returncode != 0 or len(stdout) < 2:
                return base64.b64encode(bytes([128] * 256)).decode("ascii")

            sample_count = len(stdout) // 2
            bucket_size = max(1, sample_count // 256)
            waveform = bytearray()
            for bucket_start in range(
                0, min(sample_count, bucket_size * 256), bucket_size
            ):
                bucket_end = min(sample_count, bucket_start + bucket_size)
                peak = 0
                for sample_index in range(bucket_start, bucket_end):
                    byte_index = sample_index * 2
                    sample = int.from_bytes(
                        stdout[byte_index : byte_index + 2], "little", signed=True
                    )
                    peak = max(peak, abs(sample))
                waveform.append(min(255, int(peak / 32767 * 255)))

            if len(waveform) < 256:
                waveform.extend([0] * (256 - len(waveform)))
            return base64.b64encode(bytes(waveform[:256])).decode("ascii")

        async def send_discord_voice_message(source: str):
            from discord.flags import MessageFlags
            from discord.http import handle_message_parameters

            class VoiceMessageFile(discord.File):
                def __init__(self, fp, filename: str, duration: float, waveform: str):
                    super().__init__(fp, filename=filename)
                    self._duration = duration
                    self._waveform = waveform

                def to_dict(self, index: int):
                    payload = super().to_dict(index)
                    payload["duration_secs"] = self._duration
                    payload["waveform"] = self._waveform
                    return payload

            channel = message.channel
            state = getattr(channel, "_state", getattr(message, "_state", None))
            if state is None or not hasattr(state, "http"):
                raise RuntimeError("Discord message state is unavailable")

            flags = MessageFlags._from_value(0)
            flags.voice = True
            duration = await get_audio_duration(source)
            waveform = await make_waveform(source)
            voice_file = VoiceMessageFile(
                source,
                filename="voice-message.ogg",
                duration=duration,
                waveform=waveform,
            )
            with handle_message_parameters(file=voice_file, flags=flags) as params:
                await state.http.send_message(channel.id, params=params)

        # Send as voice-style audio. Telegram adapters use sendVoice; Discord needs a voice flag plus waveform metadata.
        if os.path.exists(filename):
            send_path = filename
            try:
                send_path = await make_voice_ogg(filename)
                if hasattr(message, "send_voice_file"):
                    await cast(Any, message).send_voice_file(send_path)
                else:
                    await send_discord_voice_message(send_path)
                return "__NO_RESPONSE__"
            except Exception as discord_err:
                return f"Error sending TTS voice message to channel: {discord_err}"
            finally:
                for path in {filename, voice_filename}:
                    if os.path.exists(path):
                        try:
                            os.remove(path)
                        except Exception:
                            pass
        else:
            return f"Error: Audio file {filename} was not generated"


class LeaveVcTool(Tool):
    """Leave the active voice channel"""

    def get_description(self):
        return (
            "Immediately disconnect from the active voice channel in this server. "
            "Use this when the user or conversation indicates that you should leave the voice channel."
        )

    async def execute(self, message: Message, **kwargs) -> str:
        if not message.guild:
            return "Error: This tool can only be used within a server/guild."
        vc = None
        for client in self.bot.voice_clients:
            if client.guild.id == message.guild.id:
                vc = client
                break
        if not vc or not vc.is_connected():
            return "Error: I am not currently connected to any voice channel in this server."
        try:
            if hasattr(self.bot, "_vc_stop_listening"):
                await self.bot._vc_stop_listening(
                    message.guild, vc.channel, message.channel
                )
            await vc.disconnect(force=True)
            return "Successfully disconnected from the voice channel."
        except Exception as e:
            return f"Error leaving voice channel: {e}"
