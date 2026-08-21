"""Tools for Maxwell Bot

All tools return a result string for the LLM. They do NOT send errors
to the Discord channel — errors are returned as strings so the LLM can
generate a natural response. Only success outputs (images, DMs) are
sent directly to their target.
"""

import contextlib
import html
import ipaddress
import json
import logging
import os
import random
import re
import shutil
import socket
import ssl
import sys
import tempfile
import time
import wave
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlparse

import aiofiles
import aiohttp
import asyncio
import base64
import uuid
import discord
from discord import Activity, File, Message, Status
from tools import Tool
from captcha_solver import CaptchaSolveError
from config import Config
from control_defaults import parse_bool
from tool_schemas import (
    elide_tool_calls_for_history,
    normalize_native_tool_calls,
    trim_tool_tail,
)
from utils import (  # single source of truth, fd-safe
    FileLock,
    _atomic_json_write_sync,
)

try:
    from ddgs import DDGS as _DDGS

    _DDGS_AVAILABLE = True
except ImportError:
    _DDGS = None
    _DDGS_AVAILABLE = False

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


async def _synthesize_fish_tts(
    text: str,
    output_path: str,
    *,
    api_key: str,
    model: str,
    reference_id: str,
    fmt: str = "mp3",
) -> str | None:
    """Call Fish Audio's TTS API. Returns output_path on success, None on
    failure (caller falls through to next provider).

    Fish is preferred over Riva when FISH_API_KEY is set: free tier, no gRPC
    dependency, supports emotion tags like `[excited]`, `[laughing]` inline.

    Docs: https://docs.fish.audio/api-reference/developer-apis/text-to-speech
    """
    if not api_key:
        return None
    url = "https://api.fish.audio/v1/tts"
    payload = {
        "text": text,
        "format": fmt,
    }
    if reference_id:
        payload["reference_id"] = reference_id
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "model": model,
    }
    try:
        session = await _get_shared_session()
        timeout = aiohttp.ClientTimeout(total=45)
        async with session.post(
            url, json=payload, headers=headers, timeout=timeout
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                logger.warning("Fish TTS API returned %s: %s", resp.status, body[:200])
                return None
            data = await resp.read()
        if not data or len(data) < 64:
            logger.warning(
                "Fish TTS returned empty/too-small payload (%d bytes)", len(data)
            )
            return None
        # Fish returns MP3 bytes (or whatever fmt requested); write directly.
        # The downstream `make_voice_ogg` re-encodes via ffmpeg so extension
        # does not matter — ffmpeg sniffs the format.
        with open(output_path, "wb") as f:
            f.write(data)
        logger.info(
            "Fish TTS synthesized %d bytes (model=%s, ref=%s)",
            len(data),
            model,
            bool(reference_id),
        )
        return output_path
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        logger.warning("Fish TTS request failed: %s", e)
        return None
    except Exception as e:
        logger.warning("Fish TTS unexpected error: %s", e)
        return None


# Named Fish reference voices. Each name maps to its own env var; the
# legacy TTS_FISH_REFERENCE_ID stays the backward-compatible default so
# existing installs keep their current voice unless they opt into a name.
FISH_REFERENCE_ENV = {
    "tiktok": "TTS_FISH_REFERENCE_ID_TIKTOK",
    "mommy": "TTS_FISH_REFERENCE_ID_MOMMY",
}

# Hardcoded fallback when no TTS_FISH_REFERENCE_ID* env var is set at all.
FISH_REFERENCE_DEFAULT = "8d21b053e2804e2a890e1cf62f267b6f"


def _fish_reference_id(voice: str | None = None) -> str:
    """Resolve a named Fish voice ("tiktok", "mommy", ...) to a reference id.

    Unknown/empty names fall back to TTS_FISH_REFERENCE_ID (then the
    hardcoded default), so callers that don't care about voices keep the
    exact behaviour they had before named voices existed.
    """
    if voice:
        env_key = FISH_REFERENCE_ENV.get(str(voice).strip().lower())
        if env_key:
            value = os.environ.get(env_key, "").strip()
            if value:
                return value
    return os.environ.get("TTS_FISH_REFERENCE_ID", FISH_REFERENCE_DEFAULT).strip()


def _is_safe_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    # Unwrap IPv4-mapped IPv6 (::ffff:127.0.0.1) so loopback/private checks apply.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
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
            with contextlib.suppress(Exception):
                await _SHARED_SESSION.close()
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
            with contextlib.suppress(Exception):
                await _SHARED_SESSION.close()
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


# Bash heredoc opener. Models almost always write the redirect on the same
# line as the delimiter (`cat << 'EOF' > file.py`); a here-string (`<<<`)
# is not a heredoc. Optional `<<-` (tab-stripped body) is accepted.
_HEREDOC_DELIM_RE = re.compile(
    r"""
    (?<!<)<<(?!<)
    -?
    \s*
    (?:
        '([A-Za-z_][A-Za-z0-9_-]*)'
      | "([A-Za-z_][A-Za-z0-9_-]*)"
      | \\?([A-Za-z_][A-Za-z0-9_-]*)
    )
    """,
    re.VERBOSE,
)


def _heredoc_delimiter(line: str) -> str | None:
    """Return the heredoc delimiter token if `line` opens a heredoc."""
    m = _HEREDOC_DELIM_RE.search(line)
    if not m:
        return None
    return m.group(1) or m.group(2) or m.group(3)


def _strip_heredoc_blocks(command: str) -> str:
    """Return `command` with heredoc bodies removed.

    A heredoc looks like `... << 'EOF'` (or `<< "EOF"` / `<<EOF` / `<<-EOF`)
    followed by lines of literal content ending with a line containing only
    the delimiter. Redirects and pipes after the delimiter on the opener line
    (`cat << 'EOF' > file`, `python3 - <<'PY' | tee out.py`) are part of the
    command, not the body. Stripping the body lets us validate the remaining
    (non-heredoc) parts as a single line.
    """
    out: list[str] = []
    i = 0
    lines = command.split("\n")
    while i < len(lines):
        line = lines[i]
        delimiter = _heredoc_delimiter(line)
        if delimiter:
            out.append(line)
            i += 1
            while i < len(lines):
                if lines[i].strip() == delimiter:
                    out.append(lines[i])
                    i += 1
                    break
                i += 1
            continue
        out.append(line)
        i += 1
    return "\n".join(out).rstrip("\n")


def _unterminated_heredoc_error(command: str) -> str | None:
    """Explain a newline violation caused by a malformed heredoc.

    Return a targeted hint when a heredoc was never closed so the caller
    is told exactly what to fix.
    """
    lines = str(command or "").split("\n")
    opener: str | None = None  # the delimiter token, when inside a heredoc body
    opener_text = ""
    saw_unparsed_opener = False
    i = 0
    while i < len(lines):
        line = lines[i]
        if opener is None:
            delimiter = _heredoc_delimiter(line)
            if delimiter:
                opener = delimiter
                opener_text = line.strip()
            elif "<<" in line and "<<<" not in line:
                saw_unparsed_opener = True
        else:
            if line.strip() == opener:
                opener = None
        i += 1
    if opener is not None:
        return (
            f"heredoc opened with `{opener_text}` but never closed — add a final "
            f"line containing exactly `{opener}` (nothing else, no trailing text)"
        )
    if saw_unparsed_opener:
        return (
            "could not parse the heredoc opener — use `cat << 'EOF' > file` "
            "(quoted delimiter; `> file` on the same line is fine), then the "
            "file body, then a line containing only EOF"
        )
    return None


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


# ── Permanent public image persistence ──────────────────────────────
# Discord CDN attachment URLs carry an `ex=` signature that expires ~24h
# after upload. Any site that embeds one silently loses its image within a
# day. Generated images are therefore ALSO written under the public site
# dir (_images/) where the host serves them at a stable, never-expiring
# URL that curl/wget/<img>/websites can use directly.


def _public_image_target(bot) -> tuple[str, str]:
    """Return (local_dir, public_base_url) for permanently served images.

    Files land in <MAXWELL_SITE_DIR>/_images/ and are served at
    <MAXWELL_PUBLIC_BASE_URL>/bot/_images/<file> — the same origin that
    serves create_site pages, so nothing expires and no external CDN is
    involved.
    """
    cfg = getattr(bot, "config", None)
    site_dir = str(getattr(cfg, "MAXWELL_SITE_DIR", "public/bot") or "public/bot")
    pub = str(
        getattr(cfg, "MAXWELL_PUBLIC_BASE_URL", "https://maxwell.example.com")
        or "https://maxwell.example.com"
    ).rstrip("/")
    return os.path.join(site_dir, "_images"), f"{pub}/bot/_images"


def _persist_public_image(
    bot, image_bytes: bytes, ext: str = ".png", prefix: str = "img"
) -> tuple[str | None, str | None]:
    """Best-effort write of image bytes to the public _images dir.

    Returns (local_path, public_url) or (None, None) on failure. Callers
    must keep working without a permanent link if the save fails.
    """
    try:
        img_dir, pub_base = _public_image_target(bot)
        os.makedirs(img_dir, exist_ok=True)
        name = f"{prefix}-{int(datetime.now(timezone.utc).timestamp())}-{random.randint(100000, 999999)}{ext}"
        path = os.path.join(img_dir, name)
        with open(path, "wb") as f:
            f.write(image_bytes)
        logger.info(f"Persisted public image {path}")
        return path, f"{pub_base}/{name}"
    except Exception as e:
        logger.warning(f"Failed to persist public image: {e}")
        return None, None


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
                        last_error = "Error: NVIDIA image generation rate limited. Try again later."
                        logger.warning(
                            f"NVIDIA image rate limited, retry {attempt + 1}/{max_retries}"
                        )
                        if attempt < max_retries - 1:
                            await asyncio.sleep((attempt + 1) * 10)
                            continue
                        break
                    if 500 <= response.status < 600:
                        error_text = await response.text()
                        last_error = f"Error generating image: NVIDIA returned {response.status}."
                        logger.warning(
                            f"NVIDIA image server error {response.status}, retry {attempt + 1}/{max_retries}: {error_text[:200]}"
                        )
                        if attempt < max_retries - 1:
                            await asyncio.sleep((attempt + 1) * 15)
                            continue
                        break
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
                    # Persist a permanent public copy — the Discord CDN URL
                    # expires ~24h, which silently breaks any site that
                    # embeds it. The stable URL survives and is curl-able.
                    local_path, perm_url = _persist_public_image(
                        self.bot, image_bytes, prefix="nvidia"
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
                    if perm_url:
                        result += (
                            f"\nPermanent URL: {perm_url} "
                            "(never expires — use this in websites, <img> tags, or curl)"
                        )
                    if local_path:
                        result += (
                            f"\nLocal path: {local_path} "
                            f'(pass to create_site as images=[{{"path": "{local_path}"}}] '
                            "to bundle it into a site)"
                        )
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

        # Persist a permanent public copy (Discord CDN URLs expire ~24h).
        local_path, perm_url = _persist_public_image(self.bot, image_bytes, prefix="hd")

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
        if perm_url:
            result += (
                f"\nPermanent URL: {perm_url} "
                "(never expires — use this directly in HTML <img> tags or curl)"
            )
        if local_path:
            result += (
                f"\nLocal path: {local_path} "
                f'(pass to create_site as images=[{{"path": "{local_path}"}}] '
                "to bundle it into a site)"
            )
        return result


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
            "Set visible activity or custom status. Call only when asked or "
            "after a real state change — not every turn. "
            "Params: type (playing/watching/listening/competing/custom), text, "
            "elapsed (optional). type='custom' for a plain status. text='' clears."
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
            "Sleep 1-60 minutes (default 30). While asleep, LLM turns are skipped "
            "and pings/DMs get one 'max is sleeping' notice. Use only at a real "
            "end-of-conversation, not as a goodbye. Calling again resets the window. "
            "Params: duration_minutes."
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
        if not self.bot._is_admin(message.author.id):
            return "Error: sleep is admin-only"
        return self.bot.set_sleep(n)


class ClearSleepTool(Tool):
    """Cancel an active sleep window. Idempotent — safe to call when
    not sleeping. Use when the bot decided to sleep but the user
    immediately needs a reply."""

    is_destructive: bool = False
    streams_output: bool = False

    def get_description(self):
        return (
            "Cancel the active sleep window and wake immediately. "
            "Use only if you just slept and the user still needs you."
        )

    async def execute(self, message: Message, **kwargs) -> str:
        if self.bot is None:
            return "Error: bot not attached"
        if not self.bot._is_admin(message.author.id):
            return "Error: clear_sleep is admin-only"
        return self.bot.clear_sleep()


class WaitTool(Tool):
    """Pause the current tool batch for N seconds before continuing.

    Use this WITHIN a single turn to space out multiple actions — e.g.
    `send_message('starting...')` → `wait(2)` → `send_message('done!')`
    for a staged reveal, or `send_message('countdown: 3')` → `wait(1)` →
    `send_message('2')` → `wait(1)` → `send_message('1')` → `wait(1)` →
    `send_message('go!')`.

    Distinct from `sleep`: `sleep` turns off the bot for minutes (rest),
    `wait` is a sub-turn pause that keeps the turn open and lets you
    follow up with more tool calls.

    Max is 10 seconds — longer pauses should use `sleep` instead. The
    user-visible progress message updates to 'waiting Ns…' so they
    know the bot isn't stuck."""

    is_destructive: bool = False
    streams_output: bool = False

    def get_description(self):
        return (
            "Pause this tool batch (`wait`) for `seconds` (float, default 2, max 10). "
            "Turn stays open. For spacing separate send_messages only — not "
            "to chunk a normal reply. Distinct from sleep (minutes, ends dispatch)."
        )

    async def execute(
        self,
        message: Message,
        seconds: float | str = 2.0,
        **kwargs,
    ) -> str:
        try:
            n = float(seconds)
        except (TypeError, ValueError):
            n = 2.0
        if n < 0:
            n = 0.0
        if n > 10:
            # Hard cap. The model can't override this; longer pauses belong
            # in `sleep`. The error is visible to the model so it can
            # adjust instead of silently truncating.
            return "Error: wait duration capped at 10 seconds. Use `sleep` for longer pauses."
        await asyncio.sleep(n)
        return f"Waited {n:.1f}s"


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


# The Discord host/path is REQUIRED before the capture group. An optional
# prefix made ``https://discord.gg/xyz`` match the scheme token ``https``,
# so every HTTPS invite joined discord.gg/https instead of the target.
_INVITE_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:(?:ptb|canary)\.)?"
    r"(?:discord(?:app)?\.com/invite|discord\.gg)/"
    r"([a-zA-Z0-9_-]{2,32})",
    re.IGNORECASE,
)
_INVITE_BARE_RE = re.compile(r"^[a-zA-Z0-9_-]{2,32}$")
_INVITE_PARAM_KEYS = ("invite", "url", "link", "code", "invite_url", "invite_code")


