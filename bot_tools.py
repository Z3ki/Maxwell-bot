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
import socket
import tempfile
from pathlib import Path
from typing import Any, cast
import contextlib
import html
import ssl
import wave

import asyncio
import base64
import discord
import aiohttp
import aiofiles
import logging
import random
from datetime import datetime, timezone, timedelta
from io import BytesIO
from urllib.parse import parse_qs, urlparse

from discord import Message, File, Activity, Status
from tools import Tool
from ddgs import DDGS as _DDGS
from utils import (  # single source of truth, fd-safe
    FileLock,
    _atomic_json_write_sync,
)
from subagent_runner import run_subagent_task

logger = logging.getLogger(__name__)

# Owner IDs come from env var only — no hardcoded defaults to leak in open-source.
# Load dotenv first so bare `python bot.py` sees MAXWELL_OWNER_IDS from .env
# (config.py also loads dotenv; this avoids import-order freezing empty OWNER_IDS).
try:
    from dotenv.main import load_dotenv as _load_dotenv_early
    from pathlib import Path as _PathEarly

    _load_dotenv_early(
        _PathEarly(
            os.getenv(
                "MAXWELL_ENV_FILE", _PathEarly(__file__).resolve().parent / ".env"
            )
        ),
        override=False,
    )
except Exception:
    pass

OWNER_IDS = {
    item.strip()
    for item in os.environ.get("MAXWELL_OWNER_IDS", "").split(",")
    if item.strip()
}


def refresh_owner_ids() -> set[str]:
    """Re-read MAXWELL_OWNER_IDS from the environment (e.g. after dotenv)."""
    global OWNER_IDS
    OWNER_IDS = {
        item.strip()
        for item in os.environ.get("MAXWELL_OWNER_IDS", "").split(",")
        if item.strip()
    }
    return OWNER_IDS


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
    # Unwrap IPv4-mapped IPv6 (::ffff:127.0.0.1) so loopback/private checks apply.
    if getattr(ip, "ipv4_mapped", None) is not None:
        ip = ip.ipv4_mapped
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
                resolver=cast(Any, _SafeResolver()),
                limit=30,
                limit_per_host=5,
                force_close=True,
            )
            _SHARED_SESSION = aiohttp.ClientSession(connector=connector)
        return _SHARED_SESSION


async def _recreate_shared_session():
    global _SHARED_SESSION
    async with _SESSION_LOCK:
        if _SHARED_SESSION is not None and not _SHARED_SESSION.closed:
            try:
                await _SHARED_SESSION.close()
            except Exception:
                pass
        connector = aiohttp.TCPConnector(
            resolver=cast(Any, _SafeResolver()),
            limit=30,
            limit_per_host=5,
            force_close=True,
        )
        _SHARED_SESSION = aiohttp.ClientSession(connector=connector)
        return _SHARED_SESSION


async def close_shared_session():
    global _SHARED_SESSION
    async with _SESSION_LOCK:
        if _SHARED_SESSION is not None and not _SHARED_SESSION.closed:
            try:
                await _SHARED_SESSION.close()
            except Exception:
                pass
        _SHARED_SESSION = None


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


def _strip_heredoc_blocks(command: str) -> str:
    """Return `command` with heredoc bodies removed.

    A heredoc looks like `... << 'EOF'` (or `<< "EOF"` / `<<EOF`) followed by
    lines of literal content ending with a line containing only the delimiter
    `EOF`. The literal block is the only place we permit newlines, so stripping
    it lets us validate the remaining (non-heredoc) parts as a single line.
    """
    out: list[str] = []
    i = 0
    lines = command.split("\n")
    while i < len(lines):
        line = lines[i]
        # Heredoc opener: find `<<` then the delimiter token. Anchor on the
        # unquoted start of the line; backtracking-safe.
        idx = line.find("<<")
        if idx >= 0:
            tail = line[idx + 2 :]
            stripped = tail.strip()
            m = re.match(r"^(['\"]?)([A-Za-z0-9_]+)\1\s*$", stripped)
            if m:
                delimiter = m.group(2)
                i += 1
                # Skip until we hit the closing delimiter on its own line.
                while i < len(lines) and lines[i].strip() != delimiter:
                    i += 1
                # Discard the closing delimiter line itself.
                i += 1
                continue
        out.append(line)
        i += 1
    return "\n".join(out)


def _is_path_allowed(path: str, allowed_base: str) -> bool:
    """Return True if `path` resolves to a regular file under `allowed_base`.

    Blocks path traversal, absolute escapes, and symlinks that point outside
    the allowed directory. Used to stop LLM-driven file reads.
    """
    if not path or not isinstance(path, str):
        return False
    try:
        base = Path(allowed_base).resolve()
        target = Path(path).resolve()
        if not target.is_file():
            return False
        # is_relative_to rejects .. escapes and symlinks outside base
        return target.is_relative_to(base)
    except (OSError, ValueError):
        return False


def _safe_attachment_filename(name: str | None, default: str = "attachment") -> str:
    """Return a safe Discord attachment filename.

    Strips path components, control characters, and leading dots, then limits
    length. Keeps the original extension when possible.
    """
    raw = str(name or default).strip()
    # Take only the final path segment and strip any query/fragment junk
    raw = Path(raw).name
    # Remove control chars and anything that isn't a safe filename character
    raw = re.sub(r"[^A-Za-z0-9._-]", "_", raw)
    # Collapse repeated separators
    raw = re.sub(r"[._-]{2,}", "_", raw)
    # Avoid hidden files and names that are only dots/separators
    raw = raw.lstrip(".")
    if not raw or raw in {"", ".", ".."}:
        raw = default
    # Limit total length; reserve space for any suffix the caller may add
    max_len = 80
    if len(raw) > max_len:
        stem, ext = os.path.splitext(raw)
        raw = stem[: max_len - len(ext)] + ext
    return raw


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
                    # Step aside for the live progress message before we
                    # post the image — the user should see the artifact,
                    # not "running image_generator" anymore.
                    self._signal_streaming(message)
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
                    result += "If it looks bad, call image_generator again with an improved prompt. "
                    result += "If you were generating this for a site, call create_site NOW (in your next response) with the URL embedded in the body — do not call create_site before image_generator returns this URL."
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
            except aiohttp.ClientError as e:
                logger.warning(
                    f"NVIDIA image connection error (attempt {attempt + 1}/{max_retries}): {e}"
                )
                if "Server disconnected" in str(e) or "Connection" in str(e):
                    session = await _recreate_shared_session()
                last_error = (
                    "Error generating image: connection failed. Try again later."
                )
                if attempt < max_retries - 1:
                    wait_time = (attempt + 1) * 10
                    await asyncio.sleep(wait_time)
                    continue
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
        # Step aside for the live progress message — the HD image is
        # the user-visible result; the "running hd_image" status is
        # redundant the moment the upload starts.
        self._signal_streaming(message)
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
        if self.bot and not self.bot._is_admin(message.author.id):
            return "Error: memory_edit is admin-only"
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
        # Silent: no DM, no channel echo, no LLM-visible text. The status
        # change is already visible on the bot's profile. Returning "" tells
        # the LLM not to send_message about it either.
        return ""


class SetActivityTool(Tool):
    """Set bot activity/custom status"""

    def get_description(self):
        return (
            "Set your activity or custom status message (the visible text under your name).\n"
            "CALL SPARINGLY — this is a low-value, high-noise tool. Default to NOT calling it. "
            "Only call when: (a) the user explicitly asks you to change your status, activity, "
            "vibe, or what you're doing; (b) you just finished a significant task (build, ship, "
            "deploy, fix) and the status would genuinely reflect the new state; (c) you joined "
            "a voice call or started a long-running operation worth surfacing. "
            "Do NOT call on every turn, every reply, or every topic shift. Do NOT call to "
            "'match the vibe' of a casual message — your custom status is not a live ticker. "
            "If you've set a similar status in the last few turns, skip the call. "
            "Params: type (playing/watching/listening/competing/custom), text (the status text), "
            "elapsed (optional, e.g. '2h 30m'). Use type='custom' for a plain status. "
            "Keep text short, lowercase, in-character. Call with text='' to clear."
        )

    def _parse_elapsed(self, elapsed: str) -> int:
        total_ms = 0
        for match in re.finditer(r"(\d+)\s*(h|m|s|d)", elapsed.lower()):
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
            # Silent: the cleared status is already visible on the profile.
            # No DM, no channel echo, no LLM-visible text.
            return ""

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
        # Silent: the new status is already visible on the profile. No DM,
        # no channel echo, no LLM-visible text — the user can see it
        # themselves without the bot narrating the change.
        return ""


class SleepTool(Tool):
    """Take a sleep window. While sleeping the bot won't dispatch
    LLM turns — anyone who pings or DMs gets a 'max is sleeping,
    back in Xm' notification (deduped per user). The 2026-07-19 user
    directive: the bot kept spamming goodnight/goodbye in chat; a
    real sleep window is the structural fix. Use this when the
    conversation is genuinely winding down — not as a generic
    goodbye."""

    is_destructive: bool = False
    streams_output: bool = False

    def get_description(self):
        return (
            "Take a sleep window (1-60 minutes). While sleeping, the bot "
            "won't dispatch any LLM turns; pings and DMs get a single "
            "'max is sleeping, back in ~Xm' notice. Use this when the "
            "conversation is genuinely done (the user said goodnight, or "
            "it's a natural end-of-day lull) — NOT as a way to add a "
            "goodbye to a normal reply. The 2026-07-19 user complaint was "
            "that the bot kept signing off unnecessarily; the sleep tool "
            "is the actual off-switch when a real rest is warranted. "
            "Params: duration_minutes (1-60, default 30). The max is "
            "enforced server-side. Calling again resets the window."
        )

    async def execute(
        self,
        message: Message,
        duration_minutes: int | str = 30,
        **kwargs,
    ) -> str:
        # Defensive parse — the model may emit a string.
        try:
            n = int(duration_minutes)
        except (TypeError, ValueError):
            n = 30
        if n < 1:
            n = 1
        if n > 60:
            n = 60
        if self.bot is None:
            return "Error: bot not attached, cannot sleep"
        return self.bot.set_sleep(n)