def _invite_raw_from_params(invite: Any = None, kwargs: dict | None = None) -> str:
    """Return the first non-empty invite string from the primary arg or aliases."""
    values: list[Any] = [invite]
    if kwargs:
        for key in _INVITE_PARAM_KEYS:
            if key == "invite":
                continue
            values.append(kwargs.get(key))
    for val in values:
        if isinstance(val, (list, tuple)) and val:
            val = val[0]
        text = str(val or "").strip()
        if text:
            return text
    return ""


def _extract_invite_code(invite: str) -> str:
    """Normalize an invite to its bare code.

    Accepts ``discord.gg/xyz``, ``https://discord.com/invite/xyz``,
    ``discordapp.com/invite/xyz``, ptb/canary hosts, Discord ``<>``
    markdown, query strings, or a bare code like ``xyz``.
    """
    invite = (invite or "").strip().strip("<>").strip()
    if not invite:
        return ""
    m = _INVITE_URL_RE.search(invite)
    if m:
        return m.group(1)
    if "/" in invite or "://" in invite:
        return ""
    if _INVITE_BARE_RE.fullmatch(invite):
        return invite
    return ""


_VERIFY_CHANNEL_KEYWORDS = (
    "verify",
    "captcha",
    "wick",
    "gate",
    "verification",
    "human",
    "onboarding",
)


def _solver_status(bot) -> str:
    """Describe whether a captcha solver is configured (for tool results)."""
    cfg = getattr(bot, "config", None)
    if cfg is None:
        return ""
    service = getattr(cfg, "CAPTCHA_SOLVER_SERVICE", "") or ""
    key = getattr(cfg, "CAPTCHA_SOLVER_API_KEY", "") or ""
    if service and key:
        return f" (auto-solver {service} is configured)"
    return " (no captcha solver configured — CAPTCHA_SOLVER_SERVICE/CAPTCHA_SOLVER_API_KEY)"


def _format_captcha(e) -> str:
    """Render a CaptchaRequired exception into a readable, complete report."""
    parts = []
    parts.append(f"service={e.service}")
    parts.append(f"sitekey={e.sitekey}")
    if e.session_id:
        parts.append(f"session_id={e.session_id}")
    if e.rqdata:
        parts.append(f"rqdata={e.rqdata}")
    if e.rqtoken:
        parts.append(f"rqtoken={e.rqtoken}")
    parts.append(f"invisible={e.should_serve_invisible}")
    errors = getattr(e, "errors", None) or []
    if errors:
        parts.append(f"reason={', '.join(str(x) for x in errors)}")
    return " | ".join(parts)


class JoinServerTool(Tool):
    """Join a Discord server via invite link or code."""

    def get_description(self):
        return (
            "Join a Discord server via invite code or full invite URL "
            "(https://discord.gg/code). Always pass the exact link or code "
            "the user gave — do not invent or reuse another invite. "
            "Reports name, gates, captcha, and errors. Params: invite (required)."
        )

    async def execute(
        self, message: Message, invite: str | None = None, **kwargs
    ) -> str:
        if self.bot and not self.bot._is_admin(message.author.id):
            return "Error: join_server is admin-only"
        raw = _invite_raw_from_params(invite, kwargs)
        code = _extract_invite_code(raw)
        if not code:
            return (
                "Error: could not parse an invite code from "
                f"'{raw or invite}'. Pass a link like discord.gg/xyz or a bare code."
            )
        try:
            inv = await self.bot.fetch_invite(code, with_counts=True)
        except discord.NotFound:
            return f"Error: invite '{code}' is invalid or expired"
        except discord.Forbidden:
            return (
                f"Error: blocked from fetching invite '{code}' "
                "(banned from server or invite disabled)"
            )
        except discord.HTTPException as e:
            return f"Error fetching invite '{code}': HTTP {e.status}: {e.text[:200]}"
        except Exception as e:
            return f"Error fetching invite '{code}': {type(e).__name__}: {e}"

        g = inv.guild
        gname = g.name if g else "unknown server"
        gid = g.id if g else None
        members = getattr(inv, "approximate_member_count", None)
        features = list(g.features) if g else []
        level = g.verification_level.name if g and g.verification_level else "unknown"
        lines = [
            f"Invite ok — {gname} (ID: {gid}) {members or '?'} members, verification={level}"
        ]
        if features:
            lines.append(f"  features: {', '.join(features)}")

        if gid and self.bot.get_guild(gid):
            return "\n".join(lines + [f"Already in {gname} — no join needed."])

        manual_approval = "MEMBER_VERIFICATION_MANUAL_APPROVAL" in features
        if manual_approval:
            lines.append(
                "  NOTE: server uses MANUAL APPROVAL — join will be pending until an admin approves."
            )

        try:
            await inv.accept()
        except discord.CaptchaRequired as e:
            # The global client captcha handler (bot._handle_captcha) already
            # tried the auto-solver and/or DM-based human solve. If it raised,
            # we get here — post the solve link right in this channel so the
            # person who asked for the join can complete it, then re-submit
            # the invite accept with the solved token.
            human = bool(
                getattr(getattr(self.bot, "config", None), "CAPTCHA_HUMAN_SOLVE", False)
            )
            if human:
                channel = getattr(message, "channel", None)

                async def _notify_in_channel(url: str) -> None:
                    try:
                        if channel is not None:
                            await channel.send(
                                "⚠️ CAPTCHA required to join "
                                + gname
                                + ". Solve here (expires ~2 min): "
                                + url
                            )
                    except Exception as ex:
                        logger.warning("captcha in-channel notify failed: %s", ex)

                try:
                    token = await self.bot._solve_captcha_with_notify(
                        e, notify=_notify_in_channel
                    )
                except CaptchaSolveError as se:
                    return (
                        "\n".join(lines)
                        + f"\nCAPTCHA REQUIRED to join {gname}: {_format_captcha(e)}"
                        + _solver_status(self.bot)
                        + f"\nHuman solve failed: {se}"
                    )
                try:
                    data = await self.bot._retry_invite_with_captcha(code, e, token)
                except discord.HTTPException as he:
                    return (
                        "\n".join(lines)
                        + f"\nCAPTCHA solved but join retry failed: HTTP {he.status}: "
                        + (he.text[:200] if he.text else "")
                    )
                except Exception as ex:
                    return (
                        "\n".join(lines)
                        + f"\nCAPTCHA solved but join retry failed: {type(ex).__name__}: {ex}"
                    )
                gid2 = None
                if isinstance(data, dict):
                    gid2 = (data.get("guild") or {}).get("id")
                joined_guild = None
                for _ in range(12):
                    joined_guild = (
                        self.bot.get_guild(gid2 or gid) if (gid2 or gid) else None
                    )
                    if joined_guild is not None:
                        break
                    await asyncio.sleep(1)
                if joined_guild is not None:
                    onboard_note = ""
                    try:
                        onboard = await self.bot._auto_onboard(
                            joined_guild, detail=True
                        )
                        if onboard.get("ok"):
                            onboard_note = "\n" + str(onboard.get("summary") or "")
                    except Exception as ex:
                        logger.debug("auto-onboard (captcha join) failed: %s", ex)
                    return (
                        "\n".join(lines)
                        + f"\nJOINED {joined_guild.name} (ID: {joined_guild.id}) — "
                        + "captcha was solved via the posted link."
                        + onboard_note
                    )
                return (
                    "\n".join(lines)
                    + "\nCAPTCHA solved and join re-submitted — waiting on the "
                    + "guild to appear in cache. Check list_servers shortly."
                )
            return (
                "\n".join(lines)
                + f"\nCAPTCHA REQUIRED to join {gname}: {_format_captcha(e)}"
                + _solver_status(self.bot)
                + "\nJoin blocked until the captcha is solved."
            )
        except discord.NotFound as e:
            return f"Error joining '{code}': invite invalid/expired (HTTP {e.status})"
        except discord.Forbidden as e:
            return (
                f"Error joining {gname}: forbidden (HTTP {e.status}) — "
                "likely banned from the server, or the invite was revoked "
                "between fetch and accept."
            )
        except discord.HTTPException as e:
            detail = e.text[:200] if e.text else ""
            if e.status == 429:
                return (
                    f"Error joining {gname}: rate limited (429). "
                    "Wait a bit and retry — Discord throttles rapid joins."
                )
            return f"Error joining {gname}: HTTP {e.status}: {detail}"
        except Exception as e:
            return f"Error joining {gname}: {type(e).__name__}: {e}"

        # Wait for the guild to land in the cache (gateway round-trip; large
        # guilds with member chunking can take a while).
        joined_guild = None
        for _ in range(25):
            joined_guild = self.bot.get_guild(gid) if gid else None
            if joined_guild is not None:
                break
            await asyncio.sleep(1)

        if joined_guild is None:
            if manual_approval:
                return (
                    "\n".join(lines)
                    + "\nJOIN REQUEST SUBMITTED — the server uses MANUAL APPROVAL, "
                    "so membership is pending until an admin approves. "
                    "The bot is NOT inside the server yet."
                )
            # Confirm membership from the API before claiming anything — the
            # gateway cache can lag behind the actual accept.
            confirmed = False
            try:
                from discord.http import Route

                gdata = await self.bot.http.request(
                    Route("GET", "/guilds/{guild_id}", guild_id=gid),
                    params={"with_counts": "true"},
                )
                confirmed = bool(gdata and gdata.get("id"))
            except Exception:
                confirmed = False
            if confirmed:
                gname2 = (gdata or {}).get("name") or gname
                return (
                    "\n".join(lines)
                    + f"\nJOINED {gname2} (ID: {gid}) — confirmed via API. "
                    "The gateway cache is still syncing; list_servers will show it shortly."
                )
            return (
                "\n".join(lines)
                + "\nJoin result uncertain — the invite was accepted but the "
                "guild is not visible yet. Ask the server owner to confirm, "
                "then check list_servers."
            )

        lines.append(
            f"JOINED {joined_guild.name} (ID: {joined_guild.id}, "
            f"members={joined_guild.member_count})"
        )

        # Auto-complete the server's onboarding flow (role selection prompts)
        # so role-gated servers are usable right away.
        try:
            onboard = await self.bot._auto_onboard(joined_guild, detail=True)
            if onboard.get("ok"):
                lines.append(f"  {onboard.get('summary')}")
                roles = len(onboard.get("role_ids") or [])
                if roles:
                    lines.append(
                        f"  picked up {roles} role(s) — run server_setup to change them."
                    )
            elif onboard.get("prompts"):
                # Prompts exist but the submit didn't land: say so, because the
                # account is role-less in there until someone retries.
                lines.append(f"  onboarding NOT completed: {onboard.get('summary')}")
        except Exception as ex:
            logger.debug("auto-onboard via join tool failed: %s", ex)

        # Post-join verification gates (Wick captcha-on-join, verify channels,
        # MEE6/Bloxlink-style role gates). These aren't API errors — the join
        # worked, but the account is usually role-locked until it completes
        # the gate.
        gate_channels = [
            ch.name
            for ch in joined_guild.channels
            if any(k in (ch.name or "").lower() for k in _VERIFY_CHANNEL_KEYWORDS)
        ][:6]
        if gate_channels:
            lines.append(
                f"  NOTE: verification gate channels present: {', '.join(gate_channels)} — "
                "the account may be role-locked until it completes verification there."
            )
        result = "\n".join(lines)
        logger.info(
            "join_server result for %s: %s", code, result.replace("\n", " | ")[:400]
        )
        return result


def _resolve_guild(guilds: list, target: str) -> tuple[Any, str]:
    """Find one guild by ID, exact name, then unique partial name.

    Returns (guild, "") on a hit and (None, error_text) otherwise, so both
    leave_server and server_setup report ambiguity the same way.
    """
    target = (target or "").strip()
    if not guilds:
        return None, "Error: not in any servers"
    if not target:
        return None, "Error: no server given"
    if target.isdigit():
        guild = next((g for g in guilds if str(g.id) == target), None)
        if guild is not None:
            return guild, ""
    lowered = target.lower()
    guild = next((g for g in guilds if (g.name or "").lower() == lowered), None)
    if guild is not None:
        return guild, ""
    matches = [g for g in guilds if lowered in (g.name or "").lower()]
    if len(matches) == 1:
        return matches[0], ""
    if len(matches) > 1:
        return None, (
            f"Error: '{target}' matches {len(matches)} servers "
            + ", ".join(f"{g.name} ({g.id})" for g in matches[:8])
            + " — use the numeric ID to disambiguate."
        )
    return None, (
        f"Error: not in any server named/matching '{target}'. "
        "Use list_servers to see current servers."
    )


class ServerSetupTool(Tool):
    """Pick your own roles and channels through a server's onboarding prompts."""

    def get_description(self):
        return (
            "Set yourself up in a server: read its onboarding prompts (the "
            "'what roles do you want' / channel pickers) and choose the options "
            "you actually want. Use this when you're in a server but have no "
            "roles or can't see the channels. Runs automatically on join, so "
            "this is for servers you're already in or to change your picks. "
            "Params: server (name or ID; defaults to the current server), "
            "preferences (optional steer, e.g. 'only AI and coding stuff'), "
            "list_only (true to see the options without picking anything)."
        )

    async def execute(
        self,
        message: Message,
        server: str | None = None,
        preferences: str | None = None,
        list_only: bool = False,
        **kwargs,
    ) -> str:
        target = (server or "").strip()
        if target:
            guild, err = _resolve_guild(list(self.bot.guilds or []), target)
            if guild is None:
                return err
        else:
            guild = getattr(message, "guild", None)
            if guild is None:
                return (
                    "Error: no server given and this isn't a server channel. "
                    "Pass server (name or ID) — list_servers shows them."
                )

        # Models hand booleans over as "true"/"false" strings often enough
        # that bool("false") would silently flip this into a real submit.
        dry_run = parse_bool(list_only, False)
        try:
            result = await self.bot._auto_onboard(
                guild,
                preferences=(preferences or ""),
                dry_run=dry_run,
                detail=True,
            )
        except Exception as e:
            return f"Error setting up {guild.name}: {type(e).__name__}: {e}"

        prompts = result.get("prompts") or []
        lines = [f"{guild.name} (ID: {guild.id})"]
        if not prompts:
            lines.append(
                f"  {result.get('summary')} — nothing to pick here. If you still "
                "can't see channels, the server gates on verification, not onboarding."
            )
            return "\n".join(lines)

        lines.append(f"  prompts: {len(prompts)}")
        for prompt in prompts:
            rule = "one" if prompt["single_select"] else "any"
            chosen = set((result.get("choice") or {}).get(prompt["id"], []))
            lines.append(f'  • "{prompt["title"]}" (pick {rule}):')
            for opt in prompt["options"]:
                mark = "✓" if opt["id"] in chosen else " "
                desc = f' — {opt["description"]}' if opt["description"] else ""
                lines.append(f'      [{mark}] {opt["title"]}{desc}'[:240])

        roles = [
            r.name
            for r in (
                guild.get_role(int(rid)) for rid in (result.get("role_ids") or [])
            )
            if r is not None
        ]
        channels = [
            c.name
            for c in (
                guild.get_channel(int(cid)) for cid in (result.get("channel_ids") or [])
            )
            if c is not None
        ]
        if not result.get("ok"):
            lines.append(f"  FAILED: {result.get('summary')}")
            return "\n".join(lines)
        verb = "would take" if dry_run else "took"
        lines.append(f"  {result.get('summary')}")
        if roles:
            lines.append(f"  {verb} roles: {', '.join(roles)}")
        if channels:
            lines.append(f"  {verb} channels: {', '.join(channels)}")
        if result.get("picked_by") == "fallback":
            lines.append(
                "  NOTE: the model call failed, so these are first-option "
                "defaults — rerun to choose properly."
            )
        return "\n".join(lines)


class LeaveServerTool(Tool):
    """Leave a Discord server by name or ID."""

    def get_description(self):
        return (
            "Leave a Discord server. Pass the server name or numeric ID "
            "(use list_servers to find them). Matches by ID first, then "
            "exact name, then partial/unique name. Reports errors clearly. "
            "Params: server (required)."
        )

    async def execute(
        self, message: Message, server: str | None = None, **kwargs
    ) -> str:
        if self.bot and not self.bot._is_admin(message.author.id):
            return "Error: leave_server is admin-only"
        target = (server or "").strip()
        if not target:
            return "Error: leave_server requires a server name or ID"

        guild, err = _resolve_guild(list(self.bot.guilds or []), target)
        if guild is None:
            return err

        try:
            await guild.leave()
            return f"LEFT {guild.name} (ID: {guild.id})"
        except discord.Forbidden as e:
            return f"Error leaving {guild.name}: forbidden (HTTP {e.status})"
        except discord.NotFound as e:
            return f"Error leaving {guild.name}: guild not found (HTTP {e.status})"
        except discord.HTTPException as e:
            return f"Error leaving {guild.name}: HTTP {e.status}: " + (
                e.text[:200] if e.text else ""
            )
        except Exception as e:
            return f"Error leaving {guild.name}: {type(e).__name__}: {e}"


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
        if not message.guild and not getattr(message, "channel", None):
            return "Error: Channel context unavailable"
        try:
            search_limit = max(1, min(int(limit), 25))
            results = []
            # If query is empty or blank, fetch recent channel history instead of failing
            if not query or not str(query).strip():
                chan = getattr(message, "channel", None)
                if chan and hasattr(chan, "history"):
                    async for msg in chan.history(limit=search_limit):
                        snippet = msg.content[:150] + ("..." if len(msg.content) > 150 else "")
                        results.append(f"[{msg.id}] {msg.author.display_name}: {snippet}")
                    if not results:
                        return "No recent messages found in this channel"
                    return f"Recent messages ({len(results)}):\n" + "\n".join(results)
                return "Error: query is required"

            if not message.guild:
                return "Error: Cannot search by keyword in DMs"
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
        position: str | None = None,
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
        if position is not None:
            try:
                updates["position"] = max(0, int(position))
            except (TypeError, ValueError):
                return "Error: position must be a number"
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
        return (
            "Change your profile picture. Params: url (direct jpg/png/gif/webp). "
            "Discord rate-limits spam."
        )

    async def execute(self, message: Message, url: str | None = None, **kwargs) -> str:
        if self.bot and not self.bot._is_admin(message.author.id):
            return "Error: change_avatar is admin-only"
        if not url:
            return "Error: url is required"

        if not _is_safe_url(url):
            return "Error: Cannot fetch from private/internal URLs"

        # Local cooldown fully removed — was previously env-driven
        # (AVATAR_COOLDOWN_SECONDS, default 0). Discord's own API rate limit
        # is the only throttle left; the bot will get a 429 from Discord if
        # it spams, which is fine.

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
            return "Avatar changed successfully"
        except discord.HTTPException as e:
            return f"Error changing avatar: {e}"
        except Exception as e:
            return f"Error: {e}"