class ClearSleepTool(Tool):
    """Cancel an active sleep window. Idempotent — safe to call when
    not sleeping. Use when the bot decided to sleep but the user
    immediately needs a reply."""

    is_destructive: bool = False
    streams_output: bool = False

    def get_description(self):
        return (
            "Cancel the active sleep window and wake up immediately. "
            "Use sparingly — only when you called sleep and the user "
            "unexpectedly pings right after. The 2026-07-19 user note: "
            "the bot said 'goodnight, sleeping 30m' and the user said "
            "'wait no I have a question'."
        )

    async def execute(self, message: Message, **kwargs) -> str:
        if self.bot is None:
            return "Error: bot not attached"
        return self.bot.clear_sleep()


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

            hours = int(duration_hours)
            if hours < 1 or hours > 168:
                return "Error: duration_hours must be between 1 and 168"
            poll = discord.Poll(
                question=question,
                duration=timedelta(hours=hours),
            )
            for opt in option_list:
                poll.add_answer(text=opt)

            # Step aside for the live progress message before posting
            # the poll. The poll itself is the user-visible action.
            self._signal_streaming(message)
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
        if self.bot and not self.bot._is_admin(message.author.id):
            return "Error: set_nickname is admin-only"
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
        if self.bot and not self.bot._is_admin(message.author.id):
            return "Error: forward_message is admin-only"
        if not message_id or not channel_id:
            return "Error: message_id and channel_id are required"
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
        if self.bot and not self.bot._is_admin(message.author.id):
            return "Error: list_servers is admin-only"
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
        if self.bot and not self.bot._is_admin(message.author.id):
            return "Error: list_admin_servers is admin-only"
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
        return "Change your profile picture. Params: url (required, direct image URL jpg/png/gif/webp). Local cooldown is disabled by default (AVATAR_COOLDOWN_SECONDS env); Discord's own rate limit still applies."

    async def execute(self, message: Message, url: str | None = None, **kwargs) -> str:
        if self.bot and not self.bot._is_admin(message.author.id):
            return "Error: change_avatar is admin-only"
        if not url:
            return "Error: url is required"

        if not _is_safe_url(url):
            return "Error: Cannot fetch from private/internal URLs"

        # Cooldown is env-driven so we can disable it (Discord's own API
        # rate limit still applies — you'll get a 429 from Discord instead
        # of a friendly local error if you spam it). Default 0 = off.
        try:
            cooldown = int(os.environ.get("AVATAR_COOLDOWN_SECONDS", "0"))
        except ValueError:
            cooldown = 0

        if cooldown > 0 and self.bot._last_avatar_change:
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

    MAX_CONTENT_SIZE = 3000000  # 3MB for big single-file 3D scenes, full movie recreations, complex interactive demos etc. (use base64 encoding in tool call for safety)

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
            "Params: name (short slug), title (headline), body (FULL HTML document — write complete "
            "<!DOCTYPE html> with all CSS/JS inline, written as-is with no template wrapping), "
            "encoding (text|base64, default text). Inline <script>/<style> run; external https CDNs "
            "(cdnjs, jsdelivr, unpkg, Google Fonts) load. Use this for full websites, apps, games, "
            "calculators, demos, portfolios — anything interactive. Supports Discord server widgets "
            '(<iframe src="https://discord.com/widget?id=GUILD_ID">; widget must be enabled in '
            "Server Settings > Widget), YouTube/Vimeo/Twitch/Spotify/SoundCloud <iframe> embeds, "
            'external images/video/audio, model-viewer/Chart.js/D3, etc. Put <meta property="og:title">, '
            'og:description, og:image (absolute https URL), og:url, <meta name="theme-color">, '
            '<meta name="twitter:card" content="summary_large_image">, <link rel="icon">, and '
            "<title> in <head> so the link unfurls with a rich preview when shared in Discord.\n\n"
            "IMAGE ORDERING: if the site needs images, call image_generator (or hd_image) FIRST in a "
            "separate turn and wait for the Discord CDN URL. Do NOT batch create_site with image_generator. "
            "Plain text/CSS sites don't need images — call create_site once."
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
        # Available to everyone (non-admins too). Quota + ownership checks apply.
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
        is_admin = bool(self.bot and self.bot._is_admin(message.author.id))
        if hasattr(self.bot, "_load_sites"):
            self.bot._load_sites(quiet=True)
        sites = self.bot._sites

        # Block slug takeover: only owner or admin may overwrite an existing site.
        existing = sites.get(slug) if isinstance(sites, dict) else None
        if isinstance(existing, dict):
            owner = str(existing.get("user_id") or "")
            if owner and owner != user_id and not is_admin:
                return (
                    f"Error: site slug '{slug}' is already owned by another user. "
                    "Pick a different name."
                )

        control = (
            getattr(self.bot, "control", {}) or getattr(self.bot, "_control", {}) or {}
        )
        max_sites = int(control.get("create_site_quota_per_user", 10))
        active_user_sites = [s for s in sites.values() if s.get("user_id") == user_id]
        if len(active_user_sites) >= max_sites:
            return f"Error: site quota reached ({len(active_user_sites)}/{max_sites} active sites). Delete an old site first."

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
                # Reuse the same broad-but-safe allowlist as SendFileTool so
                # images produced by image_generator (Discord CDN downloads)
                # and the shell sandbox (shelldocker) / subagents can actually be
                # embedded. The old check only allowed MAXWELL_SITE_DIR, which
                # rejected virtually every real image source (the feature was
                # silently non-functional).
                send_tool = self.bot.tools.get("send_file") if self.bot else None
                if send_tool is not None and hasattr(
                    send_tool, "_allowed_send_file_bases"
                ):
                    allowed_bases = send_tool._allowed_send_file_bases()
                else:
                    allowed_bases = [self.base_dir]
                for entry in image_list:
                    if isinstance(entry, str):
                        entry = {"path": entry}
                    src_path = entry.get("path", "")
                    if not src_path or not any(
                        _is_path_allowed(src_path, b) for b in allowed_bases
                    ):
                        missing_images.append(src_path or "(empty path)")
                        logger.warning(f"Site image blocked or not found: {src_path}")
                        continue
                    filename = entry.get("filename") or os.path.basename(src_path)
                    # Sanitize filename: only safe chars, and strip path
                    # separators / leading dots so ".." can't write outside
                    # the images/ dir.
                    filename = re.sub(r"[^a-zA-Z0-9._-]", "_", filename).strip(".")
                    filename = re.sub(r"^[.\\/-]+", "", filename)
                    if not filename or filename in {".", ".."}:
                        filename = "image"
                    dest = os.path.join(img_dir, filename)
                    # Final guard: ensure dest stays inside img_dir.
                    if os.path.commonpath(
                        [os.path.abspath(dest), os.path.abspath(img_dir)]
                    ) != os.path.abspath(img_dir):
                        missing_images.append(src_path)
                        logger.warning(
                            f"Site image filename escapes images dir: {filename}"
                        )
                        continue
                    try:
                        shutil.copy2(src_path, dest)
                        public_url = f"{self.base_url}/{slug}/images/{filename}"
                        image_urls.append(public_url)
                        logger.info(f"Copied site image {src_path} -> {dest}")
                    except Exception as e:
                        logger.warning(f"Failed to copy image {src_path}: {e}")

            index_path = os.path.join(site_dir, "index.html")
            # Inject a permissive CSP meta tag. The whole point of create_site is
            # letting the model write complete, functional HTML pages with inline
            # <script> and <style>, external CDN libraries (fonts, frameworks),
            # and arbitrary images. The old CSP blocked script-src to 'self' only,
            # which silently broke every JS-bearing page the tool was built to
            # produce. Per the README security model, generated sites are arbitrary
            # HTML served on a SEPARATE origin from admin pages, so XSS risk to
            # admin credentials is already mitigated at the hosting layer.
            # 'unsafe-inline' covers both script and style; data: URIs cover inline
            # SVG/embedded assets; https: allows CDNs without listing each host.
            if "<head" in body.lower():
                head_match = re.search(r"<head[^>]*>", body, re.IGNORECASE)
                if head_match and re.search(
                    r"http-equiv\s*=\s*[\"']?Content-Security-Policy",
                    body,
                    re.IGNORECASE,
                ):
                    csp_meta = (
                        ""  # page already declares its own CSP; don't double-inject
                    )
                else:
                    csp_meta = (
                        '<meta http-equiv="Content-Security-Policy" '
                        'content="default-src https: data: blob:; '
                        "img-src https: data: blob:; "
                        "style-src 'unsafe-inline' https:; "
                        "script-src 'unsafe-inline' 'unsafe-eval' https:; "
                        "font-src https: data:; "
                        "connect-src https:; "
                        'media-src https: data: blob:;">'
                    )
                if csp_meta:
                    body = re.sub(
                        r"(<head[^>]*>)",
                        r"\1\n" + csp_meta,
                        body,
                        count=1,
                        flags=re.IGNORECASE,
                    )
            elif "<html" in body.lower():
                csp_meta = (
                    '<meta http-equiv="Content-Security-Policy" '
                    'content="default-src https: data: blob:; '
                    "img-src https: data: blob:; "
                    "style-src 'unsafe-inline' https:; "
                    "script-src 'unsafe-inline' 'unsafe-eval' https:; "
                    "font-src https: data:; "
                    "connect-src https:; "
                    'media-src https: data: blob:;">'
                )
                body = re.sub(
                    r"(<html[^>]*>)",
                    r"\1\n<head>" + csp_meta + "</head>",
                    body,
                    count=1,
                    flags=re.IGNORECASE,
                )
            else:
                csp_meta = (
                    '<meta http-equiv="Content-Security-Policy" '
                    'content="default-src https: data: blob:; '
                    "img-src https: data: blob:; "
                    "style-src 'unsafe-inline' https:; "
                    "script-src 'unsafe-inline' 'unsafe-eval' https:; "
                    "font-src https: data:; "
                    "connect-src https:; "
                    'media-src https: data: blob:;">'
                )
                body = "<head>" + csp_meta + "</head>\n" + body
            # Atomic write for the public HTML to avoid truncated/orphan sites on
            # crash, OOM, or concurrent overwrite (reliability fix per persistence review).
            tmp_path = index_path + ".tmp"
            async with aiofiles.open(tmp_path, "w", encoding="utf-8") as f:
                await f.write(body)
                await f.flush()
            os.replace(tmp_path, index_path)

            # Commit the site metadata under a cross-process FileLock so a
            # concurrent create_site (or an API site_update/site_delete) can't
            # lose this entry or have this entry overwrite theirs. Reload fresh
            # inside the lock and re-check ownership/quota (they may have
            # changed since the pre-check). If the save fails, remove the
            # just-written HTML so we don't leave an untracked orphan site.
            site_entry = {
                "user_id": user_id,
                "user_name": message.author.display_name,
                "created_at": datetime.now(timezone.utc).timestamp(),
                "title": title,
                "path": site_dir,
            }
            try:
                committed = await asyncio.to_thread(
                    self._commit_site_locked, slug, user_id, is_admin, site_entry
                )
            except Exception as e:
                # Best-effort cleanup of the orphaned live HTML we just published.
                with contextlib.suppress(Exception):
                    import shutil

                    shutil.rmtree(site_dir, ignore_errors=True)
                logger.error(f"Failed to commit site metadata for {slug}: {e}")
                return f"Error creating site: {e}"
            if not committed:
                # Overwrite disallowed by a concurrent owner change / quota hit
                # discovered under the lock; clean up the HTML we wrote.
                with contextlib.suppress(Exception):
                    import shutil

                    shutil.rmtree(site_dir, ignore_errors=True)
                return (
                    f"Error: site slug '{slug}' could not be committed "
                    "(owner/quota changed concurrently). Try again."
                )
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

    def _commit_site_locked(
        self, slug: str, user_id: str, is_admin: bool, entry: dict
    ) -> bool:
        """Reload sites.json under a cross-process lock, re-check ownership and
        quota, add the entry, and save atomically. Returns True on commit.

        Runs in a worker thread (via asyncio.to_thread) because FileLock uses
        blocking fcntl. This is the single locked RMW for create_site metadata,
        closing the lost-update race with the API process and concurrent
        creates.
        """
        path = Path(self.bot.config.DATA_DIR) / "sites.json"
        max_sites = int(
            (
                getattr(self.bot, "control", {})
                or getattr(self.bot, "_control", {})
                or {}
            ).get("create_site_quota_per_user", 10)
        )
        with FileLock(path, timeout=15.0):
            sites = {}
            try:
                if path.exists():
                    data = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        sites = {k: v for k, v in data.items() if isinstance(v, dict)}
            except (json.JSONDecodeError, OSError, ValueError) as e:
                logger.warning(f"Corrupt sites.json on commit, starting fresh: {e}")
                sites = {}
            # Re-check slug ownership under the lock (may have changed).
            existing = sites.get(slug)
            if isinstance(existing, dict):
                owner = str(existing.get("user_id") or "")
                if owner and owner != user_id and not is_admin:
                    return False
            # Re-check quota under the lock.
            active = [s for s in sites.values() if s.get("user_id") == user_id]
            # If this slug is already ours (overwrite), it doesn't count as new.
            already_ours = (
                isinstance(existing, dict)
                and str(existing.get("user_id") or "") == user_id
            )
            if not already_ours and len(active) >= max_sites:
                return False
            sites[slug] = entry
            _atomic_json_write_sync(path, sites)
            # Keep the in-memory map + mtime in sync for this process.
            self.bot._sites = sites
            try:
                self.bot._sites_mtime = path.stat().st_mtime
            except OSError:
                pass
            return True

    async def _save_sites(self):
        try:
            path = Path(self.bot.config.DATA_DIR) / "sites.json"

            # Cross-process lock so the API's site_update/site_delete and this
            # write can't interleave and lose an entry.
            def _locked_write():
                with FileLock(path, timeout=15.0):
                    _atomic_json_write_sync(path, self.bot._sites)
                    return path.stat().st_mtime if path.exists() else 0.0

            mtime = await asyncio.to_thread(_locked_write)
            if hasattr(self.bot, "_sites_mtime"):
                self.bot._sites_mtime = mtime
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

        # Web search returns untrusted content. Mark the current turn as
        # tainted so subsequent destructive tools (shell, sub_agent) prompt
        # for confirmation. This is the second line of defense against
        # indirect prompt injection from search snippets.
        if self.bot is not None and hasattr(self.bot, "mark_message_tainted"):
            self.bot.mark_message_tainted(message)

        try:
            loop = asyncio.get_running_loop()
            # Bound the search: DDGS uses sync requests internally with no
            # timeout, so a hung endpoint would block this tool and occupy a
            # default-executor thread indefinitely.
            results = await asyncio.wait_for(
                loop.run_in_executor(
                    None, lambda: list(_DDGS().text(query, max_results=limit))
                ),
                timeout=30,
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
    """Create and send an arbitrary file attachment, or send an existing file from disk."""

    MAX_SIZE = 25 * 1024 * 1024

    def get_description(self):
        return (
            "Create a file with any filename/extension and send it as an attachment, "
            "or send a file already on disk. "
            "Use this for .txt, .py, .json, .html, binary files, etc. "
            "For code/HTML/JSON or exact file bytes, prefer encoding=base64 so markup/backticks are preserved exactly. "
            "Params: filename (required when using content), content (required when creating inline), "
            "path (optional: absolute path to an existing file on disk to send), "
            "encoding (optional: text or base64; default text). "
            "When path is given, the file is read from a safe directory (data, sites, subagents, shell workdir) and sent directly."
        )

    async def execute(
        self,
        message: Message,
        filename: str | None = None,
        content: str | None = None,
        encoding: str = "text",
        path: str | None = None,
        **kwargs,
    ) -> str:
        if self.bot and not self.bot._is_admin(message.author.id):
            return "Error: send_file is admin-only"
        # Path mode: send a file that already exists on disk (or in the shell
        # container — we docker-cp it out as a fallback for container paths).
        if path:
            # Normalize container paths (/home/maxwell/...) to the host bind
            # mount so the allowlist and resolver see a real host path.
            resolved_input = self._resolve_send_file_path(path)
            # First, the fast path: a regular host file the model knows about.
            host_path, host_error = await self._try_read_host_file(resolved_input)
            if host_path is not None:
                target = host_path
            else:
                # Fallback: the model passed a container-only path (anything
                # inside the maxwell-shell container). Try docker cp it out.
                # Allowed for any path inside the container — the model
                # already has shell access, and refusing "any file" creates
                # an artificial one-step barrier that breaks the round-trip.
                target, cp_error = await self._docker_cp_from_shell(path)
                if target is None:
                    return (
                        f"Error: could not read file at '{path}'. "
                        f"Host: {host_error or 'not found'}. "
                        f"Container: {cp_error or 'not found or not readable'}."
                    )

            try:
                blob = await asyncio.to_thread(target.read_bytes)
            except Exception as e:
                return f"Error reading file from disk: {e}"
            # Always sanitize the outbound attachment name (never trust filename=).
            safe_name = _safe_attachment_filename(
                filename or target.name, default="file"
            )
            return await self._send_blob(message, blob, safe_name)

        # Inline-content mode (original behavior).
        if not filename or not str(filename).strip():
            return "Error: filename is required"
        if content is None:
            return "Error: content is required"

        safe_name = _safe_attachment_filename(filename, default="file")
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

        return await self._send_blob(message, blob, safe_name)

    def _allowed_send_file_bases(self) -> list[str]:
        # Do NOT allow the full data/ tree (admins.json, cookies, traces, etc.).
        # Only export-safe subtrees and workspace dirs the tools themselves create.
        bases: list[str] = []
        data_dir = os.path.abspath(
            getattr(
                getattr(getattr(self, "bot", None), "config", None), "DATA_DIR", "data"
            )
            or "data"
        )
        for sub in ("exports", "public_files", "attachments"):
            bases.append(os.path.join(data_dir, sub))
        site_dir = getattr(getattr(self, "bot", None), "config", None)
        if site_dir:
            site_path = getattr(site_dir, "MAXWELL_SITE_DIR", "")
            if site_path:
                bases.append(os.path.abspath(site_path))
        subagent_base = os.environ.get("OPENCODE_SUBAGENT_BASE_DIR", "subagents")
        bases.append(os.path.abspath(subagent_base))
        # Shell tool working dir (volume mounted into container as /home/maxwell).
        shell_host = os.path.join(os.path.dirname(__file__), "shelldocker")
        bases.append(os.path.abspath(shell_host))
        return bases

    def _resolve_send_file_path(self, raw_path: str) -> str:
        """Map a path the model might pass to the actual host path.

        Accepts both forms:
          * host paths: /root/maxwell/shelldocker/foo.png (or any allowed base)
          * container paths: /home/maxwell/foo.png  -> shelldocker/foo.png

        Returns the resolved absolute host path, or the original input if no
        remap is needed (let the existing _is_path_allowed check decide).
        """
        cleaned = str(raw_path or "").strip()
        if not cleaned:
            return cleaned
        # Normalize container-side /home/maxwell/<x> to the host bind mount.
        # Match /home/maxwell, /home/maxwell/, or just home/maxwell (defensive).
        m = re.match(r"^/?home/maxwell/?(.*)$", cleaned)
        if m:
            shell_host = os.path.abspath(
                os.path.join(os.path.dirname(__file__), "shelldocker")
            )
            rel = m.group(1).lstrip("/")
            return os.path.join(shell_host, rel) if rel else shell_host
        return cleaned

    async def _try_read_host_file(
        self, resolved_path: str
    ) -> tuple[Path | None, str | None]:
        """Read a file from the host if it exists in an allowed base.

        Returns (Path, None) on success, (None, error_string) on miss.
        """
        allowed_bases = self._allowed_send_file_bases()
        for base in allowed_bases:
            if _is_path_allowed(resolved_path, base):
                try:
                    p = Path(resolved_path).resolve()
                    if p.is_file():
                        return p, None
                except OSError:
                    continue
        return None, "not in an allowed host directory or not found"

    async def _docker_cp_from_shell(
        self, container_path: str
    ) -> tuple[Path | None, str | None]:
        """docker-cp a file out of the maxwell-shell container to a local temp
        path, then return that local Path. Used as a fallback when the model
        passes a path that only exists inside the container.

        Path safety: we only allow reads from inside the running
        maxwell-shell container. The container's root is bounded by the
        sandbox flags (no host FS mount by default; even in MAXWELL_SHELL_FULL_HOST
        mode, /host is a separate root).
        """
        if not container_path or not isinstance(container_path, str):
            return None, "empty path"
        clean = container_path.strip()
        if not clean.startswith("/"):
            clean = "/" + clean  # require absolute inside container
        # No traversal escapes from the container root; this is read-only.
        if ".." in clean.split("/"):
            return None, "path traversal not allowed"

        # Confirm the container is running.
        try:
            shell_tool = self.bot.tools.get("shell") if self.bot else None
            container_name = (
                getattr(shell_tool, "CONTAINER_NAME", "maxwell-shell")
                if shell_tool
                else "maxwell-shell"
            )
        except Exception:
            container_name = "maxwell-shell"

        tmp_dir = tempfile.mkdtemp(prefix="maxwell_sendfile_")
        local_path = os.path.join(tmp_dir, os.path.basename(clean) or "file")
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker",
                "cp",
                f"{container_name}:{clean}",
                local_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return None, "docker cp timed out"
            if proc.returncode != 0:
                return None, (
                    stderr.decode(errors="replace").strip()
                    or f"docker cp exit {proc.returncode}"
                )
            if not os.path.isfile(local_path):
                return None, "docker cp reported success but file is missing"
            return Path(local_path), None
        except FileNotFoundError:
            return None, "docker is not installed or not on PATH"
        except Exception as e:
            return None, f"docker cp failed: {e}"

    async def _send_blob(self, message: Message, blob: bytes, safe_name: str) -> str:
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


# Patterns blocked in shell commands (defense-in-depth even in full-access mode).
# These mainly prevent accidental or malicious attempts to run nested privileged containers,
# mount host paths from inside commands, or access the Docker socket.
# Note: the outer shell sandbox itself now runs with full network + full host FS access (/host).
# Blocklist is best-effort: it's the outer wall, not the only wall. The inner
# wall is taint tracking + the docker sandbox capabilities (no-new-privileges,
# cap-drop ALL, no host net by default). Anything that tries to escape the
# blocklist gets caught by the next layer.
def _shell_exports_dir() -> str:
    """Canonical dir where shell-produced files are staged for re-attach.

    Defaults to <repo>/data/exports, overridable via MAXWELL_SHELL_EXPORT_DIR.
    send_file already allowlists data/exports, so staged files can be
    re-attached with a plain `send_file path=.../exports/<name>` call.
    """
    override = os.environ.get("MAXWELL_SHELL_EXPORT_DIR", "").strip()
    if override:
        return os.path.abspath(override)
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "data", "exports"))