def _find_html_tag_end(text: str, start: int) -> int | None:
    """Return the end of an HTML tag, respecting quoted attributes."""
    quote = ""
    for index in range(start + 1, len(text)):
        char = text[index]
        if quote:
            if char == quote:
                quote = ""
        elif char in {'"', "'"}:
            quote = char
        elif char == ">":
            return index
    return None


def _normalize_site_body_text_escapes(body: str) -> str:
    r"""Turn escaped whitespace into real whitespace in HTML text nodes.

    A native tool call has two layers of JSON escaping. Models sometimes leave
    the resulting ``\n`` characters in visible HTML text instead of emitting a
    real line break, so a page displays ``\n`` literally. Normalize only text
    outside tags, ``<script>``, and ``<style>`` blocks:

    - visible HTML/``<pre>`` text gets real newlines, tabs, and carriage returns;
    - JavaScript, CSS, JSON script blocks, and attributes stay byte-for-byte
      intact because ``\n`` is often intentional there.

    Base64 site bodies bypass this helper because base64 is the exact-bytes
    escape hatch documented by the tool.
    """
    if not isinstance(body, str) or not body:
        return body

    out: list[str] = []
    i = 0
    changed = False
    raw_tag = ""
    whitespace = {"n": "\n", "r": "\r", "t": "\t"}

    while i < len(body):
        if raw_tag:
            close = re.search(rf"</\s*{raw_tag}\s*>", body[i:], re.IGNORECASE)
            if close is None:
                out.append(body[i:])
                break
            close_end = i + close.end()
            out.append(body[i:close_end])
            i = close_end
            raw_tag = ""
            continue

        # A literal ``<`` is common in code samples inside <pre>. Only treat
        # it as markup when the next character can begin a real HTML tag;
        # otherwise it remains ordinary text and escaped whitespace is still
        # normalized after it.
        if body[i] == "<" and (
            i + 1 < len(body)
            and body[i + 1].isalpha()
            or i + 1 < len(body)
            and body[i + 1] in {"/", "!", "?"}
        ):
            tag_end = _find_html_tag_end(body, i)
            if tag_end is None:
                # Malformed/truncated markup: don't reinterpret the rest of
                # the body as text and potentially change code in it.
                out.append(body[i:])
                break
            tag = body[i : tag_end + 1]
            out.append(tag)
            raw_open = re.match(r"<\s*(script|style)\b", tag, re.IGNORECASE)
            if raw_open and not tag.rstrip().endswith("/>"):
                raw_tag = raw_open.group(1)
            i = tag_end + 1
            continue

        if body[i] == "\\":
            slash_start = i
            while i < len(body) and body[i] == "\\":
                i += 1
            slash_count = i - slash_start
            if i < len(body) and body[i] in whitespace and slash_count % 2:
                # Preserve paired backslashes and decode only the final,
                # unpaired escape: ``\\n`` remains literal ``\n`` while
                # ``\n`` becomes an actual newline.
                out.append("\\" * (slash_count - 1))
                out.append(whitespace[body[i]])
                i += 1
                changed = True
            else:
                out.append("\\" * slash_count)
            continue

        out.append(body[i])
        i += 1

    return "".join(out) if changed else body


class CreateSiteTool(Tool):
    """Create a temporary website under the configured public /bot path."""

    MAX_CONTENT_SIZE = 3000000  # 3MB for big single-file 3D scenes, full movie recreations, complex interactive demos etc. (use base64 encoding in tool call for safety)

    async def _download_site_image(
        self, url: str, img_dir: str, filename_hint=None
    ) -> tuple[str | None, str | None]:
        """Download an image from a URL into img_dir for a site.

        Returns (dest_path, None) on success, (None, error) on failure.
        Lets create_site consume image URLs directly (Discord CDN, the
        permanent image URLs from image_generator, external hosts) instead
        of requiring a local path.
        """
        if not _is_safe_url(url):
            return None, "unsafe URL"
        try:
            session = await _get_shared_session()
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=30, connect=10),
                allow_redirects=False,
            ) as resp:
                if resp.status != 200:
                    return None, f"HTTP {resp.status}"
                content_type = (
                    (resp.headers.get("Content-Type") or "")
                    .split(";", 1)[0]
                    .strip()
                    .lower()
                )
                if not content_type.startswith("image/"):
                    return None, f"not an image ({content_type or 'unknown'})"
                blob = await _read_response_limited(resp, 10 * 1024 * 1024)
        except Exception as e:
            return None, str(e)[:120]
        if not blob:
            return None, "empty body"
        ext = Path(urlparse(url).path).suffix.lower()
        if ext not in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}:
            ext = ".png"
        filename = str(
            filename_hint or f"site-image-{int(datetime.now(timezone.utc).timestamp())}"
        )
        filename = re.sub(r"[^a-zA-Z0-9._-]", "_", filename).strip(".")
        filename = re.sub(r"^[.\\/-]+", "", filename)
        if not filename or filename in {".", ".."}:
            filename = "image"
        if not os.path.splitext(filename)[1]:
            filename += ext
        dest = os.path.join(img_dir, filename)
        if os.path.commonpath(
            [os.path.abspath(dest), os.path.abspath(img_dir)]
        ) != os.path.abspath(img_dir):
            return None, "filename escapes images dir"
        try:
            with open(dest, "wb") as f:
                f.write(blob)
        except Exception as e:
            return None, f"write failed: {e}"
        return dest, None

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
            f"Create a temporary website at {self.base_url}/<name> (auto-deletes in 24h). "
            "Params: name (slug), title, body (complete HTML document with inline CSS/JS; "
            "real line breaks or <br> in visible text, never literal \\n), "
            "encoding (text|base64). Generate images in a prior turn and paste CDN URLs "
            "into the HTML — don't batch image_generator with create_site. "
            "Never paste HTML into chat."
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
        elif not isinstance(body, str):
            return "Error: site body must be a string"
        else:
            normalized_body = _normalize_site_body_text_escapes(body)
            if normalized_body != body:
                logger.info(
                    "Normalized escaped whitespace in create_site body (%d chars changed)",
                    sum(a != b for a, b in zip(body, normalized_body, strict=False))
                    + abs(len(body) - len(normalized_body)),
                )
                body = normalized_body

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
        already_ours = (
            isinstance(existing, dict)
            and str(existing.get("user_id") or "") == user_id
        )
        if not already_ours and len(active_user_sites) >= max_sites:
            return f"Error: site quota reached ({len(active_user_sites)}/{max_sites} active sites). Delete an old site first."

        if len(body) > self.MAX_CONTENT_SIZE:
            return f"Error: content too long ({len(body)} chars, max {self.MAX_CONTENT_SIZE})"

        site_dir = os.path.join(self.base_dir, slug)
        created_new_dir = not os.path.isdir(site_dir)
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
                    src_url = str(entry.get("url") or "").strip()
                    src_path = entry.get("path", "")
                    if src_url and not src_path:
                        # URL entries: download the image into the site's
                        # images/ dir so the site is fully self-hosted and
                        # never depends on an expiring external link.
                        dest, err = await self._download_site_image(
                            src_url, img_dir, entry.get("filename")
                        )
                        if dest:
                            public_url = f"{self.base_url}/{slug}/images/{os.path.basename(dest)}"
                            image_urls.append(public_url)
                            logger.info(f"Downloaded site image {src_url} -> {dest}")
                        else:
                            missing_images.append(src_url)
                            logger.warning(
                                f"Site image URL failed: {src_url} ({err or 'unknown'})"
                            )
                        continue
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
                # Best-effort cleanup of orphaned HTML only when this call
                # created the directory. Never rmtree a slug another user
                # already committed.
                if created_new_dir:
                    with contextlib.suppress(Exception):
                        shutil.rmtree(site_dir, ignore_errors=True)
                logger.error(f"Failed to commit site metadata for {slug}: {e}")
                return f"Error creating site: {e}"
            if not committed:
                # Overwrite disallowed by a concurrent owner change / quota hit
                # discovered under the lock; clean up only a directory we created.
                if created_new_dir:
                    with contextlib.suppress(Exception):
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
                logger.error(f"Corrupt sites.json on commit, aborting: {e}")
                return False
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
            with contextlib.suppress(OSError):
                self.bot._sites_mtime = path.stat().st_mtime
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


_WEB_REPLY_CTX_RE = re.compile(r"\[Latest message replies to[^\]]*\]", re.IGNORECASE)