_SHELL_BLOCKED_PATTERNS = [
    r"--privileged\b",
    r"--pid=host\b",
    r"--device\b",
    r"--mount\b",
    r"--volume\b",
    r"\b-v\s+\S+:\S+",  # trying to do extra docker -v from inside command
    r"/var/run/docker\.sock",
    r"docker\.sock",
    r"docker\s+(?:run|exec)\b",
    # Common shell-redirect / pipe-to-interpreter chains that turn a benign
    # `cat` or `echo` into remote code execution. The "downloaded and run
    # immediately" pattern is a classic prompt-injection payload.
    r"\bcurl\b[^|]*\|\s*(?:sh|bash|zsh|dash|ksh|fish|ash|python\d?|perl|ruby|node)\b",
    r"\bwget\b[^|]*\|\s*(?:sh|bash|zsh|dash|ksh|fish|ash|python\d?|perl|ruby|node)\b",
    r"\bcurl\b[^|]*-o\s*-?\s*\|",  # curl -o- | sh
    r"\bbase64\s+(?:-d|--decode)\b[^|]*\|\s*(?:sh|bash|zsh|python\d?)\b",
    r"\beval\s*\$\(.*(?:curl|wget)\b",  # eval $(curl ...)
]


class ShellTool(Tool):
    """Execute shell commands in the dedicated Docker sandbox."""

    # Shell executes arbitrary code in a container. It's the most dangerous
    # tool we expose, so it gets the taint-check / user-confirmation gate.
    is_destructive = True

    CONTAINER_NAME = "maxwell-shell"
    IMAGE_NAME = "maxwell-shell"
    DOCKERFILE_DIR = os.path.join(os.path.dirname(__file__), "docker")

    # Output / command-length caps. Read from env so the operator can tune
    # without a code change. 0 = unlimited (use with care; see below).
    # Defaults are generous: 100k chars of captured output covers any sane
    # `cat /var/log/*` or `find` invocation, and 64k command length is enough
    # for a multi-line ffmpeg pipeline. If you actually need more, raise
    # MAXWELL_SHELL_MAX_OUTPUT / MAXWELL_SHELL_MAX_COMMAND_LENGTH in .env.
    #
    # Why not just remove the caps entirely? Because we still have to fit
    # the response through Discord (2000 char chunks) AND through the LLM
    # context window. A 50 MB stdout will OOM the model long before it
    # OOMs us. 0/unlimited is fine if you've tuned your context budget.
    _MAX_OUTPUT_DEFAULT = 100_000
    _MAX_COMMAND_LENGTH_DEFAULT = 65_536

    # Hard ceiling on shell timeout. The actual timeout is read from env at
    # call time so the operator can raise/lower it, but we never let it
    # exceed this regardless of config. Why a cap? Because the tool runs
    # arbitrary code, and a runaway `cat /dev/zero` or `apt install
    # chromium` can pin a core forever. The cap is high (1 hour) but not
    # gone. If you find yourself wanting to remove it, you probably want
    # a different tool (a job queue, not a chatbot tool call).
    _TIMEOUT_CEILING_SECONDS = 3600

    @classmethod
    def _max_output(cls) -> int:
        """Captured stdout+stderr cap. 0 = unlimited."""
        raw = os.environ.get("MAXWELL_SHELL_MAX_OUTPUT", "").strip()
        if not raw:
            return cls._MAX_OUTPUT_DEFAULT
        try:
            v = int(raw)
        except ValueError:
            return cls._MAX_OUTPUT_DEFAULT
        return max(0, v)  # 0 means unlimited

    @classmethod
    def _max_command_length(cls) -> int:
        """Max chars in a single shell command. 0 = unlimited."""
        raw = os.environ.get("MAXWELL_SHELL_MAX_COMMAND_LENGTH", "").strip()
        if not raw:
            return cls._MAX_COMMAND_LENGTH_DEFAULT
        try:
            v = int(raw)
        except ValueError:
            return cls._MAX_COMMAND_LENGTH_DEFAULT
        return max(0, v)

    @classmethod
    def _timeout_seconds(cls) -> int:
        """Max wall-clock seconds for a shell command. Always > 0; capped at 1h."""
        raw = os.environ.get("MAXWELL_SHELL_TIMEOUT", "").strip()
        if not raw:
            return 600  # 10 min default — was 30s, way too tight for real work
        try:
            v = int(raw)
        except ValueError:
            return 600
        return max(1, min(v, cls._TIMEOUT_CEILING_SECONDS))

    # Serialize container lifecycle + exec so parallel tool batches cannot
    # race docker rm -f / recreate.
    _lifecycle_lock = asyncio.Lock()

    @staticmethod
    def _full_host_access() -> bool:
        """Opt-in host RCE mode. Default is isolated (no /host, no host net)."""
        return os.environ.get("MAXWELL_SHELL_FULL_HOST", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

    def get_description(self):
        # Surface live limits so the model doesn't have to guess. Pulled at
        # description-build time, which happens per-turn on tool registration.
        max_out = self._max_output()
        max_cmd = self._max_command_length()
        to = self._timeout_seconds()
        max_out_str = "unlimited" if max_out == 0 else f"{max_out:,} chars"
        max_cmd_str = "unlimited" if max_cmd == 0 else f"{max_cmd:,} chars"
        limits_note = (
            f"Limits: command <= {max_cmd_str}, captured output <= {max_out_str}, "
            f"timeout {to}s. Set MAXWELL_SHELL_MAX_OUTPUT=0 / "
            f"MAXWELL_SHELL_MAX_COMMAND_LENGTH=0 in .env to disable."
        )
        if self._full_host_access():
            return (
                "Run a shell command with bash -lc in the maxwell-shell container. "
                "FULL ACCESS MODE (MAXWELL_SHELL_FULL_HOST): host network, host root at /host, root user. "
                "Params: command (required), files (optional: a JSON array OR comma-separated list of "
                "paths to attach to the reply). To send an artifact the command produced, pass its "
                "path under /home/maxwell in `files` — e.g. files='[\"out.png\"]' or files='out.png, "
                "data.zip'. The file will be docker-cp'd out and uploaded to the channel. "
                "Examples:\n"
                '  command="echo hi"                       -> stdout only\n'
                "  command=\"python3 make.py\", files='out.png' -> runs script + attaches out.png\n"
                "  command=\"zip a.zip a.png b.png\", files='a.zip' -> runs zip + attaches archive\n"
                f"{limits_note}"
            )
        return (
            "Run a shell command with bash -lc in the maxwell-shell sandbox container. "
            "Isolated sandbox: no host filesystem mount, bridge network, memory/cpu/pids limits. "
            "Working directory is /home/maxwell (project shelldocker volume only). "
            "Params: command (required), files (optional: a JSON array OR comma-separated list of "
            "paths under /home/maxwell to attach to the reply). When your command produces an "
            "artifact (image, video, audio, archive, pdf, csv, html, anything the user wants), "
            "list its path in `files` and it will be docker-cp'd out of the container and uploaded "
            "to the channel automatically. Examples:\n"
            '  command="echo hi"                              -> stdout only\n'
            "  command=\"python3 make.py\", files='out.png'     -> runs script + attaches out.png\n"
            "  command=\"ffmpeg -i in.mp4 out.mp4\", files='out.mp4' -> transcodes + attaches result\n"
            "  command=\"convert x.svg x.png\", files='x.png'   -> rasterizes + attaches png\n"
            "Max 10 MB per file. No path traversal (paths under /home/maxwell only).\n"
            f"{limits_note}"
        )

    async def _run_docker(self, *args: str, timeout: int = 30):
        proc = await asyncio.create_subprocess_exec(
            "docker",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            return await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            ), proc.returncode
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise

    async def _ensure_container(self):
        # Reuse a running container when present and access mode matches.
        # Recreate when missing/stopped or when full-host mode flag changed.
        desired_mode = "full" if self._full_host_access() else "isolated"
        try:
            (stdout, _stderr), code = await self._run_docker(
                "inspect",
                "-f",
                '{{.State.Running}} {{index .Config.Labels "maxwell.shell.mode"}}',
                self.CONTAINER_NAME,
                timeout=10,
            )
            if code == 0:
                parts = stdout.decode(errors="replace").strip().split(None, 1)
                running = (parts[0] if parts else "").lower() == "true"
                mode = parts[1] if len(parts) > 1 else ""
                if running and mode == desired_mode:
                    return
                # Wrong mode or stopped — recreate cleanly.
                with contextlib.suppress(Exception):
                    await self._run_docker("rm", "-f", self.CONTAINER_NAME, timeout=10)
                (_stdout, stderr), start_code = await self._run_docker(
                    "start", self.CONTAINER_NAME, timeout=15
                )
                if start_code == 0:
                    return
                # Stale container with wrong flags — remove and recreate below.
                with contextlib.suppress(Exception):
                    await self._run_docker("rm", "-f", self.CONTAINER_NAME, timeout=10)
        except FileNotFoundError as exc:
            raise RuntimeError("docker is not installed or not on PATH") from exc
        except asyncio.TimeoutError as exc:
            raise RuntimeError("docker did not respond while checking sandbox") from exc

        (_stdout, stderr), build_code = await self._run_docker(
            "build", "-t", self.IMAGE_NAME, self.DOCKERFILE_DIR, timeout=600
        )
        if build_code != 0:
            raise RuntimeError(
                stderr.decode(errors="replace").strip() or "docker build failed"
            )

        shell_host = os.path.join(os.path.dirname(__file__), "shelldocker")
        run_args = [
            "run",
            "-d",
            "--name",
            self.CONTAINER_NAME,
            "--label",
            f"maxwell.shell.mode={desired_mode}",
            "--memory",
            "4g",
            "--cpus",
            "2.0",
            "--pids-limit",
            "1024",
            "--tmpfs",
            "/tmp:rw,exec,nosuid,size=256m",
            "-v",
            f"{shell_host}:/home/maxwell:rw",
        ]
        if self._full_host_access():
            # Explicit opt-in: host network + full host FS (documented RCE for admins).
            run_args.extend(
                [
                    "--network",
                    "host",
                    "-v",
                    "/:/host:rw",
                ]
            )
        else:
            # Default: isolated sandbox (no docker.sock, no host root, no host net).
            run_args.extend(
                [
                    "--network",
                    "bridge",
                    "--security-opt",
                    "no-new-privileges:true",
                    "--cap-drop",
                    "ALL",
                    "--cap-add",
                    "CHOWN",
                    "--cap-add",
                    "SETUID",
                    "--cap-add",
                    "SETGID",
                    "--cap-add",
                    "DAC_OVERRIDE",
                    "--cap-add",
                    "FOWNER",
                    "--cap-add",
                    "NET_RAW",
                    "--cap-add",
                    "NET_BIND_SERVICE",
                ]
            )
        run_args.append(self.IMAGE_NAME)
        (_stdout, stderr), run_code = await self._run_docker(*run_args, timeout=30)
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

    def _validate_command(self, command: str) -> str | None:
        """Return an error reason if the command looks dangerous, otherwise None."""
        if not command:
            return "empty command"
        # 0 = unlimited (operator opts in via MAXWELL_SHELL_MAX_COMMAND_LENGTH=0)
        max_len = self._max_command_length()
        if max_len and len(command) > max_len:
            return f"command too long (max {max_len} chars; set MAXWELL_SHELL_MAX_COMMAND_LENGTH=0 to disable)"
        # Newlines are allowed everywhere. The model writes multi-line scripts
        # (python via heredoc, multi-step bash pipelines, ffmpeg chains) and the
        # old heredoc-only newline restriction rejected valid commands
        # constantly — shell was nearly unusable. The Docker sandbox is the
        # real security boundary (no host FS/net/sock by default); the
        # taint-check gate (in execute) + the blocked patterns below defend
        # against prompt-injection command chaining. We still strip heredoc
        # bodies before the blocked-pattern scan so literal heredoc content
        # fed to an interpreter isn't false-flagged as a shell-level chain.
        non_heredoc = _strip_heredoc_blocks(command)
        if any(ord(c) < 32 and c not in ("\t", "\n", "\r") for c in non_heredoc):
            return "control characters are not allowed in shell commands"
        for pattern in _SHELL_BLOCKED_PATTERNS:
            if re.search(pattern, non_heredoc, re.IGNORECASE):
                return "blocked dangerous shell pattern"
        return None

    async def _run_shell_command(self, command: str):
        sanitized = self._normalize_command(command)
        validation_error = self._validate_command(sanitized)
        if validation_error:
            raise RuntimeError(validation_error)
        if not sanitized:
            raise RuntimeError("empty command")
        async with self._lifecycle_lock:
            await self._ensure_container()
            proc = await asyncio.create_subprocess_exec(
                "docker",
                "exec",
                "--workdir",
                "/home/maxwell",
                "--user",
                "root",
                self.CONTAINER_NAME,
                "bash",
                "-lc",
                sanitized,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=self._timeout_seconds()
                )
                return stdout, stderr, proc.returncode
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise
            except asyncio.CancelledError:
                # Outer autonomy wait_for or other cancel can hit here; always kill child.
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()
                raise
            finally:
                # Belt-and-suspenders: ensure no zombie if communicate didn't finish.
                if proc.returncode is None:
                    try:
                        proc.kill()
                        await proc.wait()
                    except Exception:
                        pass

    def _shell_echo_text(self, command: str, *suffixes: str) -> str:
        """Build the body for a ```ansi block: a (truncated) command echo + suffix lines.

        The command can be a long multi-line script; echoing it verbatim blows
        past Discord's 2000-char limit once wrapped in a codeblock. Cap the
        echo so the actual error/output — the useful part — always fits.
        """
        max_echo = 600
        echo = (
            command
            if len(command) <= max_echo
            else command[:max_echo] + " …(truncated)"
        )
        parts = [f"$ {echo}"]
        parts.extend(s for s in suffixes if s)
        return "\n".join(parts)

    async def _send_ansi_chunks(self, message: Message, text: str) -> None:
        """Send `text` as one or more ```ansi codeblocks, each ≤2000 chars.

        Discord rejects (400 Invalid Form Body, 50035) any message over 2000
        chars. The ```ansi\n...\n``` wrapper is 13 chars (8 for the opener
        `` ```ansi\n`` + 5 for the closer `` \n``` ``) and we leave an
        extra few chars of headroom in case a future change tacks on a
        leading space, a language hint, or a trailing newline. Each chunk
        body is therefore capped at 1980 to stay safely under the limit.
        Without that headroom the chunker silently produced 2001-char
        messages that 400'd (see the 19:16 error flood in the bot log).
        Splits on newlines where possible so output stays readable.
        """
        wrapper = 13  # len("```ansi\n") + len("\n```")
        headroom = 7  # safety margin for tweaks / stray whitespace
        limit = 2000 - wrapper - headroom
        chunks: list[str] = []
        remaining = text
        while remaining:
            if len(remaining) <= limit:
                chunks.append(remaining)
                break
            cut = remaining.rfind("\n", 0, limit)
            if cut <= 0:
                cut = limit
            chunks.append(remaining[:cut])
            remaining = remaining[cut:].lstrip("\n")
        for i, chunk in enumerate(chunks):
            await message.channel.send(f"```ansi\n{chunk}\n```")
            if len(chunks) > 1 and i < len(chunks) - 1:
                await asyncio.sleep(0.3)

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

        # No whitelist: any user in an allowed channel can run shell. The
        # sandbox is the security boundary (root inside container, but no
        # host / mount, no host net, no docker socket by default). The
        # taint-check below still requires `,confirm` on turns that read
        # URL/web-search content.

        # Indirect-prompt-injection defense: if the current turn is tainted
        # (the model just read content from a URL / web search that may carry
        # prompt-injection payloads), require an explicit confirm flag on the
        # call. Without this, a malicious page can say "run `rm -rf ~`" and
        # the model can comply even with the blocklist in place.
        tainted = bool(
            self.bot is not None
            and getattr(self.bot, "is_message_tainted", None)
            and self.bot.is_message_tainted(message)
        )
        if tainted and not kwargs.get("_confirmed", False):
            preview = normalized[:200] + ("..." if len(normalized) > 200 else "")
            return (
                "Error: shell refused: this turn read content from a fetched "
                "URL/web search that may carry prompt-injection payloads. "
                "The user must confirm out-of-band with `,confirm` "
                "(admins/whitelisted users only) before this can run.\n"
                f"Command preview: {preview}"
            )

        try:
            stdout, stderr, exit_code = await self._run_shell_command(normalized)
        except asyncio.TimeoutError:
            text = self._shell_echo_text(
                normalized, f"\u23f1 Timed out after {self._timeout_seconds()}s"
            )
            # Even the error path posts its own message — tell the live
            # progress line to step aside so we don't show both.
            self._signal_streaming(message)
            await self._send_ansi_chunks(message, text)
            return f"__SHELL_SENT__\n{text}"
        except Exception as e:
            text = self._shell_echo_text(normalized, f"\u274c Error: {e}")
            self._signal_streaming(message)
            await self._send_ansi_chunks(message, text)
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

        # 0 = unlimited. Still useful as a safety belt against accidental
        # 500 MB stdout floods — but if the operator really wants the
        # full firehose, they can opt in.
        max_out = self._max_output()
        if max_out and len(combined) > max_out:
            combined = combined[:max_out] + "\n... (truncated)"

        text = self._shell_echo_text(normalized, combined)
        self._signal_streaming(message)
        await self._send_ansi_chunks(message, text)

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
        """Copy a file out of the container, stage it in data/exports/, and
        send it to Discord. Returns filename on success.

        Staging into data/exports/ (which send_file already allowlists) means a
        follow-up `send_file path=.../exports/<name>` can re-attach the same
        artifact without another docker cp — the round-trip is one-shot.
        """
        # Sanitize — no path traversal escapes from /home/maxwell
        clean = rel_path.strip().lstrip("/")
        # The model usually passes a full container path like
        # /home/maxwell/img/foo.png (the system prompt tells it to). lstrip
        # only killed the leading slash, so strip the home/maxwell prefix
        # too — otherwise we re-prepend it and docker cp looks for
        # /home/maxwell/home/maxwell/img/foo.png (which is the bug we're fixing).
        clean = re.sub(r"^home/maxwell/?", "", clean)
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
            if file_size > 10 * 1024 * 1024:
                logger.warning(f"Shell file too large to send: {file_size} bytes")
                return None

            filename = os.path.basename(clean)
            # Step aside for the live progress message before posting
            # the file artifact.
            self._signal_streaming(message)
            await message.channel.send(file=File(local_path, filename=filename))
            logger.info(f"Sent shell file: {filename} ({file_size} bytes)")

            # Stage a copy into the canonical exports dir for later re-attach.
            try:
                exports_dir = _shell_exports_dir()
                os.makedirs(exports_dir, exist_ok=True)
                staged = os.path.join(exports_dir, filename)
                # Avoid clobbering an existing export with the same name.
                if os.path.exists(staged):
                    base, ext = os.path.splitext(filename)
                    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
                    staged = os.path.join(exports_dir, f"{base}_{stamp}{ext}")
                shutil.copy2(local_path, staged)
                logger.info(f"Staged shell file to exports: {staged}")
            except Exception as e:
                logger.warning(f"Failed to stage shell file to exports: {e}")

            return filename
        except asyncio.TimeoutError:
            logger.warning(f"docker cp timed out for {container_path}")
            return None
        except Exception as e:
            logger.warning(f"Failed to send shell file {rel_path}: {e}")
            return None
        finally:
            with contextlib.suppress(Exception):
                shutil.rmtree(tmp_dir, ignore_errors=True)


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

        # Mark this turn as tainted: the URL is operator-supplied but its
        # *content* is untrusted and may include prompt-injection payloads
        # designed to steer the model into proposing shell / sub_agent calls.
        if self.bot is not None and hasattr(self.bot, "mark_message_tainted"):
            self.bot.mark_message_tainted(message)

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
                with contextlib.suppress(Exception):
                    text = json.dumps(json.loads(text), indent=2, ensure_ascii=False)
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
                # Decode ALL HTML entities (named + numeric) in one pass instead
                # of hand-picking a few common ones. The old code dropped numeric
                # entities like &#8217; (right single quote) entirely and missed
                # anything beyond the handful it special-cased.
                text = html.unescape(text)
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
    YOUTUBE_HOST_RE = re.compile(
        r"(^|\.)(youtube\.com|youtu\.be|youtube-nocookie\.com)$", re.I
    )

    def get_description(self):
        return (
            "Fetch a YouTube video's transcript/captions and optionally extract still frames at timestamps. "
            "Extracted frames are attached to the model for inspection, not posted to chat. "
            "Use this for YouTube links instead of fetch_url. Params: url (required), timestamps (optional comma-separated seconds or mm:ss/hh:mm:ss), "
            "max_transcript_chars (optional, default 12000, max 20000), lang (optional, default en)."
        )

    def _cookies_file(self) -> str | None:
        raw_path = os.environ.get("YOUTUBE_COOKIES_FILE", "").strip()
        if raw_path:
            path = Path(raw_path).expanduser()
        else:
            data_dir = Path(
                getattr(
                    getattr(self.bot, "config", None),
                    "DATA_DIR",
                    os.environ.get("DATA_DIR", "data"),
                )
            )
            path = data_dir / "youtube_cookies.txt"
        try:
            if path.exists() and path.is_file() and path.stat().st_size > 0:
                return str(path)
        except OSError:
            return None
        return None

    def _yt_dlp_args(self, *args: str) -> list[str]:
        cmd = ["yt-dlp", "--no-update"]
        if shutil.which("node"):
            cmd.extend(["--js-runtimes", "node"])
        cookies = self._cookies_file()
        if cookies:
            cmd.extend(["--cookies", cookies])
        cmd.extend(args)
        return cmd

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
            text = re.sub(
                r"</?(?:url|tool:youtube|youtube)\b[^>]*>",
                "",
                text,
                flags=re.IGNORECASE,
            ).strip()
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
            match = re.search(
                r"/(?:embed|shorts|live)/([A-Za-z0-9_-]{6,})", parsed.path
            )
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

    async def _run_cmd(
        self, args: list[str], timeout: int = 60
    ) -> tuple[int, str, str]:
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
        current_ts = ""
        for line in raw.splitlines():
            text = line.strip()
            if not text or text == "WEBVTT" or text.startswith(("Kind:", "Language:")):
                continue
            if "-->" in text:
                m = re.match(r"(\d+):(\d{2})(?::(\d{2}))?\.\d{3}", text)
                if m:
                    h, mn, sc = m.group(1), m.group(2), m.group(3)
                    if sc:
                        current_ts = f"{int(h)}:{int(mn):02d}:{int(sc):02d}"
                    else:
                        current_ts = f"{int(h)}:{int(mn):02d}"
                continue
            if re.fullmatch(r"\d+", text):
                continue
            text = re.sub(r"<[^>]+>", "", text)
            text = re.sub(r"&amp;", "&", text)
            text = re.sub(r"&lt;", "<", text)
            text = re.sub(r"&gt;", ">", text)
            text = re.sub(r"\s+", " ", text).strip()
            if text and text not in seen:
                seen.add(text)
                lines.append(f"[{current_ts}] {text}" if current_ts else text)
        return "\n".join(lines)

    async def _download_transcript(self, url: str, lang: str, tmp: Path) -> str:
        direct = await self._download_timedtext(url, lang)
        if direct:
            return direct
        if not shutil.which("yt-dlp"):
            return ""
        out_tpl = str(tmp / "subs.%(ext)s")
        args = self._yt_dlp_args(
            "--skip-download",
            "--ignore-no-formats-error",
            "--write-subs",
            "--write-auto-subs",
            "--sub-langs",
            f"{lang}-orig,{lang}.*,{lang},en-orig,en.*",
            "--sub-format",
            "vtt",
            "-o",
            out_tpl,
            url,
        )
        _code, _stdout, _stderr = await self._run_cmd(args, timeout=60)
        candidates = sorted(
            tmp.glob("subs*.vtt"), key=lambda p: p.stat().st_size, reverse=True
        )
        if not candidates:
            return ""
        return self._strip_vtt(
            candidates[0].read_text(encoding="utf-8", errors="replace")
        )

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
                        events = (
                            data.get("events", []) if isinstance(data, dict) else []
                        )
                        lines = []
                        for event in events:
                            segs = (
                                event.get("segs") if isinstance(event, dict) else None
                            )
                            if not isinstance(segs, list):
                                continue
                            line = "".join(
                                str(seg.get("utf8", ""))
                                for seg in segs
                                if isinstance(seg, dict)
                            )
                            line = re.sub(r"\s+", " ", line).strip()
                            if line:
                                start = event.get("start")
                                if isinstance(start, (int, float)) and start >= 0:
                                    lines.append(
                                        f"[{YouTubeTool._format_ts(float(start))}] {line}"
                                    )
                                else:
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
            self._yt_dlp_args("--dump-json", "--no-playlist", url), timeout=45
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

    async def _extract_frames(
        self, url: str, timestamps: list[float], tmp: Path
    ) -> list[str]:
        if not timestamps or not shutil.which("ffmpeg") or not shutil.which("yt-dlp"):
            return []
        code, stream_url, stderr = await self._run_cmd(
            self._yt_dlp_args(
                "--extractor-args",
                "youtube:player_client=web_embedded",
                "-g",
                "--no-playlist",
                "-f",
                "best[height<=720]/best",
                url,
            ),
            timeout=45,
        )
        if code != 0 or not stream_url.strip():
            return [
                f"frame extraction unavailable: {stderr.strip()[:180] or 'no stream url'}"
            ]
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
                sent.append(
                    f"{self._format_ts(ts)} frame failed: {stderr.strip()[:120]}"
                )
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

    async def _thumbnail_image(self, url: str) -> str:
        video_id = self._video_id(url)
        if not video_id:
            return ""
        session = await _get_shared_session()
        for name in ("maxresdefault.jpg", "sddefault.jpg", "hqdefault.jpg", "0.jpg"):
            thumb_url = f"https://i.ytimg.com/vi/{video_id}/{name}"
            try:
                async with session.get(
                    thumb_url, timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    if resp.status != 200:
                        continue
                    content_type = resp.headers.get("Content-Type", "")
                    if not content_type.startswith("image/"):
                        continue
                    raw = await _read_response_limited(resp, 2 * 1024 * 1024)
                    if not raw.startswith(b"\xff\xd8\xff") and not raw.startswith(
                        b"\x89PNG"
                    ):
                        continue
                    encoded = base64.b64encode(raw).decode("ascii")
                    return (
                        "thumbnail attached for visual inspection\n"
                        f"__IMAGE_B64__{encoded}__END_IMAGE_B64__"
                    )
            except Exception:
                continue
        return ""

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
            max_chars = max(
                1000, min(int(max_transcript_chars), self.MAX_TRANSCRIPT_CHARS)
            )
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
            if not any("__IMAGE_B64__" in item for item in frame_results):
                thumbnail = await self._thumbnail_image(url)
                if thumbnail:
                    frame_results.append(thumbnail)

        title = str(info.get("title") or "YouTube video")
        uploader = str(info.get("uploader") or info.get("channel") or "unknown")
        duration = info.get("duration")
        duration_text = (
            self._format_ts(float(duration))
            if isinstance(duration, (int, float))
            else "unknown"
        )
        parts = [
            f"Title: {title}",
            f"Channel: {uploader}",
            f"Duration: {duration_text}",
        ]
        if transcript:
            if len(transcript) > max_chars:
                transcript = transcript[:max_chars] + "\n... (transcript truncated)"
            parts.append("Transcript:\n" + transcript)
        else:
            parts.append(
                "Transcript: unavailable (no captions found or yt-dlp could not fetch them)."
            )
        if requested_ts:
            parts.append(
                "Frames: "
                + (
                    "; ".join(frame_results)
                    if frame_results
                    else "requested but unavailable"
                )
            )
        elif frame_results:
            parts.append("Visual context: " + "; ".join(frame_results))
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

        filename = _safe_attachment_filename(
            url.rsplit("/", 1)[-1].split("?")[0], default="media"
        )
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
            # Unknown extension: don't disguise it as a PNG; use a generic safe suffix.
            # Discord still transports the raw bytes, so this is only a naming hint.
            logger.warning(
                f"SendMediaTool normalizing unknown extension {ext!r} to .bin"
            )
            filename = os.path.splitext(filename)[0] + ".bin"

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

    # Per-channel last-TTS monotonic timestamp; bounds Riva (paid) + gTTS
    # quota drain and channel spam. The bot is single-process so a class-level
    # dict is sufficient.
    _COOLDOWN_SECONDS = 15.0
    _last_tts: dict[str, float] = {}

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

        # Per-channel cooldown to prevent quota drain / voice-message spam.
        channel_id = str(getattr(getattr(message, "channel", None), "id", "") or "")
        if channel_id:
            now = asyncio.get_running_loop().time()
            last = TtsTool._last_tts.get(channel_id, 0.0)
            if now - last < TtsTool._COOLDOWN_SECONDS:
                wait = int(TtsTool._COOLDOWN_SECONDS - (now - last))
                return (
                    f"Error: TTS on cooldown for this channel (~{wait}s left). "
                    "Wait and try again."
                )
            TtsTool._last_tts[channel_id] = now
            # Keep the map bounded.
            if len(TtsTool._last_tts) > 200:
                cutoff = now - 600
                TtsTool._last_tts = {
                    c: t for c, t in TtsTool._last_tts.items() if t > cutoff
                }

        language_key = _tts_language_key(language, lang, **kwargs)
        lang_is_spanish = language_key == "spanish"

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
            # Bound the gRPC call: a stalled Riva endpoint would hang this tool
            # and leak an executor thread otherwise.
            resp = await asyncio.wait_for(
                loop.run_in_executor(None, run_riva), timeout=30
            )
            logger.info(
                f"Riva TTS synthesized audio with voice={tts_voice_name!r}, language={tts_language_code!r}"
            )

            # Save the WAV file
            with wave.open(filename, "wb") as out_f:
                out_f.setnchannels(1)
                out_f.setsampwidth(2)
                out_f.setframerate(44100)
                out_f.writeframesraw(resp.audio)

        except Exception as e:
            logger.warning(f"Riva TTS synthesis failed: {e}. Falling back to gTTS.")
            # Fallback to local basic gTTS
            try:
                from gtts import gTTS

                def run_gtts():
                    tts = gTTS(text=text, lang="es" if lang_is_spanish else "en")
                    tts.save(filename)

                loop = asyncio.get_running_loop()
                await asyncio.wait_for(loop.run_in_executor(None, run_gtts), timeout=30)
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
                # Distinct from terminal no_response so TTS in a multi-tool batch
                # does not abort follow-up / suppress other tool results.
                return "__TTS_SENT__"
            except Exception as discord_err:
                return f"Error sending TTS voice message to channel: {discord_err}"
            finally:
                for path in {filename, voice_filename}:
                    if os.path.exists(path):
                        with contextlib.suppress(Exception):
                            os.remove(path)
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
        if self.bot and not self.bot._is_admin(message.author.id):
            return "Error: leave_vc is admin-only"
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
            # Cancel any in-flight VC reply/utterance tasks for this guild.
            key = None
            if hasattr(self.bot, "_vc_context_key"):
                key = self.bot._vc_context_key(
                    message.guild, vc.channel, message.channel
                )
            active = getattr(self.bot, "_vc_active_tasks", None) or {}
            for task in list(active.get(key, []) if key else []):
                if task and not task.done():
                    task.cancel()
            if key and isinstance(active, dict):
                active.pop(key, None)
            await vc.disconnect(force=True)
            return "Successfully disconnected from the voice channel."
        except Exception as e:
            return f"Error leaving voice channel: {e}"


class SubAgentTool(Tool):
    """Spawn a background OpenCode sub-agent to handle long tasks."""

    # Sub-agent can execute arbitrary code in a container, fetch the web,
    # and (per its own prompt) run shell. Taint-gate it like ShellTool.
    is_destructive = True

    def get_description(self):
        return (
            "Launch an OpenCode sub-agent to work on a long or complex task in the background. "
            "The main bot can keep chatting while the sub-agent runs. "
            "Params: task (required, full prompt for the sub-agent), slug (optional short name), "
            "timeout_minutes (optional, default 30, max 120), files (optional JSON array of file paths to attach). "
            "The sub-agent works in its own directory under the Maxwell project. "
            "It uses Ollama Cloud with the minimax-m3 model by default. "
            "I'll post the result to the channel when the sub-agent finishes."
        )

    async def execute(
        self,
        message: Message,
        task: str | None = None,
        slug: str | None = None,
        timeout_minutes: str = "30",
        files: str | None = None,
        **kwargs,
    ) -> str:
        if self.bot and not self.bot._is_admin(message.author.id):
            return "Error: sub_agent is admin-only"
        if not task or not str(task).strip():
            return "Error: task prompt is required"

        # Indirect-prompt-injection defense. Same logic as ShellTool: a sub-
        # agent can run arbitrary code in a container, so refuse to spawn one
        # from a tainted turn unless the caller explicitly opts in.
        tainted = bool(
            self.bot is not None
            and getattr(self.bot, "is_message_tainted", None)
            and self.bot.is_message_tainted(message)
        )
        if tainted and not kwargs.get("_confirmed", False):
            preview = str(task)[:200] + ("..." if len(str(task)) > 200 else "")
            return (
                "Error: sub_agent refused: this turn read content from a fetched "
                "URL/web search that may carry prompt-injection payloads. "
                "The user must confirm out-of-band with `,confirm` "
                "(admins only) before this can run.\n"
                f"Task preview: {preview}"
            )

        try:
            minutes = max(1, min(int(timeout_minutes), 120))
        except (TypeError, ValueError):
            minutes = 30

        extra_files: list[str] = []
        if files:
            try:
                parsed = json.loads(files)
                if isinstance(parsed, list):
                    extra_files = [str(f).strip() for f in parsed if str(f).strip()]
                elif isinstance(parsed, str):
                    extra_files = [parsed.strip()] if parsed.strip() else []
            except json.JSONDecodeError:
                extra_files = [f.strip() for f in str(files).split(",") if f.strip()]

        # Allowlist extra files to project-safe trees only (never .env / data secrets).
        allowed_bases = [
            os.path.abspath("subagents"),
            os.path.abspath(os.environ.get("OPENCODE_SUBAGENT_BASE_DIR", "subagents")),
            os.path.abspath(os.path.join(os.path.dirname(__file__), "shelldocker")),
        ]
        site_dir = getattr(getattr(self.bot, "config", None), "MAXWELL_SITE_DIR", "")
        if site_dir:
            allowed_bases.append(os.path.abspath(site_dir))
        safe_files: list[str] = []
        for fpath in extra_files[:20]:
            try:
                resolved = str(Path(fpath).expanduser().resolve())
            except Exception:
                continue
            if any(_is_path_allowed(resolved, base) for base in allowed_bases):
                safe_files.append(resolved)
            else:
                logger.warning(
                    "sub_agent rejected extra file outside allowlist: %s", fpath
                )
        extra_files = safe_files

        try:
            model = os.environ.get("OPENCODE_SUBAGENT_MODEL", "ollama-cloud/minimax-m3")
            return await run_subagent_task(
                self.bot,
                message,
                task,
                slug=slug or "task",
                model=model,
                timeout_minutes=minutes,
                extra_files=extra_files,
            )
        except Exception as e:
            logger.exception("Failed to start sub-agent")
            return f"Error starting sub-agent: {e}"


# =============================================================================
# Email tools (maxwell@z3ki.dev) — local MTA only
#
# Design note — read this before you touch any of the classes below:
#
# Sending and receiving both go through Postfix+Dovecot on localhost.
# Outbound: bot connects to 127.0.0.1:25, EHLO, STARTTLS, SASL PLAIN, MAIL FROM,
#   RCPT TO, DATA. Postfix handles all DNS lookup, queueing, retry, and the
#   actual TCP hand-off to the recipient's MX. We never touch port 25 directly.
# Inbound: bot connects to 127.0.0.1:993 (IMAPS), SASL PLAIN, SELECT INBOX,
#   FETCH. Mail is delivered to /var/mail/vmail/z3ki.dev/maxwell/ via the
#   Postfix virtual(5) transport, which is maildir-format. Dovecot serves it
#   over IMAP.
#
# No Mailgun, no Gmail, no third party. Pure VPS, by design. The cost of that
# is that Contabo's IP range is on most DNSBLs, so mail we send to Gmail/Outlook/
# Yahoo will land in spam or get rejected outright (we already saw Gmail return
# 550 5.7.26 — "your email has been blocked because the sender is unauthenticated"
# — because there's no SPF or DKIM yet). When the operator finishes the manual
# DNS work (SPF + DKIM TXT records) and opendkim is wired in, the situation
# improves. The tools themselves don't care either way.
#
# The blocking I/O (`smtplib`, `imaplib`) runs through asyncio.to_thread so
# the bot's event loop isn't held up by a 30-second SMTP timeout. This is the
# same pattern other tools in this file use implicitly.
# =============================================================================


def _email_cfg(bot) -> dict:
    """Pull the email-related config keys in one place.

    Defaults are tuned for the local Postfix+Dovecot setup; if the operator
    ever wants to point the bot at a remote SMTP/IMAP server (e.g. for
    testing against Mailgun's sandbox), they only edit env vars, not code.
    """
    cfg = getattr(bot, "config", None)
    return {
        "host": getattr(cfg, "MAXWELL_SMTP_HOST", "127.0.0.1"),
        "smtp_port": int(getattr(cfg, "MAXWELL_SMTP_PORT", "25")),
        "imap_host": getattr(cfg, "MAXWELL_IMAP_HOST", "127.0.0.1"),
        "imap_port": int(getattr(cfg, "MAXWELL_IMAP_PORT", "993")),
        "user": getattr(cfg, "MAXWELL_EMAIL_USER", "maxwell@z3ki.dev"),
        "password": getattr(cfg, "MAXWELL_EMAIL_PASSWORD", ""),
        "from_addr": getattr(cfg, "MAXWELL_EMAIL_FROM", "maxwell@z3ki.dev"),
        "from_name": getattr(cfg, "MAXWELL_EMAIL_FROM_NAME", "Maxwell"),
    }


def _smtp_send_sync(
    host: str,
    port: int,
    user: str,
    password: str,
    from_addr: str,
    from_name: str,
    to_addrs: list[str],
    cc_addrs: list[str],
    bcc_addrs: list[str],
    subject: str,
    body: str,
    is_html: bool,
    reply_to: str | None,
) -> str:
    """Blocking SMTP send. Runs in a thread.

    Returns a one-line status string the bot shows the user. On failure,
    returns "Error: ..." with the underlying exception's text, truncated.
    """
    import smtplib
    from email.message import EmailMessage
    from email.utils import formatdate, make_msgid

    msg = EmailMessage()
    msg["From"] = f"{from_name} <{from_addr}>" if from_name else from_addr
    msg["To"] = ", ".join(to_addrs)
    if cc_addrs:
        msg["Cc"] = ", ".join(cc_addrs)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=from_addr.split("@", 1)[-1])
    if reply_to:
        msg["Reply-To"] = reply_to
    if is_html:
        msg.set_content("This message requires an HTML-capable client.")
        msg.add_alternative(body, subtype="html")
    else:
        msg.set_content(body)

    # All recipients in one RCPT TO list, including BCC. Postfix delivers
    # to each. BCC addresses are stripped from headers (EmailMessage does
    # this automatically) but still in the envelope.
    all_rcpts = to_addrs + cc_addrs + bcc_addrs

    # Per-recipient timeout is the right knob here. 30s connects +
    # 60s message I/O is generous; a hung SMTP server shouldn't keep us
    # in a thread for longer than that.
    timeout = 60
    with smtplib.SMTP(host, port, timeout=timeout) as s:
        s.ehlo()
        # STARTTLS or nothing. The local MTA requires it (smtpd_tls_auth_only=yes);
        # if we ever point at a remote server without TLS, that server's not
        # one we should be talking to.
        s.starttls()
        s.ehlo()
        s.login(user, password)
        refused = s.sendmail(from_addr, all_rcpts, msg.as_string())
    if refused:
        # sendmail returns a dict of {recipient: error} for any it couldn't
        # queue. Postfix should queue everything if the recipient domain is
        # real; if we see something here, treat it as a hard error.
        return "Error: SMTP refused recipients: " + ", ".join(
            f"{r}: {e}" for r, e in refused.items()
        )
    return f"Email queued for {len(all_rcpts)} recipient(s)."


def _imap_connect_sync(host: str, port: int, user: str, password: str):
    """Open IMAPS, return the connection. Caller must close it.

    Use the public Mailbox API instead of poking the raw IMAP4 object; the
    high-level API handles quoting/escaping and gives a sane exception
    hierarchy (imaplib.IMAP4.error) on auth or protocol failures.
    """
    import imaplib

    # The local Dovecot uses a self-signed snakeoil cert. We don't want
    # to make every email read fail with CERTIFICATE_VERIFY_FAILED, so
    # we build a context that doesn't verify. If you swap to a real cert
    # later, remove this and let the default validation apply.
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    M = imaplib.IMAP4_SSL(host, port, ssl_context=ctx)
    M.login(user, password)
    return M


def _imap_list_recent_sync(
    host: str,
    port: int,
    user: str,
    password: str,
    limit: int,
    days_back: int,
    unread_only: bool,
) -> str:
    """List recent messages in INBOX. Returns a multi-line string for the model."""
    M = _imap_connect_sync(host, port, user, password)
    try:
        M.select("INBOX")
        # Build the IMAP search criteria. We use SINCE for date bounding
        # because it's the most universally supported. The cutoff is
        # today - days_back, which Dovecot's IMAP server computes from
        # the local clock. SUBJECT and other keys aren't relevant here.
        from datetime import datetime, timedelta, timezone

        cutoff = datetime.now(timezone.utc).date() - timedelta(days=days_back)
        # IMAP date format is DD-Mon-YYYY, locale-independent.
        date_str = cutoff.strftime("%d-%b-%Y")
        criteria_parts = [f"SINCE {date_str}"]
        if unread_only:
            criteria_parts.append("UNSEEN")
        criteria = " ".join(criteria_parts)
        typ, data = M.search(None, criteria)
        if typ != "OK" or not data or not data[0]:
            return "Inbox is empty for the given filter."
        ids = data[0].split()[-limit:]  # most recent N (highest UIDs last)
        if not ids:
            return "Inbox is empty for the given filter."

        # Fetch ENVELOPE for each id — From, Subject, Date, Size, etc. in
        # one round-trip per message. RFC822.HEADER would pull the whole
        # header block; ENVELOPE is the structured form, easier on the
        # model and on the wire.
        lines: list[str] = []
        for mid in ids:
            typ, msgdata = M.fetch(mid, "(ENVELOPE)")
            if typ != "OK" or not msgdata or not msgdata[0]:
                lines.append(f"- id={mid.decode(errors='replace')} (fetch failed)")
                continue
            # imaplib's response shape varies by server. Dovecot collapses
            # the inline literal into a single response line so msgdata[0]
            # is one bytes blob: b'5 (ENVELOPE ("Sun..." ...))'. Older
            # servers split into two tuple entries. Handle both: pick the
            # first entry that's a bytes object (NOT an int — iterating
            # bytes would give ints, and a single bytes entry is what we
            # actually want).
            try:
                env_bytes: bytes | None = None
                if isinstance(msgdata[0], bytes):
                    env_bytes = msgdata[0]
                else:
                    for entry in msgdata[0]:
                        if isinstance(entry, bytes):
                            env_bytes = entry
                            break
                if env_bytes is None:
                    lines.append(
                        f"- id={mid.decode(errors='replace')} (no envelope in response)"
                    )
                    continue
                env = env_bytes.decode("utf-8", errors="replace")
                # Strip the "mid (ENVELOPE " prefix and trailing ")".
                idx = env.find("(ENVELOPE ")
                if idx < 0:
                    lines.append(
                        f"- id={mid.decode(errors='replace')} (no envelope marker)"
                    )
                    continue
                env = env[idx + len("(ENVELOPE ") :]
                # Trim the trailing ")". We need to do this at the right
                # depth because the envelope contains nested parens.
                # The closing of ENVELOPE is the LAST ")" at depth 0.
                depth = 0
                end_idx = -1
                for i, ch in enumerate(env):
                    if ch == "(":
                        depth += 1
                    elif ch == ")":
                        if depth == 0:
                            end_idx = i
                            break
                        depth -= 1
                if end_idx > 0:
                    env = env[:end_idx]
                # ENVELOPE is now `(date subject from sender reply-to to
                # cc bcc in-reply-to message-id)`. We want from/subject/date.
                from_addr = _imap_extract_envelope_field(env, "from")
                subj = _imap_extract_envelope_field(env, "subject")
                date = _imap_extract_envelope_field(env, "date")
            except Exception as e:
                lines.append(f"- id={mid.decode(errors='replace')} (parse failed: {e})")
                continue
            lines.append(
                f"- id={mid.decode(errors='replace')}\n"
                f"  From: {from_addr}\n"
                f"  Subject: {subj}\n"
                f"  Date: {date}"
            )
        return f"Found {len(lines)} message(s):\n\n" + "\n\n".join(lines)
    finally:
        contextlib.suppress(Exception)
        M.close()
        contextlib.suppress(Exception)
        M.logout()


def _imap_extract_envelope_field(envelope_str: str, field_name: str) -> str:
    """Pull one named field out of an IMAP ENVELOPE response.

    The ENVELOPE response is a parenthesized space-separated list of NIL
    markers and quoted strings. We walk it and match by position, since
    the field order is fixed in the RFC. Returns '?' on any failure.
    """
    try:
        if not envelope_str:
            return "?"
        # Strip the outer parens.
        s = envelope_str.strip()
        if s.startswith("("):
            s = s[1:]
        if s.endswith(")"):
            s = s[:-1]

        # Walk the parenthesized list, handling nested parens and quoted
        # strings. The ENVELOPE structure has nested parens around
        # address lists, so this is more than a split() away.
        tokens = _imap_tokenize(s)
        # Field order: date subject from sender reply-to to cc bcc
        # in-reply-to message-id
        order = [
            "date",
            "subject",
            "from",
            "sender",
            "reply-to",
            "to",
            "cc",
            "bcc",
            "in-reply-to",
            "message-id",
        ]
        if field_name not in order:
            return "?"
        # Skip the fields we don't want.
        idx = order.index(field_name)
        return _imap_format_envelope_value(tokens, idx)
    except Exception:
        return "?"


def _imap_tokenize(s: str) -> list[str]:
    """Tokenize an IMAP parenthesized list into top-level entries.

    Handles nested parens and quoted strings with escapes. Returns each
    top-level item as a string (with its own surrounding parens kept
    where relevant, or NIL for empty).
    """
    out: list[str] = []
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c.isspace():
            i += 1
            continue
        if c == "(":
            # Find matching close, handling nested.
            depth = 1
            j = i + 1
            while j < n and depth > 0:
                if s[j] == "(":
                    depth += 1
                elif s[j] == ")":
                    depth -= 1
                j += 1
            out.append(s[i:j])
            i = j
            continue
        if c == '"':
            # Quoted string; collect until matching unescaped quote.
            j = i + 1
            buf: list[str] = ['"']
            while j < n:
                if s[j] == "\\" and j + 1 < n:
                    buf.append(s[j : j + 2])
                    j += 2
                    continue
                if s[j] == '"':
                    buf.append('"')
                    j += 1
                    break
                buf.append(s[j])
                j += 1
            out.append("".join(buf))
            i = j
            continue
        if s[i : i + 3] == "NIL":
            out.append("NIL")
            i += 3
            continue
        # Atom (unquoted, no spaces/parens).
        j = i
        while j < n and not s[j].isspace() and s[j] not in "()":
            j += 1
        out.append(s[i:j])
        i = j
    return out


def _imap_format_envelope_value(tokens: list[str], field_index: int) -> str:
    """Render a single ENVELOPE field for the model.

    The "from", "to", "cc", "bcc" fields are parenthesized address lists
    of the form `((name route mailbox host))`. We collapse those into
    "Name <mailbox@host>" or just "mailbox@host" when no name. Other
    fields (date, subject, message-id) are quoted strings or NIL — we
    unwrap quotes and return the bare value.
    """
    if field_index >= len(tokens):
        return "?"
    tok = tokens[field_index]
    if tok == "NIL":
        return ""
    if tok.startswith("("):
        # Address list. Walk it and format each entry.
        return _imap_format_address_list(tok)
    if tok.startswith('"') and tok.endswith('"'):
        return tok[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return tok


def _imap_format_address_list(s: str) -> str:
    """Render `((name route mailbox host) ...)` as comma-separated addresses."""
    if not s:
        return ""
    inner = s.strip()
    if inner.startswith("("):
        inner = inner[1:]
    if inner.endswith(")"):
        inner = inner[:-1]
    tokens = _imap_tokenize(inner)
    addrs: list[str] = []
    for tok in tokens:
        if not tok.startswith("("):
            continue
        # Each address: (name route mailbox host)
        a_inner = tok.strip()
        if a_inner.startswith("("):
            a_inner = a_inner[1:]
        if a_inner.endswith(")"):
            a_inner = a_inner[:-1]
        parts = _imap_tokenize(a_inner)
        # parts = [name, route, mailbox, host]
        name = ""
        if len(parts) >= 1 and parts[0] != "NIL":
            name = parts[0]
            if name.startswith('"') and name.endswith('"'):
                name = name[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        mailbox = ""
        if len(parts) >= 3 and parts[2] != "NIL":
            mailbox = parts[2]
            if mailbox.startswith('"') and mailbox.endswith('"'):
                mailbox = mailbox[1:-1]
        host = ""
        if len(parts) >= 4 and parts[3] != "NIL":
            host = parts[3]
            if host.startswith('"') and host.endswith('"'):
                host = host[1:-1]
        addr = f"{mailbox}@{host}" if host else mailbox
        if name:
            addrs.append(f"{name} <{addr}>")
        else:
            addrs.append(addr)
    return ", ".join(addrs)


def _imap_get_message_sync(
    host: str, port: int, user: str, password: str, message_id: str, max_chars: int
) -> str:
    """Fetch one message and return its headers + body, capped at max_chars."""
    M = _imap_connect_sync(host, port, user, password)
    try:
        M.select("INBOX")
        typ, data = M.fetch(message_id, "(RFC822)")
        if typ != "OK" or not data or not data[0]:
            return f"Error: IMAP fetch failed for message {message_id}"
        raw = data[0][1]
        if isinstance(raw, bytes):
            raw_bytes = raw
        else:
            raw_bytes = raw.encode("utf-8", errors="replace")

        from email import policy
        from email.parser import BytesParser

        msg = BytesParser(policy=policy.default).parsebytes(raw_bytes)
        body = _extract_text_body(msg) or "(no plain-text body found)"
        if len(body) > max_chars:
            body = body[: max_chars - 1].rstrip() + "…"

        from_addr = msg.get("From", "?")
        to_addr = msg.get("To", "?")
        subject = msg.get("Subject", "(no subject)")
        date = msg.get("Date", "?")

        out_lines = [
            f"Message id: {message_id}",
            f"From: {from_addr}",
            f"To: {to_addr}",
            f"Subject: {subject}",
            f"Date: {date}",
            "",
            "---",
            body,
        ]
        return "\n".join(out_lines)
    finally:
        contextlib.suppress(Exception)
        M.close()
        contextlib.suppress(Exception)
        M.logout()


def _extract_text_body(msg) -> str:
    """Walk an email Message and return the best text body we can find.

    Prefers text/plain. If only text/html is present, strips tags as a
    last resort. Multipart/alternative is common: same content in two
    formats, the model wants the plain one.
    """
    import re

    # Walk parts in order; collect any text/plain we find. If we find
    # multiple, the first is usually the most relevant.
    plain: str | None = None
    html: str | None = None
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype == "text/plain" and not part.is_multipart():
                with contextlib.suppress(Exception):
                    plain = part.get_content()
                    break  # first text/plain wins
            if ctype == "text/html" and html is None and not part.is_multipart():
                with contextlib.suppress(Exception):
                    html = part.get_content()
        if plain is not None:
            return plain
        if html is not None:
            return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()
    # Single-part message: try text/plain, then text/html, then raw.
    try:
        return msg.get_content()
    except Exception:
        try:
            payload = msg.get_payload(decode=True) or b""
            return payload.decode("utf-8", errors="replace")
        except Exception:
            return ""


def _imap_search_sync(
    host: str,
    port: int,
    user: str,
    password: str,
    query: str,
    limit: int,
) -> str:
    """Run an IMAP SEARCH and return matching message ids + envelopes."""
    M = _imap_connect_sync(host, port, user, password)
    try:
        M.select("INBOX")
        typ, data = M.search(None, f'TEXT "{query}"')
        if typ != "OK" or not data or not data[0]:
            return f"No messages matched: {query!r}"
        ids = data[0].split()[-limit:]
        if not ids:
            return f"No messages matched: {query!r}"

        # ENVELOPE for each so the model has subject/from without a second
        # round-trip. Same shape as in the list tool above.
        lines = [f"Search results for {query!r} ({len(ids)} match(es)):"]
        for mid in ids:
            typ, msgdata = M.fetch(mid, "(ENVELOPE)")
            if typ != "OK" or not msgdata or not msgdata[0]:
                lines.append(f"- id={mid.decode(errors='replace')}")
                continue
            try:
                env_bytes: bytes | None = None
                if isinstance(msgdata[0], bytes):
                    env_bytes = msgdata[0]
                else:
                    for entry in msgdata[0]:
                        if isinstance(entry, bytes):
                            env_bytes = entry
                            break
                if env_bytes is None:
                    lines.append(f"- id={mid.decode(errors='replace')}")
                    continue
                env = env_bytes.decode("utf-8", errors="replace")
                idx = env.find("(ENVELOPE ")
                if idx >= 0:
                    env = env[idx + len("(ENVELOPE ") :]
                    depth = 0
                    end_idx = -1
                    for i, ch in enumerate(env):
                        if ch == "(":
                            depth += 1
                        elif ch == ")":
                            if depth == 0:
                                end_idx = i
                                break
                            depth -= 1
                    if end_idx > 0:
                        env = env[:end_idx]
                from_addr = _imap_extract_envelope_field(env, "from")
                subj = _imap_extract_envelope_field(env, "subject")
                date = _imap_extract_envelope_field(env, "date")
            except Exception:
                from_addr = subj = date = "?"
            lines.append(
                f"- id={mid.decode(errors='replace')}\n"
                f"  From: {from_addr}\n"
                f"  Subject: {subj}\n"
                f"  Date: {date}"
            )
        return "\n\n".join(lines)
    finally:
        contextlib.suppress(Exception)
        M.close()
        contextlib.suppress(Exception)
        M.logout()


class EmailSendTool(Tool):
    """Send mail FROM the local mailbox via local Postfix."""

    # Sending mail is the obvious prompt-injection target ("send my password
    # to attacker@evil") and on a tainted turn the user has to confirm.
    is_destructive: bool = True

    def get_description(self) -> str:
        return (
            "Send an email from the bot's local mailbox (maxwell@z3ki.dev by default) "
            "through the local Postfix instance on 127.0.0.1:25. No third-party relay; "
            "Postfix handles delivery to the recipient's MX. "
            "Params: to (required, comma-separated for multiple), subject (required), "
            "body (required, plain text or HTML — set is_html=true for HTML), "
            "is_html (optional bool, default false), reply_to (optional), "
            "cc (optional, comma-separated), bcc (optional, comma-separated)."
        )

    async def execute(
        self,
        message: Message,
        to: str | None = None,
        subject: str | None = None,
        body: str | None = None,
        is_html: str = "false",
        reply_to: str | None = None,
        cc: str | None = None,
        bcc: str | None = None,
        **kwargs,
    ) -> str:
        cfg = _email_cfg(self.bot)
        if not cfg["password"]:
            return (
                "Error: local mail is not configured. Set MAXWELL_EMAIL_PASSWORD "
                "in .env (the same password Dovecot knows about — /etc/dovecot/users)."
            )
        if not to or not str(to).strip():
            return "Error: 'to' is required"
        if not subject or not str(subject).strip():
            return "Error: 'subject' is required"
        if body is None:
            return "Error: 'body' is required"

        # Indirect-prompt-injection gate. If this turn was tainted by a
        # fetched URL or web search result, refuse without an explicit user
        # confirmation. Same pattern as shell/sub_agent.
        tainted = bool(
            self.bot is not None
            and getattr(self.bot, "is_message_tainted", None)
            and self.bot.is_message_tainted(message)
        )
        if tainted and not kwargs.get("_confirmed", False):
            preview = str(body)[:200] + ("..." if len(str(body)) > 200 else "")
            return (
                "Error: email_send refused: this turn read content from a "
                "fetched URL/web search that may carry prompt-injection "
                "payloads. The user must confirm out-of-band with `,confirm` "
                "(admins only) before this can run.\n"
                f"Recipient: {to}\n"
                f"Subject: {subject}\n"
                f"Body preview: {preview}"
            )

        to_addrs = [a.strip() for a in str(to).split(",") if a.strip()]
        cc_addrs = [a.strip() for a in str(cc).split(",") if a.strip()] if cc else []
        bcc_addrs = [a.strip() for a in str(bcc).split(",") if a.strip()] if bcc else []

        try:
            return await asyncio.to_thread(
                _smtp_send_sync,
                cfg["host"],
                cfg["smtp_port"],
                cfg["user"],
                cfg["password"],
                cfg["from_addr"],
                cfg["from_name"],
                to_addrs,
                cc_addrs,
                bcc_addrs,
                str(subject),
                str(body),
                str(is_html).lower() in {"1", "true", "yes"},
                str(reply_to).strip() if reply_to else None,
            )
        except Exception as e:
            return f"Error: SMTP send failed: {e}"


class EmailReadInboxTool(Tool):
    """List recent messages in the local mailbox."""

    is_destructive: bool = False

    def get_description(self) -> str:
        return (
            "Read recent emails from the local mailbox (maxwell@z3ki.dev). "
            "Returns a compact list: id, from, subject, date. Use email_get_message "
            "to fetch a message body. Params: max_results (optional, default 10, max 50), "
            "days_back (optional, default 7, max 90), unread_only (optional bool, default false)."
        )

    async def execute(
        self,
        message: Message,
        max_results: str = "10",
        days_back: str = "7",
        unread_only: str = "false",
        **kwargs,
    ) -> str:
        cfg = _email_cfg(self.bot)
        if not cfg["password"]:
            return (
                "Error: local mail is not configured. Set MAXWELL_EMAIL_PASSWORD "
                "in .env (the same password Dovecot knows about — /etc/dovecot/users)."
            )
        try:
            limit = max(1, min(int(max_results), 50))
        except (TypeError, ValueError):
            limit = 10
        try:
            days = max(0, min(int(days_back), 90))
        except (TypeError, ValueError):
            days = 7
        try:
            return await asyncio.to_thread(
                _imap_list_recent_sync,
                cfg["imap_host"],
                cfg["imap_port"],
                cfg["user"],
                cfg["password"],
                limit,
                days,
                str(unread_only).lower() in {"1", "true", "yes"},
            )
        except Exception as e:
            return f"Error: IMAP read failed: {e}"


class EmailGetMessageTool(Tool):
    """Fetch the full body of a single local message by id."""

    is_destructive: bool = False

    def get_description(self) -> str:
        return (
            "Fetch the full body and headers of a single email by its id. "
            "Get the id from email_read_inbox or email_search. Params: message_id (required), "
            "max_chars (optional, default 8000) — caps the returned body length."
        )

    async def execute(
        self,
        message: Message,
        message_id: str | None = None,
        max_chars: str = "8000",
        **kwargs,
    ) -> str:
        if not message_id or not str(message_id).strip():
            return "Error: message_id is required"
        try:
            cap = max(200, min(int(max_chars), 50000))
        except (TypeError, ValueError):
            cap = 8000

        cfg = _email_cfg(self.bot)
        if not cfg["password"]:
            return "Error: local mail is not configured. Set MAXWELL_EMAIL_PASSWORD in .env."
        try:
            return await asyncio.to_thread(
                _imap_get_message_sync,
                cfg["imap_host"],
                cfg["imap_port"],
                cfg["user"],
                cfg["password"],
                str(message_id).strip(),
                cap,
            )
        except Exception as e:
            return f"Error: IMAP fetch failed: {e}"


class EmailSearchTool(Tool):
    """Full-text search of the local mailbox."""

    is_destructive: bool = False

    def get_description(self) -> str:
        return (
            "Search the local mailbox (maxwell@z3ki.dev) using IMAP TEXT search. "
            "Params: query (required, e.g. 'github', 'invoice', 'from:support'), "
            "max_results (optional, default 10, max 50). Returns matching message ids "
            "plus subject/from/date. Use email_get_message to read the body."
        )

    async def execute(
        self,
        message: Message,
        query: str | None = None,
        max_results: str = "10",
        **kwargs,
    ) -> str:
        if not query or not str(query).strip():
            return "Error: query is required"
        try:
            limit = max(1, min(int(max_results), 50))
        except (TypeError, ValueError):
            limit = 10
        cfg = _email_cfg(self.bot)
        if not cfg["password"]:
            return "Error: local mail is not configured. Set MAXWELL_EMAIL_PASSWORD in .env."
        try:
            return await asyncio.to_thread(
                _imap_search_sync,
                cfg["imap_host"],
                cfg["imap_port"],
                cfg["user"],
                cfg["password"],
                str(query).strip(),
                limit,
            )
        except Exception as e:
            return f"Error: IMAP search failed: {e}"