def _sanitize_web_query(query: str | None) -> str:
    """Drop Discord reply-context glue so searches stay on the user's words."""
    q = str(query or "")
    q = _WEB_REPLY_CTX_RE.sub(" ", q)
    q = re.split(r"\n?\[Latest message replies to", q, maxsplit=1, flags=re.IGNORECASE)[
        0
    ]
    q = re.sub(r"\[RESPOND TO THIS\]\s*", "", q, flags=re.IGNORECASE)
    return " ".join(q.split()).strip()[:160]


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
        query = _sanitize_web_query(query)
        if not query:
            return "Error: query is required"
        if not _DDGS_AVAILABLE:
            return (
                "Error: web_search is not available in this install — the "
                "`ddgs` Python package is missing. Run `pip install ddgs` "
                "or set ENABLE_WEB_SEARCH=false in .env to silence this."
            )
        # _DDGS is guaranteed non-None when _DDGS_AVAILABLE is True, but pyright
        # can't see the correlation across the lambda below. Bind a local
        # non-None reference so a None can never be called at runtime.
        if _DDGS is None:
            return "Error: web_search is not available (ddgs import failed)"
        ddgs_cls: Any = _DDGS

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
                    None, lambda: list(ddgs_cls().text(query, max_results=limit))
                ),
                timeout=30,
            )

            if not results:
                return f"No results found for '{query}'"

            # ─── persist to RAG (operator feature 2026-08-09) ───
            # Embed top results as kind='web_result' so future turns in
            # the same conversation can recall what was just searched.
            # Off by default in the env var, but defaults ON for new
            # installs. Skipped silently if RAG is unavailable or
            # disabled — never fails the search.
            try:
                rag_enabled = bool(
                    getattr(self.bot.config, "RAG_WEB_STORE_ENABLED", True)
                )
                memory = getattr(self.bot, "memory", None)
                if (
                    rag_enabled
                    and memory is not None
                    and hasattr(memory, "store_web_results")
                ):
                    guild_id = ""
                    if message is not None and getattr(message, "guild", None):
                        guild_id = str(message.guild.id)
                    n = await memory.store_web_results(
                        query=query,
                        results=list(results),
                        guild_id=guild_id,
                    )
                    if n:
                        logger.info(
                            f"web_search stored {n} results for query={query!r}"
                        )
            except Exception as e:
                logger.debug(f"web_search RAG persistence skipped: {e}")

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
            "Send a message to the current chat. Default: one call per turn with the full reply. "
            "You can call this more than once if you actually want separate Discord messages; do not split a normal reply. "
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
        sent_any = False
        sent_chunks: list[str] = []
        try:
            chunks = self._chunks(text)
            use_reply = str(reply).lower() not in {"0", "false", "no", "off"}
            for i, chunk in enumerate(chunks):
                try:
                    if i == 0 and use_reply:
                        try:
                            await message.reply(chunk)
                        except (discord.NotFound, discord.HTTPException) as exc:
                            code = getattr(exc, "code", None)
                            parent_gone = isinstance(exc, discord.NotFound) or code in {
                                10008,
                                50035,
                            }
                            if code == 50035 and "message_reference" not in str(exc).lower():
                                raise
                            if not parent_gone:
                                raise
                            await message.channel.send(chunk)
                    else:
                        await message.channel.send(chunk)
                    sent_any = True
                    sent_chunks.append(chunk)
                except Exception:
                    if sent_any:
                        return "__MESSAGE_SENT__\n" + "\n".join(sent_chunks)
                    raise
                if len(chunks) > 1:
                    await asyncio.sleep(0.2)
            # Return the marker followed by the actual sent content. The
            # content is what the bot said, so recalling it in memory is
            # correct. We intentionally do NOT include leakable debug prose
            # like "Sent N chars in M chunk(s)": that prose reads as natural
            # language and the model echoed it into visible replies
            # ("20 chars in 1 chunk(s)" appeared in chat). Downstream detects
            # send_message via " __MESSAGE_SENT__" in the result string.
            return f"__MESSAGE_SENT__\n{text}"
        except discord.Forbidden:
            return "Error: missing permissions to send message"
        except Exception as e:
            if sent_any:
                return f"__MESSAGE_SENT__\n{text}"
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
            "Create or send a file attachment. Params: filename + content "
            "(inline), or path (existing file / container path). "
            "encoding=text|base64 (prefer base64 for code/HTML). "
            "A path is not delivery — this tool attaches the file."
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
        # Intentionally NOT admin-gated. send_file is an output channel —
        # the model already has shell + every other tool to produce content,
        # and gating the return path on `_is_admin` was just a barrier that
        # blocked non-admin users from receiving files. The path-mode
        # allowlist (_allowed_send_file_bases) is the real safety boundary.
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
                tmp_to_clean = None
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
                tmp_to_clean = target

            try:
                blob = await asyncio.to_thread(target.read_bytes)
            except Exception as e:
                return f"Error reading file from disk: {e}"
            finally:
                if tmp_to_clean is not None:
                    with contextlib.suppress(Exception):
                        shutil.rmtree(tmp_to_clean.parent, ignore_errors=True)
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
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(Exception):
                    await proc.wait()
                with contextlib.suppress(Exception):
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                return None, "docker cp timed out"
            except asyncio.CancelledError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(Exception):
                    await proc.wait()
                with contextlib.suppress(Exception):
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                raise
            if proc.returncode != 0:
                with contextlib.suppress(Exception):
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                return None, (
                    stderr.decode(errors="replace").strip()
                    or f"docker cp exit {proc.returncode}"
                )
            if not os.path.isfile(local_path):
                with contextlib.suppress(Exception):
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                return None, "docker cp reported success but file is missing"
            return Path(local_path), None
        except FileNotFoundError:
            with contextlib.suppress(Exception):
                shutil.rmtree(tmp_dir, ignore_errors=True)
            return None, "docker is not installed or not on PATH"
        except Exception as e:
            with contextlib.suppress(Exception):
                shutil.rmtree(tmp_dir, ignore_errors=True)
            return None, f"docker cp failed: {e}"

    async def _send_blob(self, message: Message, blob: bytes, safe_name: str) -> str:
        if len(blob) > self.MAX_SIZE:
            return f"Error: file is too large (max {self.MAX_SIZE // 1024 // 1024} MB)"

        file = File(BytesIO(blob), filename=safe_name)
        sent = None
        try:
            try:
                sent = await message.reply(file=file)
            except (discord.NotFound, discord.HTTPException) as exc:
                code = getattr(exc, "code", None)
                parent_gone = isinstance(exc, discord.NotFound) or code in {
                    10008,
                    50035,
                }
                if code == 50035 and "message_reference" not in str(exc).lower():
                    raise
                if not parent_gone:
                    raise
                sent = await message.channel.send(file=file)
        except discord.Forbidden:
            return "Error: no permission to send files here"
        except discord.HTTPException as e:
            return f"Error sending file: {e}"
        except Exception as e:
            return f"Error sending file: {e}"

        # Every piece of media gets its URL attached: the sent Discord
        # attachment carries a CDN URL the model can curl/pull/reuse.
        file_url = ""
        if sent is not None and getattr(sent, "attachments", None):
            file_url = sent.attachments[0].url
        result = f"__FILE_SENT__ Sent file: {safe_name} ({len(blob)} bytes)"
        if file_url:
            result += f"\nFile URL: {file_url}"
        return result


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
    # Channel post cap. Captured stdout can be 100k for the model, but posting
    # that as Discord ```ansi``` chunks floods the chat. Visible dump is ~300
    # chars (one short codeblock); the LLM still gets the longer capture.
    _CHANNEL_MAX_CHARS_DEFAULT = 300
    _CHANNEL_MAX_CHUNKS = 1

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
    def _channel_max_chars(cls) -> int:
        """Max chars posted to the chat for one shell call. 0 = unlimited."""
        raw = os.environ.get("MAXWELL_SHELL_CHANNEL_MAX_CHARS", "").strip()
        if not raw:
            return cls._CHANNEL_MAX_CHARS_DEFAULT
        try:
            v = int(raw)
        except ValueError:
            return cls._CHANNEL_MAX_CHARS_DEFAULT
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
        chan = self._channel_max_chars()
        chan_str = "unlimited" if chan == 0 else f"{chan} chars"
        limits_note = (
            f"Limits: command <= {max_cmd_str}, output <= {max_out_str}, "
            f"channel preview <= {chan_str}, timeout {to}s."
        )
        how = (
            "To write a file, put the redirect on the opener line: "
            "`cat << 'EOF' > path/file.py` then the body then a line containing "
            "only EOF. `cmd` is an alias for `command`. Do not prefix `$ ` or "
            "wrap the command in a markdown fence. Attach outputs with files= "
            "(comma-separated paths under /home/maxwell)."
        )
        if self._full_host_access():
            return (
                "Run bash -lc in the maxwell-shell container (FULL ACCESS: host "
                "net, /host, root). Params: command (required), files (optional "
                "paths to attach). "
                f"{how} Container persists across calls. {limits_note}"
            )
        return (
            "Run bash -lc in the maxwell-shell sandbox (workdir /home/maxwell). "
            "Params: command (required), files (optional paths under /home/maxwell "
            "to attach to the channel). "
            f"{how} Container persists across calls. Max 10 MB per file. {limits_note}"
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
        except (asyncio.TimeoutError, asyncio.CancelledError):
            # 2026-07-21: also catch CancelledError. If the parent
            # task is cancelled (channel lock timeout, bot shutdown,
            # ,cancel command), proc.communicate() raises CancelledError
            # and the old `except TimeoutError` did not match — the
            # subprocess was left running, eventually filling the
            # stdout/stderr pipes and wedging the container.
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
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
                if not running and mode == desired_mode:
                    (_stdout, stderr), start_code = await self._run_docker(
                        "start", self.CONTAINER_NAME, timeout=15
                    )
                    if start_code == 0:
                        return
                # Wrong mode or start failed — require a successful rm, then recreate.
                (_stdout, stderr), rm_code = await self._run_docker(
                    "rm", "-f", self.CONTAINER_NAME, timeout=10
                )
                if running and mode != desired_mode and rm_code != 0:
                    raise RuntimeError(
                        "could not replace sandbox container with the desired isolation mode"
                    )
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

    @staticmethod
    def _command_arg(command: str | None = None, **kwargs) -> str | None:
        """Pick the command string out of native-tool args.

        Models frequently send `cmd` (and sometimes `script`/`code`) instead
        of `command`. Accept those aliases so a valid heredoc is not rejected
        as an empty command.
        """
        if command is not None and str(command).strip():
            return command
        for key in ("cmd", "script", "code"):
            val = kwargs.get(key)
            if val is not None and str(val).strip():
                return val
        return command

    def _normalize_command(self, command: str | None) -> str:
        raw = str(command or "").strip()
        if not raw:
            return ""
        raw = raw.replace("\r\n", "\n").replace("\r", "\n")

        # Models wrap the command in a markdown fence, or copy the `$ `
        # prompt from the channel echo of a previous shell call.
        fence = re.match(
            r"^```(?:bash|sh|shell|zsh|python|py)?[ \t]*\n(.*)\n```[ \t]*$",
            raw,
            re.DOTALL | re.IGNORECASE,
        )
        if fence:
            raw = fence.group(1).strip()
        if raw.startswith("$"):
            raw = re.sub(r"^\$[ \t]+", "", raw)

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
        # Multi-line commands & heredocs are allowed.
        if "\n" in command:
            hint = _unterminated_heredoc_error(command)
            if hint:
                return "heredoc error — " + hint
        non_heredoc = _strip_heredoc_blocks(command)
        if any(ord(c) < 32 and c not in ("\t", "\n", "\r") for c in non_heredoc):
            return "control characters are not allowed in shell commands"
        for pattern in _SHELL_BLOCKED_PATTERNS:
            if re.search(pattern, command, re.IGNORECASE):
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
        max_echo = 80
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

        If a previous update message or live progress message exists in the channel,
        edit that message instead of spamming new messages.

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
        max_chars = self._channel_max_chars()
        if max_chars and len(text) > max_chars:
            notice = "\n... (truncated for channel)"
            keep = max(0, max_chars - len(notice))
            text = text[:keep] + notice
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
        if len(chunks) > self._CHANNEL_MAX_CHUNKS:
            notice = "\n... (truncated for channel)"
            chunks = chunks[: self._CHANNEL_MAX_CHUNKS]
            last = chunks[-1]
            room = max(0, limit - len(notice))
            chunks[-1] = last[:room] + notice

        chan_key = str(getattr(getattr(message, "channel", None), "id", id(message)))
        for i, chunk in enumerate(chunks):
            safe = chunk.replace("```", "'''")
            formatted = f"```ansi\n{safe}\n```"

            target_msg = None
            if i == 0:
                # 1. Try reusing the live progress message if posted
                progress = self._get_channel_progress(message)
                if progress is not None:
                    posted = getattr(progress, "_posted", None)
                    if posted is not None and hasattr(posted, "edit"):
                        target_msg = posted
                # 2. Try reusing a previous shell message in this channel
                if target_msg is None and hasattr(self, "_last_shell_msg_by_channel"):
                    target_msg = self._last_shell_msg_by_channel.get(chan_key)

            edited = False
            if target_msg is not None:
                try:
                    await target_msg.edit(content=formatted)
                    edited = True
                    if not hasattr(self, "_last_shell_msg_by_channel"):
                        self._last_shell_msg_by_channel = {}
                    self._last_shell_msg_by_channel[chan_key] = target_msg
                except Exception:
                    edited = False

            if not edited:
                sent = await message.channel.send(formatted)
                if not hasattr(self, "_last_shell_msg_by_channel"):
                    self._last_shell_msg_by_channel = {}
                if sent is not None and hasattr(sent, "edit"):
                    self._last_shell_msg_by_channel[chan_key] = sent

            if len(chunks) > 1 and i < len(chunks) - 1:
                await asyncio.sleep(0.3)

    async def execute(
        self,
        message: Message,
        command: str | None = None,
        files: str | None = None,
        **kwargs,
    ) -> str:
        normalized = self._normalize_command(self._command_arg(command, **kwargs))
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


class SubAgentTool(Tool):
    """Delegate a coding task to a nested Maxwell that works it to completion.

    There is no external coding agent here — no `opencode` binary, no
    container image to build. The sub-agent is Maxwell: it runs on the same
    provider the bot already talks to, in its own scratch workdir, with a
    deliberately small toolset (run a command, read/write/list files,
    finish). That keeps the install requirement at "an AI model and a
    Discord token" instead of "an AI model, a Discord token, Docker, and a
    second agent runtime".

    The loop is: ask the model -> execute the tool calls it emits inside the
    workdir -> feed the results back -> repeat until it calls `finish`, or
    until the step/time budget runs out. The final report goes back to the
    main turn as the tool result.
    """

    # Writes files and runs commands: same trust class as `shell`.
    is_destructive = True

    _SYSTEM_PROMPT = (
        "You are Maxwell's sub-agent: a focused engineer working one task to "
        "completion, alone, with no user to ask.\n"
        "\n"
        "You work inside {workdir} — a scratch directory that is yours. Every "
        "command runs there; file paths are relative to it.\n"
        "\n"
        "Rules:\n"
        "- Work in small steps: inspect, change, run, check the output.\n"
        "- Actually verify. Run the code, the test, the linter — do not claim "
        "something works because it looks right.\n"
        "- Never ask questions. Pick the reasonable option and note the "
        "assumption in your final report.\n"
        "- Stay inside the workdir and keep commands short-lived. No "
        "interactive programs, no servers that never exit, no `sudo`.\n"
        "- When the task is done (or genuinely cannot be finished), call "
        "`finish` with a report: what you built, which files matter, what you "
        "verified, and anything left undone.\n"
        "\n"
        "You have {max_steps} steps. Spend them on the task, not on narration."
    )

    # The sub-agent's own tools. Small on purpose: a bigger surface makes
    # weaker models wander, and everything below is expressible as a command.
    _TOOLS = [
        {
            "type": "function",
            "function": {
                "name": "run_command",
                "description": (
                    "Run a bash command in the workdir and get stdout/stderr "
                    "plus the exit code back."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "Bash to run"}
                    },
                    "required": ["command"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "write_file",
                "description": (
                    "Create or overwrite a file in the workdir. Writes the "
                    "whole file — include the complete contents."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Path relative to the workdir",
                        },
                        "content": {
                            "type": "string",
                            "description": "Full file contents",
                        },
                    },
                    "required": ["path", "content"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a file from the workdir.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Path relative to the workdir",
                        }
                    },
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_files",
                "description": "List files in the workdir (recursive, capped).",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "finish",
                "description": (
                    "End the task and report back. Call this exactly once, "
                    "when the work is done or definitively blocked."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "report": {
                            "type": "string",
                            "description": (
                                "What you built, the files that matter, what "
                                "you verified, and anything left undone."
                            ),
                        }
                    },
                    "required": ["report"],
                },
            },
        },
    ]

    def get_description(self) -> str:
        return (
            "Hand a self-contained coding task to a sub-agent (another "
            "instance of you) that writes the code, runs it, fixes what "
            "breaks, and reports back with the result. Use it for work that "
            "needs several build-and-test rounds — a script, a small program, "
            "a data-crunching job — not for a one-liner you could just run "
            "with `shell`. It cannot ask questions, so state the task fully."
        )

    # ─── workspace helpers ────────────────────────────────────────────

    @staticmethod
    def _slugify(text: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")
        return (slug[:40] or "task").strip("-")

    def _workspace(self, task: str, workdir: str = "") -> Path:
        base = Path(Config.SUBAGENT_BASE_DIR)
        if not base.is_absolute():
            base = Path(__file__).resolve().parent / base
        name = self._slugify(workdir) if workdir else self._slugify(task)
        path = base / f"{name}-{uuid.uuid4().hex[:6]}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _resolve(self, workspace: Path, rel_path: str) -> Path:
        """Resolve a sub-agent path, refusing anything outside the workdir."""
        candidate = (workspace / str(rel_path or "").lstrip("/")).resolve()
        workspace = workspace.resolve()
        if candidate != workspace and workspace not in candidate.parents:
            raise ValueError(f"path escapes the workdir: {rel_path}")
        return candidate

    # ─── the sub-agent's own tools ────────────────────────────────────

    async def _run_command(self, workspace: Path, command: str) -> str:
        command = str(command or "").strip()
        if not command:
            return "error: empty command"
        try:
            proc = await asyncio.create_subprocess_exec(
                "bash",
                "-lc",
                command,
                cwd=str(workspace),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except Exception as e:
            return f"error: could not start command: {e}"
        try:
            out, _ = await asyncio.wait_for(
                proc.communicate(), timeout=Config.SUBAGENT_COMMAND_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
            return (
                f"error: command timed out after "
                f"{Config.SUBAGENT_COMMAND_TIMEOUT_SECONDS}s (it was killed)"
            )
        text = (out or b"").decode("utf-8", errors="replace")
        if len(text) > 12000:
            text = text[:12000] + "\n… (output truncated)"
        return f"exit={proc.returncode}\n{text or '(no output)'}"

    def _write_file(self, workspace: Path, path: str, content: str) -> str:
        try:
            target = self._resolve(workspace, path)
        except ValueError as e:
            return f"error: {e}"
        body = str(content or "")
        if len(body.encode("utf-8", errors="ignore")) > Config.SUBAGENT_MAX_FILE_BYTES:
            return (
                f"error: file exceeds SUBAGENT_MAX_FILE_BYTES "
                f"({Config.SUBAGENT_MAX_FILE_BYTES} bytes)"
            )
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")
        except OSError as e:
            return f"error: {e}"
        return f"wrote {target.relative_to(workspace.resolve())} ({len(body)} chars)"

    def _read_file(self, workspace: Path, path: str) -> str:
        try:
            target = self._resolve(workspace, path)
        except ValueError as e:
            return f"error: {e}"
        if not target.is_file():
            return f"error: no such file: {path}"
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return f"error: {e}"
        if len(text) > 12000:
            text = text[:12000] + "\n… (truncated)"
        return text or "(empty file)"

    def _list_files(self, workspace: Path) -> str:
        root = workspace.resolve()
        entries = []
        for item in sorted(root.rglob("*")):
            if item.is_dir():
                continue
            with contextlib.suppress(OSError):
                entries.append(f"{item.relative_to(root)} ({item.stat().st_size}b)")
            if len(entries) >= 200:
                entries.append("… (more files not listed)")
                break
        return "\n".join(entries) or "(workdir is empty)"

    async def _dispatch(self, workspace: Path, name: str, args: dict) -> str:
        if name == "run_command":
            return await self._run_command(workspace, args.get("command", ""))
        if name == "write_file":
            return self._write_file(
                workspace, args.get("path", ""), args.get("content", "")
            )
        if name == "read_file":
            return self._read_file(workspace, args.get("path", ""))
        if name == "list_files":
            return self._list_files(workspace)
        return f"error: unknown tool {name!r}"

    # ─── the loop ─────────────────────────────────────────────────────

    async def execute(self, message: Message, **kwargs) -> str:
        task = str(kwargs.get("task") or "").strip()
        if not task:
            return "sub_agent needs a `task` describing the work."

        provider = getattr(self.bot, "provider", None)
        if provider is None:
            return "sub_agent is unavailable: no LLM provider on this bot."

        max_steps = Config.SUBAGENT_MAX_STEPS
        try:
            requested = int(kwargs.get("max_steps") or 0)
        except (TypeError, ValueError):
            requested = 0
        if requested > 0:
            max_steps = min(requested, Config.SUBAGENT_MAX_STEPS)

        workspace = self._workspace(task, str(kwargs.get("workdir") or ""))
        deadline = time.monotonic() + Config.SUBAGENT_TIMEOUT_SECONDS
        model = Config.SUBAGENT_MODEL or None

        messages = [
            {
                "role": "system",
                "content": self._SYSTEM_PROMPT.format(
                    workdir=workspace, max_steps=max_steps
                ),
            },
            {"role": "user", "content": task},
        ]

        steps = 0
        commands_run = 0
        files_written: list[str] = []
        while steps < max_steps:
            if time.monotonic() > deadline:
                return self._report(
                    task,
                    workspace,
                    steps,
                    commands_run,
                    files_written,
                    f"stopped: hit the {Config.SUBAGENT_TIMEOUT_SECONDS}s time budget",
                )
            steps += 1
            try:
                reply = await provider.generate_chat_completion(
                    messages=messages,
                    tools=self._TOOLS,
                    model=model,
                    timeout=int(max(30, deadline - time.monotonic())),
                )
            except Exception as e:
                logger.warning("sub_agent provider call failed: %s", e)
                return self._report(
                    task,
                    workspace,
                    steps,
                    commands_run,
                    files_written,
                    f"stopped: the model call failed ({e})",
                )

            calls = normalize_native_tool_calls(reply.get("tool_calls"))
            content = str(reply.get("content") or "").strip()
            if not calls:
                # No tool call: the model is done talking, or it drifted into
                # prose. Either way its text is the best report we have.
                return self._report(
                    task,
                    workspace,
                    steps,
                    commands_run,
                    files_written,
                    content or "the sub-agent stopped without a report",
                )

            messages.append(
                {
                    "role": "assistant",
                    "content": reply.get("content") or "",
                    # Elided: a write_file call carries the whole file body,
                    # and the replayed transcript would otherwise pay for it
                    # again on every remaining step.
                    "tool_calls": elide_tool_calls_for_history(
                        reply.get("tool_calls") or []
                    ),
                }
            )

            for call in calls:
                name = call.get("name") or ""
                args = call.get("arguments") or {}
                if name == "finish":
                    return self._report(
                        task,
                        workspace,
                        steps,
                        commands_run,
                        files_written,
                        str(args.get("report") or content or "done"),
                    )
                if name == "run_command":
                    commands_run += 1
                result = await self._dispatch(workspace, name, args)
                if name == "write_file" and not result.startswith("error:"):
                    written = str(args.get("path") or "").strip()
                    if written and written not in files_written:
                        files_written.append(written)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id") or name,
                        "content": result,
                    }
                )
            # Bound the replayed transcript. Results are capped at 12k chars
            # each, but SUBAGENT_MAX_STEPS goes up to 200 — unbounded, the loop
            # walks off the end of the context window long before it can call
            # `finish`. The system prompt and the task stay pinned; only older
            # rounds are dropped, whole rounds at a time so no role=tool
            # message is ever orphaned from its assistant call.
            head, tail = messages[:2], messages[2:]
            messages = head + trim_tool_tail(tail)

        return self._report(
            task,
            workspace,
            steps,
            commands_run,
            files_written,
            f"stopped: used all {max_steps} steps without calling finish",
        )

    def _report(
        self,
        task: str,
        workspace: Path,
        steps: int,
        commands_run: int,
        files_written: list[str],
        outcome: str,
    ) -> str:
        lines = [
            f"sub-agent finished after {steps} step(s), {commands_run} command(s).",
            f"workdir: {workspace}",
        ]
        if files_written:
            lines.append(f"files written: {', '.join(files_written[:20])}")
        lines.append("")
        lines.append(outcome.strip())
        return "\n".join(lines)

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
    """Fetch YouTube transcripts, channel/playlist listings, and frames."""

    MAX_TRANSCRIPT_CHARS = 20000
    MAX_FRAMES = 6
    MAX_LIST_ITEMS = 50
    DEFAULT_LIST_ITEMS = 15
    YOUTUBE_HOST_RE = re.compile(
        r"(^|\.)((?:music\.)?youtube\.com|youtu\.be|youtube-nocookie\.com)$",
        re.I,
    )
    YOUTUBE_URL_RE = re.compile(
        r"https?://(?:www\.|m\.|music\.)?(?:youtube\.com|youtu\.be|youtube-nocookie\.com)/[^\s<>\"']+",
        re.I,
    )
    HANDLE_RE = re.compile(
        r"^@([A-Za-z0-9._-]{1,30})(?:/(videos|shorts|streams|live|playlists|featured|about))?/?$",
        re.I,
    )
    CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{20,24}$")
    TAB_RE = re.compile(
        r"/(videos|shorts|streams|live|playlists|featured|about|community)/?$",
        re.I,
    )
    CACHE_TTL = 10 * 60
    _result_cache: dict[str, tuple[float, str]] = {}

    def get_description(self):
        return (
            "YouTube helper: transcripts + optional timestamp frames for videos; "
            "recent-upload listings for channels and playlists; search by query. "
            "Prefer this over fetch_url for any youtube.com / youtu.be link. "
            "Params: url (video, channel, playlist, @handle, or /videos page), "
            "query (optional search if no url), limit (channel/playlist/search, "
            "default 15), timestamps (optional comma-separated seconds or mm:ss), "
            "max_transcript_chars (default 12000), lang (default en)."
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

    @staticmethod
    def _yt_dlp_binary() -> str:
        """Locate yt-dlp: PATH first, then this interpreter's own bin dir.

        `pip install yt-dlp` inside a venv puts the console script in that
        venv's bin/, which is NOT on PATH when the process was started by its
        absolute interpreter path (exactly what PM2 does). Checking sys.executable's
        directory means an install that has the package always finds the tool.
        Returns "" when it genuinely isn't installed.
        """
        found = shutil.which("yt-dlp")
        if found:
            return found
        sibling = Path(sys.executable).parent / "yt-dlp"
        if sibling.is_file() and os.access(sibling, os.X_OK):
            return str(sibling)
        return ""

    def _yt_dlp_args(self, *args: str) -> list[str]:
        cmd = [self._yt_dlp_binary() or "yt-dlp", "--no-update"]
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
        match = cls.YOUTUBE_URL_RE.search(text)
        if match:
            return match.group(0).rstrip(".,)]>")
        handle = cls.HANDLE_RE.search(text)
        if handle:
            tab = (handle.group(2) or "videos").lower()
            if tab in {"featured", "about", "community"}:
                tab = "videos"
            if tab == "live":
                tab = "streams"
            return f"https://www.youtube.com/@{handle.group(1)}/{tab}"
        if cls.CHANNEL_ID_RE.fullmatch(text):
            return f"https://www.youtube.com/channel/{text}/videos"
        return text

    @classmethod
    def _url_kind(cls, url: str) -> str:
        try:
            parsed = urlparse(url)
        except Exception:
            return "video"
        host = (parsed.hostname or "").lower()
        path = parsed.path or ""
        qs = parse_qs(parsed.query)
        if host.endswith("youtu.be"):
            return "video"
        if qs.get("v"):
            return "video"
        if re.search(r"/(?:embed|shorts|live)/[A-Za-z0-9_-]{6,}", path):
            return "video"
        if "/playlist" in path and qs.get("list"):
            return "playlist"
        if "/results" in path or qs.get("search_query"):
            return "search"
        if (
            "/@" in f"/{path.lstrip('/')}"
            or "/channel/" in path
            or path.startswith("/c/")
            or path.startswith("/user/")
        ):
            return "channel"
        return "video"

    @classmethod
    def _normalize_list_url(cls, url: str, kind: str) -> str:
        if kind != "channel":
            return url
        try:
            parsed = urlparse(url)
        except Exception:
            return url
        path = (parsed.path or "").rstrip("/")
        if cls.TAB_RE.search(path):
            path = cls.TAB_RE.sub(
                lambda m: "/streams"
                if m.group(1).lower() == "live"
                else (
                    "/videos"
                    if m.group(1).lower() in {"featured", "about", "community"}
                    else f"/{m.group(1).lower()}"
                ),
                path,
            )
        elif path:
            path = f"{path}/videos"
        else:
            return url
        return parsed._replace(path=path, query="", fragment="").geturl()

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
        if not self._yt_dlp_binary():
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
        if not self._yt_dlp_binary():
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
        if not timestamps or not shutil.which("ffmpeg") or not self._yt_dlp_binary():
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

    @classmethod
    def _cache_get(cls, key: str) -> str | None:
        item = cls._result_cache.get(key)
        if not item:
            return None
        expires, value = item
        if time.monotonic() >= expires:
            cls._result_cache.pop(key, None)
            return None
        return value

    @classmethod
    def _cache_set(cls, key: str, value: str) -> None:
        if len(cls._result_cache) > 64:
            cls._result_cache.clear()
        cls._result_cache[key] = (time.monotonic() + cls.CACHE_TTL, value)

    @classmethod
    def _parse_limit(cls, raw: Any) -> int:
        try:
            return max(1, min(int(raw), cls.MAX_LIST_ITEMS))
        except (TypeError, ValueError):
            return cls.DEFAULT_LIST_ITEMS

    @staticmethod
    def _entry_watch_url(entry: dict) -> str:
        eid = str(entry.get("id") or "").strip()
        webpage = str(entry.get("url") or entry.get("webpage_url") or "").strip()
        if webpage.startswith("http") and "youtube" in webpage:
            return webpage
        if eid.startswith(("PL", "UU", "FL", "RD", "OL")):
            return f"https://www.youtube.com/playlist?list={eid}"
        if eid:
            return f"https://www.youtube.com/watch?v={eid}"
        return webpage

    @classmethod
    def _format_catalog(cls, kind: str, data: dict, limit: int) -> str:
        title = str(
            data.get("title")
            or data.get("playlist_title")
            or data.get("channel")
            or data.get("uploader")
            or "YouTube"
        )
        channel = str(data.get("channel") or data.get("uploader") or "")
        channel_url = str(data.get("channel_url") or data.get("uploader_url") or "")
        webpage = str(data.get("webpage_url") or data.get("original_url") or "")
        description = str(data.get("description") or "").strip()
        if len(description) > 400:
            description = description[:400] + "…"
        entries = [
            e
            for e in (data.get("entries") or [])
            if isinstance(e, dict) and (e.get("id") or e.get("title"))
        ][:limit]
        parts = [
            f"Type: {kind}",
            f"Title: {title}",
        ]
        if channel:
            parts.append(f"Channel: {channel}")
        if channel_url:
            parts.append(f"Channel URL: {channel_url}")
        if webpage:
            parts.append(f"URL: {webpage}")
        count = data.get("playlist_count")
        if isinstance(count, int) and count > 0:
            parts.append(f"Total items: {count}")
        if description:
            parts.append(f"Description: {description}")
        if not entries:
            parts.append("No videos listed (empty, private, or blocked).")
            return "\n".join(parts)
        parts.append(f"Showing {len(entries)} item(s):")
        lines = []
        for i, entry in enumerate(entries, 1):
            etitle = str(entry.get("title") or entry.get("id") or "untitled")
            dur = entry.get("duration")
            dur_text = (
                f" ({cls._format_ts(float(dur))})"
                if isinstance(dur, (int, float)) and dur >= 0
                else ""
            )
            watch = cls._entry_watch_url(entry)
            extra = []
            views = entry.get("view_count")
            if isinstance(views, (int, float)):
                extra.append(f"{int(views)} views")
            uploaded = entry.get("upload_date") or entry.get("release_date")
            if isinstance(uploaded, str) and len(uploaded) == 8 and uploaded.isdigit():
                extra.append(f"{uploaded[:4]}-{uploaded[4:6]}-{uploaded[6:]}")
            meta = f" — {', '.join(extra)}" if extra else ""
            block = f"{i}. {etitle}{dur_text}{meta}"
            if watch:
                block += f"\n   {watch}"
            lines.append(block)
        parts.append("\n".join(lines))
        parts.append(
            "Call this tool again with a specific video URL to fetch a transcript or frames."
        )
        return "\n".join(parts)

    async def _dump_playlist(self, url: str, limit: int) -> dict:
        if not self._yt_dlp_binary():
            return {"error": "yt-dlp is not installed"}
        code, stdout, stderr = await self._run_cmd(
            self._yt_dlp_args(
                "--flat-playlist",
                "--dump-single-json",
                "--playlist-end",
                str(limit),
                "--no-warnings",
                url,
            ),
            timeout=75,
        )
        blob = (stdout or "").strip()
        if code != 0 or not blob:
            err = (stderr or stdout or "unknown error").strip()[:300]
            return {"error": err}
        try:
            data = json.loads(blob)
        except json.JSONDecodeError:
            return {"error": "yt-dlp returned invalid JSON"}
        return data if isinstance(data, dict) else {"error": "unexpected yt-dlp payload"}

    async def _list_catalog(self, url: str, kind: str, limit: int) -> str:
        cache_key = f"list:{kind}:{url}:{limit}"
        cached = self._cache_get(cache_key)
        if cached:
            return cached
        data = await self._dump_playlist(url, limit)
        if data.get("error") and not data.get("entries"):
            return (
                f"Error listing YouTube {kind}: {data['error']}\n"
                "If this is a 429 / bot check, wait and retry; cookies may need a refresh."
            )
        text = self._format_catalog(kind, data, limit)
        self._cache_set(cache_key, text)
        return text

    async def execute(
        self,
        message: Message,
        url: str | None = None,
        timestamps: str | None = None,
        max_transcript_chars: str = "12000",
        lang: str = "en",
        query: str | None = None,
        limit: Any = None,
        **kwargs,
    ) -> str:
        query = str(query or kwargs.get("q") or "").strip()
        list_limit = self._parse_limit(limit if limit is not None else kwargs.get("max_videos"))
        if query and not url:
            return await self._list_catalog(f"ytsearch{list_limit}:{query}", "search", list_limit)
        if not url:
            return "Error: url or query is required"
        url = self._extract_youtube_url(url)
        if url.startswith("ytsearch") or self._is_youtube_url(url):
            kind = (
                "search"
                if url.startswith("ytsearch")
                else self._url_kind(url)
            )
            if kind == "search" and not url.startswith("ytsearch"):
                q = parse_qs(urlparse(url).query).get("search_query", [""])[0].strip()
                if q:
                    return await self._list_catalog(
                        f"ytsearch{list_limit}:{q}", "search", list_limit
                    )
            if kind in {"channel", "playlist", "search"}:
                url = self._normalize_list_url(url, kind)
                return await self._list_catalog(url, kind, list_limit)
        else:
            return "Error: expected a YouTube URL or @handle"
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
        sent = None
        try:
            sent = await message.reply(file=file)
        except discord.Forbidden:
            return "Error: no permission to send files here"
        except discord.HTTPException as e:
            return f"Error sending media: {e}"

        # Attach the URL of what was actually sent (source URL + the new
        # Discord CDN URL) so the model can curl/pull/reuse either one.
        cdn_url = ""
        if sent is not None and getattr(sent, "attachments", None):
            cdn_url = sent.attachments[0].url
        result = f"__MEDIA_SENT__ Sent media: {filename}"
        result += f"\nSource URL: {url}"
        if cdn_url:
            result += f"\nFile URL: {cdn_url}"
        return result


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
            "Params: text (required string), language/lang (optional: english or spanish), "
            "voice (optional: tiktok or mommy — pick the TTS voice)."
        )

    async def execute(
        self,
        message: Message,
        text: str | None = None,
        language: str | None = None,
        lang: str | None = None,
        voice: str | None = None,
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
        fish_api_key = os.environ.get("FISH_API_KEY", "") or getattr(
            bot_config, "FISH_API_KEY", ""
        )
        token = uuid.uuid4().hex[:12]
        filename = f"tts_{token}.wav"
        voice_filename = f"tts_{token}.ogg"

        tts_source = None  # path to synthesized audio; drives fallback chain

        # Provider order: Fish (best quality, free tier, emotion tags) →
        # Riva (NVIDIA, paid) → gTTS (free fallback). Each block only sets
        # `tts_source` on success; failures fall through silently.
        if not tts_source and fish_api_key:
            fish_model = os.environ.get("TTS_FISH_MODEL", "s2.1-pro-free")
            fish_ref = _fish_reference_id(voice)
            fish_fmt = os.environ.get("TTS_FISH_FORMAT", "mp3")
            fish_out = await _synthesize_fish_tts(
                text,
                filename,
                api_key=fish_api_key,
                model=fish_model,
                reference_id=fish_ref,
                fmt=fish_fmt,
            )
            if fish_out:
                tts_source = fish_out
                logger.info(
                    "TTS provider: fish (model=%s, voice=%s)", fish_model, voice
                )

        if not tts_source:
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
                    # cast: the riva client returns an untyped stub object; the
                    # synthesized audio bytes live on `.audio` at runtime.
                    out_f.writeframesraw(cast(Any, resp).audio)
                tts_source = filename
                logger.info("TTS provider: riva")
            except Exception as e:
                logger.warning(f"Riva TTS synthesis failed: {e}")

        # Last-resort fallback: gTTS. Used when neither Fish nor Riva produced
        # audio. Kept at the bottom of the provider chain so the comment above
        # about quality (no voice selection / no emotion tags) still applies.
        if not tts_source:
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
                tts_source = filename
            except Exception as fallback_err:
                return f"Error: all TTS providers failed (last error: {fallback_err})"

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
        return "Disconnect from the active voice channel in this server."

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
        # Bare `contextlib.suppress(Exception)` statements are no-ops — they
        # only suppress when used as `with` blocks. M.close()/M.logout() can
        # raise IMAP4.error (server dropped the connection), and an exception
        # here would mask the real result or the real error above. Wrap them
        # properly so cleanup failures are swallowed instead of propagated.
        with contextlib.suppress(Exception):
            M.close()
        with contextlib.suppress(Exception):
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


def _imap_safe_seq(message_id: str) -> str | None:
    s = str(message_id or "").strip()
    return s if re.fullmatch(r"[0-9]+", s) else None


def _imap_safe_text_query(query: str) -> str | None:
    raw = str(query or "")
    if any(c in raw for c in '\r\n"\\'):
        return None
    s = raw.strip()
    if not s or len(s) > 200:
        return None
    return s


def _imap_get_message_sync(
    host: str, port: int, user: str, password: str, message_id: str, max_chars: int
) -> str:
    """Fetch one message and return its headers + body, capped at max_chars."""
    seq = _imap_safe_seq(message_id)
    if seq is None:
        return "Error: message_id must be a numeric IMAP sequence number"
    M = _imap_connect_sync(host, port, user, password)
    try:
        M.select("INBOX")
        typ, data = M.fetch(seq, "(RFC822)")
        if typ != "OK" or not data or not data[0]:
            return f"Error: IMAP fetch failed for message {message_id}"
        # Response shape varies by server: Dovecot collapses into a single
        # (bytes, bytes) tuple; older servers may return a bare bytes blob.
        # Handle both, mirroring _imap_list_recent_sync.
        raw = data[0]
        if isinstance(raw, tuple) and len(raw) >= 2:
            raw = raw[1]
        if isinstance(raw, bytes):
            raw_bytes = raw
        else:
            raw_bytes = str(raw).encode("utf-8", errors="replace")

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
        # See _imap_list_recent_sync — bare suppress() is a no-op; close/logout
        # can raise and would mask the real result/exception.
        with contextlib.suppress(Exception):
            M.close()
        with contextlib.suppress(Exception):
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
    safe = _imap_safe_text_query(query)
    if safe is None:
        return "Error: query contains invalid IMAP characters or is empty"
    M = _imap_connect_sync(host, port, user, password)
    try:
        M.select("INBOX")
        typ, data = M.search(None, f'TEXT "{safe}"')
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
        # See _imap_list_recent_sync — bare suppress() is a no-op; close/logout
        # can raise and would mask the real result/exception.
        with contextlib.suppress(Exception):
            M.close()
        with contextlib.suppress(Exception):
            M.logout()


class EmailSendTool(Tool):
    """Send mail FROM the local mailbox via local Postfix."""

    # Sending mail is the obvious prompt-injection target ("send my password
    # to attacker@evil") and on a tainted turn the user has to confirm.
    is_destructive: bool = True

    def get_description(self) -> str:
        return (
            "Send email from the bot mailbox via local Postfix. "
            "Params: to (required, comma-separated), subject, body, "
            "is_html (optional), reply_to, cc, bcc."
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

    is_destructive: bool = True

    def get_description(self) -> str:
        return (
            "List recent mailbox messages (id, from, subject, date). "
            "Use email_get_message for a body. Params: max_results (default 10), "
            "days_back (default 7), unread_only (optional)."
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
            result = await asyncio.to_thread(
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
        if self.bot is not None:
            self.bot.mark_message_tainted(message)
        return result


class EmailGetMessageTool(Tool):
    """Fetch the full body of a single local message by id."""

    is_destructive: bool = True

    def get_description(self) -> str:
        return (
            "Fetch one email by id (from email_read_inbox or email_search). "
            "Params: message_id, max_chars (default 8000)."
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
            result = await asyncio.to_thread(
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
        if self.bot is not None:
            self.bot.mark_message_tainted(message)
        return result


class EmailSearchTool(Tool):
    """Full-text search of the local mailbox."""

    is_destructive: bool = True

    def get_description(self) -> str:
        return (
            "Search the mailbox (IMAP TEXT). Params: query, max_results (default 10). "
            "Returns ids plus subject/from/date; use email_get_message for bodies."
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
            result = await asyncio.to_thread(
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
        if self.bot is not None:
            self.bot.mark_message_tainted(message)
        return result


# ---------------------------------------------------------------------------
# Self-modification tools. Both admin-gated. These let Maxwell (or any admin
# using the LLM) rewrite its own base personality + per-server prompts at
# runtime. The runtime load is hot — _load_control() reads mtime, so a write
# to bot_control.json is picked up on the next prompt assembly without a
# restart. server prompts are read on every prompt build, also hot.
# ---------------------------------------------------------------------------


class UpdateBasePersonalityTool(Tool):
    """Rewrite the global base_personality that ships in every prompt.

    This is what the model reads as its tone / do-don'ts / identity safety
    section. The MAXWELL_BASE_KNOWLEDGE block (identity, slang, voice, memes)
    is ALWAYS-ON and lives in code — it is NOT editable through this tool.
    This tool only rewrites the per-runtime personality paragraph that lives
    in bot_control.json under `base_personality`.

    Admin only — non-admins get a refused error.
    """

    is_destructive: bool = True

    def get_description(self) -> str:
        return (
            "Rewrite global base_personality (tone/do-don'ts in every prompt). "
            "Base Knowledge in code is not editable. Params: text (100-2000 chars). "
            "Admin-only."
        )

    async def execute(
        self,
        message: Message,
        text: str | None = None,
        **kwargs,
    ) -> str:
        if not self.bot or not self.bot._is_admin(message.author.id):
            return (
                "Error: update_base_personality is admin-only. The user who "
                "triggered this call is not in MAXWELL_OWNER_IDS."
            )
        if not text or not str(text).strip():
            return "Error: 'text' is required and cannot be empty."
        text = str(text).strip()
        # Soft cap: personality blocks over 4000 chars are usually a sign
        # someone pasted a whole essay. Reject and ask for a tighter version.
        if len(text) > 4000:
            return (
                f"Error: text is {len(text)} chars; the soft cap is 4000. "
                "Tighten the wording — a long personality is a context-killer, "
                "and the bot doesn't read past the most recent instructions anyway."
            )
        if len(text) < 20:
            return (
                f"Error: text is {len(text)} chars; too short to be a useful "
                "personality. Aim for at least 100-300 chars of voice/do-don'ts."
            )

        try:
            control = dict(self.bot._control)
            control["base_personality"] = text
            self.bot._control = control
            import asyncio
            from pathlib import Path

            await asyncio.to_thread(
                _atomic_json_write_sync,
                Path(self.bot.config.DATA_DIR) / "bot_control.json",
                control,
            )
        except Exception as e:
            return f"Error: failed to persist base_personality: {e}"
        return (
            f"base_personality updated. {len(text)} chars written to "
            "bot_control.json. The change is live on the next turn — no "
            "restart needed. MAXWELL_BASE_KNOWLEDGE (in code) was NOT "
            "touched; only the per-runtime personality paragraph was rewritten."
        )


class UpdateServerPromptTool(Tool):
    """Rewrite the per-server custom prompt (same as `,prompt <text>`).

    Same effect as the admin `,prompt <text>` command but invokable from
    inside an LLM turn — Maxwell can edit its own per-server instructions
    when it has a reason. Pass server_id (numeric snowflake) or pass 'DM'
    for the DM default. Pass empty text to clear the per-server prompt.

    Admin only.
    """

    is_destructive: bool = True

    def get_description(self) -> str:
        return (
            "Rewrite or clear the per-server custom prompt (same as `,prompt`). "
            "Params: server_id (snowflake or 'DM'), text (empty or '__CLEAR__' "
            "to clear). Admin-only."
        )

    async def execute(
        self,
        message: Message,
        server_id: str | None = None,
        text: str | None = None,
        **kwargs,
    ) -> str:
        if not self.bot or not self.bot._is_admin(message.author.id):
            return (
                "Error: update_server_prompt is admin-only. The user who "
                "triggered this call is not in MAXWELL_OWNER_IDS."
            )
        if not server_id or not str(server_id).strip():
            return "Error: 'server_id' is required (numeric snowflake or 'DM')."
        server_id = str(server_id).strip()
        # Soft cap mirrors the personality cap.
        if text is not None and len(str(text)) > 4000:
            return (
                f"Error: text is {len(text)} chars; the soft cap is 4000. "
                "Per-server prompts over 4000 chars are context-killers."
            )

        text_str = "" if text is None else str(text)
        cleared = text_str.strip() in ("", "__CLEAR__")

        try:
            if cleared:
                self.bot.memory.clear_server_prompt(server_id)
                return (
                    f"Cleared per-server prompt for server_id={server_id}. "
                    "The bot will fall back to base_personality + "
                    "MAXWELL_BASE_KNOWLEDGE in that server from now on."
                )
            self.bot.memory.set_server_prompt(server_id, text_str)
        except Exception as e:
            return f"Error: failed to persist server prompt: {e}"
        return (
            f"Server prompt updated for server_id={server_id}. "
            f"{len(text_str)} chars written. The change is live on the next "
            f"turn in that server — no restart needed."
        )
