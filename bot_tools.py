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
from urllib.parse import parse_qs, quote, urljoin, urlparse

import aiofiles
import aiohttp
import asyncio
import base64
import types
import uuid
import discord
from discord import Activity, File, Message, Status
from tools import Tool
import agent_events
from captcha_solver import CaptchaSolveError
from config import Config
from control_defaults import parse_bool
from tool_schemas import (
    elide_tool_calls_for_history,
    normalize_native_tool_calls,
    trim_tool_tail,
)
import site_backend
import site_server
from utils import (  # single source of truth, fd-safe
    FileLock,
    _atomic_json_write_sync,
    _safe_int,
    _spawn_background,
    is_direct_image_url,
    is_gif_page_url,
)

logger = logging.getLogger(__name__)

# Chess is optional: if python-chess is missing the chess_* tools will not be
# registered, but nothing else breaks. __CHESS_IMPORTED__ gates the tool classes.
try:  # noqa: E402
    import chess as _chess  # noqa: F401

    from chess_game import (
        ChessManager as _ChessManager,
        choose_bot_move as _chess_choose_bot_move,
        board_ascii as _chess_board_ascii,
        get_manager as _chess_get_manager,
        render_board_png as _chess_render_board_png,
    )

    __CHESS_IMPORTED__ = True
except Exception as _chess_err:  # pragma: no cover - missing optional dep
    __CHESS_IMPORTED__ = False
    _ChessManager = None
    _chess_choose_bot_move = None
    _chess_board_ascii = None
    _chess_get_manager = None
    _chess_render_board_png = None
    _chess = None
    logger.warning("chess tools disabled: python-chess not importable (%s)", _chess_err)

try:
    from ddgs import DDGS as _DDGS

    _DDGS_AVAILABLE = True
except ImportError:
    _DDGS = None
    _DDGS_AVAILABLE = False

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
    "espanol": "TTS_FISH_REFERENCE_ID_ESPANOL",
    "español": "TTS_FISH_REFERENCE_ID_ESPANOL",
    "spanish": "TTS_FISH_REFERENCE_ID_ESPANOL",
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


# Bash heredoc opener at an unquoted `<<`. Models almost always write the
# redirect on the same line as the delimiter (`cat << 'EOF' > file.py`); a
# here-string (`<<<`) is not a heredoc. Optional `<<-` (tab-stripped body)
# is accepted. Callers must only apply this at unquoted `<<` positions —
# a raw substring/regex search false-positives on `python3 -c "...<<Main"`.
_HEREDOC_OPENER_RE = re.compile(
    r"""
    <<(?!<)
    -?
    [ \t]*
    (?:
        '([A-Za-z_][A-Za-z0-9_-]*)'
      | "([A-Za-z_][A-Za-z0-9_-]*)"
      | \\?([A-Za-z_][A-Za-z0-9_-]*)
    )
    """,
    re.VERBOSE,
)


def _heredoc_token(match: re.Match) -> str:
    return match.group(1) or match.group(2) or match.group(3)


def _line_bounds(text: str, idx: int) -> tuple[int, int]:
    start = text.rfind("\n", 0, idx) + 1
    end = text.find("\n", idx)
    if end < 0:
        end = len(text)
    return start, end


def _heredoc_closer_span(
    command: str, body_start: int, delimiter: str
) -> tuple[bool, int | None, int]:
    """Return `(closed, closer_line_start, index_after_heredoc)`."""
    n = len(command)
    if body_start >= n:
        return False, None, n
    pos = body_start
    while pos <= n:
        nl = command.find("\n", pos)
        line_end = n if nl < 0 else nl
        if command[pos:line_end].strip() == delimiter:
            after = n if nl < 0 else nl + 1
            return True, pos, after
        if nl < 0:
            return False, None, n
        pos = nl + 1
    return False, None, n


def _scan_bash_heredocs(command: str) -> tuple[list[dict[str, Any]], bool]:
    """Find real bash heredocs, ignoring `<<` inside quotes or comments.

    Single quotes, double quotes, and `#` comments are not heredoc contexts.
    Here-strings (`<<<`) are skipped. Once an opener is accepted, its body is
    literal text (so `<<` inside the body does not open a nested heredoc).
    """
    text = str(command or "")
    n = len(text)
    i = 0
    in_single = False
    in_double = False
    blocks: list[dict[str, Any]] = []
    saw_unparsed = False

    while i < n:
        c = text[i]

        if in_single:
            if c == "'":
                in_single = False
            i += 1
            continue

        if in_double:
            if c == "\\" and i + 1 < n:
                i += 2
                continue
            if c == '"':
                in_double = False
            i += 1
            continue

        # Unquoted command text (including inside backticks / $()).
        if c == "\\" and i + 1 < n:
            i += 2
            continue
        if c == "'":
            in_single = True
            i += 1
            continue
        if c == '"':
            in_double = True
            i += 1
            continue
        if c == "#" and (i == 0 or text[i - 1] in " \t\n;&|(){}"):
            nl = text.find("\n", i)
            i = n if nl < 0 else nl
            continue
        if c == "<" and i + 1 < n and text[i + 1] == "<":
            if i + 2 < n and text[i + 2] == "<":
                i += 3
                continue
            match = _HEREDOC_OPENER_RE.match(text, i)
            line_start, line_end = _line_bounds(text, i)
            opener_text = text[line_start:line_end].strip()
            if not match:
                saw_unparsed = True
                i += 2
                continue
            delimiter = _heredoc_token(match)
            body_start = n if line_end >= n else line_end + 1
            closed, closer_start, after = _heredoc_closer_span(
                text, body_start, delimiter
            )
            blocks.append(
                {
                    "delimiter": delimiter,
                    "opener_text": opener_text,
                    "body_start": body_start,
                    "closer_start": closer_start,
                    "after": after,
                    "closed": closed,
                }
            )
            i = after
            continue
        i += 1

    return blocks, saw_unparsed


def _heredoc_delimiter(line: str) -> str | None:
    """Return the heredoc delimiter token if `line` opens a heredoc."""
    blocks, _ = _scan_bash_heredocs(line)
    if not blocks:
        return None
    return str(blocks[0]["delimiter"])


def _strip_heredoc_blocks(command: str) -> str:
    """Return `command` with heredoc bodies removed.

    A heredoc looks like `... << 'EOF'` (or `<< "EOF"` / `<<EOF` / `<<-EOF`)
    followed by lines of literal content ending with a line containing only
    the delimiter. Redirects and pipes after the delimiter on the opener line
    (`cat << 'EOF' > file`, `python3 - <<'PY' | tee out.py`) are part of the
    command, not the body. Stripping the body lets us validate the remaining
    (non-heredoc) parts as a single line.
    """
    text = str(command or "")
    blocks, _ = _scan_bash_heredocs(text)
    if not blocks:
        return text.rstrip("\n")
    out: list[str] = []
    prev = 0
    for block in blocks:
        body_start = int(block["body_start"])
        out.append(text[prev:body_start])
        if block["closed"]:
            closer_start = int(block["closer_start"])
            after = int(block["after"])
            out.append(text[closer_start:after])
            prev = after
        else:
            prev = len(text)
            break
    out.append(text[prev:])
    return "".join(out).rstrip("\n")


def _unterminated_heredoc_error(command: str) -> str | None:
    """Explain a newline violation caused by a malformed heredoc.

    Return a targeted hint when a heredoc was never closed so the caller
    is told exactly what to fix.
    """
    blocks, saw_unparsed_opener = _scan_bash_heredocs(command)
    for block in blocks:
        if not block["closed"]:
            opener = str(block["delimiter"])
            opener_text = str(block["opener_text"])
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


# Discord permission names that unlock server mod/admin tools. Basic send/view
# flags stay out so the model is not told it is a "mod" just for chatting.
_MOD_PERM_NAMES = (
    "administrator",
    "manage_guild",
    "manage_channels",
    "manage_roles",
    "manage_messages",
    "manage_nicknames",
    "manage_webhooks",
    "manage_expressions",
    "manage_emojis",
    "manage_emojis_and_stickers",
    "manage_events",
    "manage_threads",
    "kick_members",
    "ban_members",
    "moderate_members",
    "mute_members",
    "deafen_members",
    "move_members",
    "view_audit_log",
    "mention_everyone",
    "pin_messages",
    "create_instant_invite",
    "create_expressions",
)

_PERM_ALIASES = {
    "manage_emojis": "manage_expressions",
    "manage_emojis_and_stickers": "manage_expressions",
    "manage_expressions": "manage_expressions",
}

# Which tools a detected perm actually unlocks. administrator is handled as all.
_CAP_TOOLS: dict[str, tuple[str, ...]] = {
    "manage_channels": (
        "create_category",
        "create_channel",
        "edit_channel",
        "delete_channel",
        "lock_channel",
        "set_channel_permissions",
    ),
    "kick_members": ("kick_member",),
    "ban_members": ("ban_member", "unban_member", "list_bans"),
    "moderate_members": ("timeout_member",),
    "manage_roles": (
        "manage_role",
        "lock_channel",
        "set_channel_permissions",
    ),
    "manage_messages": ("purge_messages", "delete_message", "pin_message"),
    "pin_messages": ("pin_message",),
    "manage_nicknames": ("set_member_nickname",),
    "mute_members": ("voice_mod",),
    "deafen_members": ("voice_mod",),
    "move_members": ("voice_mod",),
    "manage_guild": ("edit_server",),
    "view_audit_log": ("audit_log",),
    "manage_expressions": ("manage_emoji",),
    "create_instant_invite": ("create_invite",),
}

_ALL_MOD_TOOLS = tuple(
    dict.fromkeys(name for names in _CAP_TOOLS.values() for name in names)
)
_SNOWFLAKE_RE = re.compile(r"(\d{15,22})")
_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd])?\s*$", re.I)


def _canon_perm(name: str) -> str:
    return _PERM_ALIASES.get(name, name)


def _admin_caps(guild, me=None) -> tuple[set[str], str]:
    me = me or _guild_me(guild)
    if not me:
        return set(), "bot member is not cached"
    perms = getattr(me, "guild_permissions", None)
    if not perms:
        return set(), "permissions are not cached"
    caps: set[str] = set()
    if getattr(perms, "administrator", False):
        caps.add("administrator")
        caps.update(_canon_perm(n) for n in _MOD_PERM_NAMES if n != "administrator")
        return caps, ""
    for name in _MOD_PERM_NAMES:
        if getattr(perms, name, False):
            caps.add(_canon_perm(name))
    return caps, ""


def _has_guild_cap(guild, cap: str) -> bool:
    caps, _reason = _admin_caps(guild)
    return "administrator" in caps or cap in caps or _canon_perm(cap) in caps


def _tools_for_caps(caps: set[str]) -> list[str]:
    if "administrator" in caps:
        return list(_ALL_MOD_TOOLS)
    found: list[str] = []
    seen: set[str] = set()
    for cap, names in _CAP_TOOLS.items():
        if cap not in caps:
            continue
        for name in names:
            if name not in seen:
                seen.add(name)
                found.append(name)
    return found


def _missing_cap(guild, cap: str) -> str:
    if _has_guild_cap(guild, cap):
        return ""
    name = getattr(guild, "name", "this server")
    return (
        f"Error: I do not have {cap}/admin in {name}. "
        "Run list_admin_servers to see roles, perms, and which tools I can use."
    )


def _mod_reason(message) -> str:
    return f"Maxwell admin tool requested by {getattr(message, 'author', '?')}"


def _parse_snowflake(value) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.isdigit():
        try:
            return int(text)
        except ValueError:
            return None
    match = _SNOWFLAKE_RE.search(text)
    if not match:
        return None
    return int(match.group(1))


def _parse_duration_seconds(value, default: int | None = None) -> int | None:
    text = str(value or "").strip().lower()
    if not text:
        return default
    if text in {"0", "off", "none", "clear", "remove", "stop", "undo"}:
        return 0
    match = _DURATION_RE.match(text)
    if not match:
        try:
            return max(0, int(float(text)))
        except (TypeError, ValueError):
            return None
    amount = float(match.group(1))
    unit = (match.group(2) or "s").lower()
    return int(amount * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit])


def _member_top_position(member) -> int:
    top = getattr(member, "top_role", None)
    if top is not None:
        return int(getattr(top, "position", 0) or 0)
    roles = list(getattr(member, "roles", None) or [])
    if not roles:
        return 0
    return max(int(getattr(role, "position", 0) or 0) for role in roles)


def _named_roles(me, guild) -> list:
    roles = list(getattr(me, "roles", None) or [])
    roles.sort(key=lambda role: int(getattr(role, "position", 0) or 0), reverse=True)
    everyone_id = getattr(guild, "id", None)
    named = []
    for role in roles:
        is_default = False
        checker = getattr(role, "is_default", None)
        if callable(checker):
            with contextlib.suppress(Exception):
                is_default = bool(checker())
        if is_default or getattr(role, "id", None) == everyone_id:
            continue
        named.append(role)
    return named


def _role_label(role) -> str:
    name = getattr(role, "name", None) or "role"
    rid = getattr(role, "id", "?")
    pos = getattr(role, "position", "?")
    return f"{name} ({rid}, pos {pos})"


def _guild_access_line(guild) -> str:
    """One prompt line: roles, elevated perms, and which mod tools can run."""
    if guild is None:
        return ""
    name = str(getattr(guild, "name", None) or "this server").strip() or "this server"
    gid = getattr(guild, "id", "?")
    me = _guild_me(guild)
    caps, reason = _admin_caps(guild, me)
    roles = _named_roles(me, guild) if me else []
    role_txt = ", ".join(getattr(r, "name", "role") for r in roles[:8]) or "@everyone"
    if reason and not caps:
        return (
            f"Your Discord access in {name} ({gid}): could not read "
            f"member/permissions ({reason})."
        )
    if "administrator" in caps:
        return (
            f"Your Discord access in {name} ({gid}): roles={role_txt} | "
            "perms=administrator | tools=all guild mod tools (channels, roles, "
            "kick, ban, timeout, purge, voice, emoji, server, audit log)"
        )
    if not caps:
        return (
            f"Your Discord access in {name} ({gid}): roles={role_txt} | "
            "perms=none | no kick/ban/channel/role tools here — "
            "list_admin_servers shows servers where you do."
        )
    tools = _tools_for_caps(caps)
    return (
        f"Your Discord access in {name} ({gid}): roles={role_txt} | "
        f"perms={', '.join(sorted(caps))} | "
        f"tools={', '.join(tools) if tools else 'none'}"
    )


def _guild_access_detail(guild) -> str:
    me = _guild_me(guild)
    caps, reason = _admin_caps(guild, me)
    name = getattr(guild, "name", "server")
    gid = getattr(guild, "id", "?")
    lines = [f"{name} (ID: {gid})"]
    if me is None:
        lines.append(f"  member: not cached ({reason or 'unknown'})")
        return "\n".join(lines)
    top = getattr(me, "top_role", None)
    lines.append(
        f"  top role: {_role_label(top) if top else 'none'} | "
        f"hierarchy pos { _member_top_position(me)}"
    )
    roles = _named_roles(me, guild)
    if roles:
        bits = []
        for role in roles[:12]:
            role_perms = getattr(role, "permissions", None)
            granted = []
            if role_perms is not None:
                if getattr(role_perms, "administrator", False):
                    granted = ["administrator"]
                else:
                    granted = [
                        _canon_perm(n)
                        for n in _MOD_PERM_NAMES
                        if n != "administrator" and getattr(role_perms, n, False)
                    ]
                    granted = list(dict.fromkeys(granted))
            extra = f" grants {', '.join(granted)}" if granted else " (cosmetic / no extra mod perms)"
            bits.append(f"{_role_label(role)}{extra}")
        lines.append("  roles: " + "; ".join(bits))
    else:
        lines.append("  roles: @everyone only")
    if reason and not caps:
        lines.append(f"  perms: none ({reason})")
    elif "administrator" in caps:
        lines.append("  perms: administrator (every guild mod tool)")
    else:
        lines.append(
            "  perms: " + (", ".join(sorted(caps)) if caps else "none")
        )
    tools = _tools_for_caps(caps)
    lines.append("  tools: " + (", ".join(tools) if tools else "none"))
    channels = list(getattr(guild, "channels", []) or [])
    cats = [ch for ch in channels if isinstance(ch, discord.CategoryChannel)]
    text = [ch for ch in channels if isinstance(ch, discord.TextChannel)]
    voice = [ch for ch in channels if isinstance(ch, discord.VoiceChannel)]
    lines.append(
        f"  channels: categories {len(cats)} text {len(text)} voice {len(voice)}"
    )
    return "\n".join(lines)


def _moderation_block(guild, me, target, *, action: str) -> str:
    if target is None or me is None:
        return "Error: member is unavailable"
    my_id = getattr(me, "id", None)
    their_id = getattr(target, "id", None)
    if their_id is not None and their_id == my_id and action in {
        "kick",
        "ban",
        "timeout",
        "voice",
        "nick",
    }:
        return f"Error: I cannot {action} myself"
    owner_id = getattr(guild, "owner_id", None) or getattr(
        getattr(guild, "owner", None), "id", None
    )
    if owner_id is not None and their_id == owner_id:
        return f"Error: I cannot {action} the server owner"
    if _member_top_position(target) >= _member_top_position(me):
        shown = getattr(target, "display_name", None) or their_id
        return (
            f"Error: {shown}'s top role is equal or higher than mine "
            f"(role hierarchy). I cannot {action} them."
        )
    return ""


def _find_role(guild, spec):
    if guild is None:
        return None, "Error: no server"
    rid = _parse_snowflake(spec)
    roles = list(getattr(guild, "roles", []) or [])
    getter = getattr(guild, "get_role", None)
    if rid is not None:
        role = None
        if callable(getter):
            role = getter(rid)
        if role is None:
            role = next((r for r in roles if getattr(r, "id", None) == rid), None)
        if role is None:
            return None, f"Error: role {spec} not found"
        return role, ""
    wanted = str(spec or "").strip().lstrip("@").lower()
    if not wanted:
        return None, "Error: role_id or role_name is required"
    matches = [
        r for r in roles if str(getattr(r, "name", "")).lower() == wanted
    ]
    if len(matches) == 1:
        return matches[0], ""
    if len(matches) > 1:
        return None, f"Error: multiple roles named '{spec}', use role_id"
    return None, f"Error: role '{spec}' not found"


async def _resolve_member(guild, spec):
    if guild is None:
        return None, "Error: no server"
    uid = _parse_snowflake(spec)
    getter = getattr(guild, "get_member", None)
    fetch = getattr(guild, "fetch_member", None)
    if uid is not None:
        member = getter(uid) if callable(getter) else None
        if member is None and callable(fetch):
            try:
                member = await fetch(uid)
            except discord.NotFound:
                return None, f"Error: user {uid} is not in {getattr(guild, 'name', 'this server')}"
            except discord.Forbidden:
                return None, f"Error: cannot fetch members in {getattr(guild, 'name', 'this server')}"
            except Exception as exc:
                return None, f"Error fetching member: {exc}"
        if member is None:
            return None, f"Error: user {uid} is not in {getattr(guild, 'name', 'this server')}"
        return member, ""
    wanted = str(spec or "").strip().lstrip("@").lower()
    if not wanted:
        return None, "Error: user_id is required"
    members = list(getattr(guild, "members", []) or [])
    matches = []
    for member in members:
        names = {
            str(getattr(member, "name", "") or "").lower(),
            str(getattr(member, "display_name", "") or "").lower(),
            str(getattr(member, "global_name", "") or "").lower(),
            str(getattr(member, "nick", "") or "").lower(),
        }
        names.discard("")
        if wanted in names:
            matches.append(member)
    if len(matches) == 1:
        return matches[0], ""
    if len(matches) > 1:
        return None, f"Error: multiple members match '{spec}', use user_id"
    return None, (
        f"Error: member '{spec}' not found in cache; use their numeric user id"
    )


def _permissions_from_names(raw) -> "discord.Permissions | None":
    text = str(raw or "").strip()
    if not text:
        return None
    perms = discord.Permissions.none()
    unknown = []
    for part in text.split(","):
        key = part.strip().lower().replace(" ", "_")
        if not key:
            continue
        key = _canon_perm(key)
        if not hasattr(perms, key):
            unknown.append(key)
            continue
        try:
            setattr(perms, key, True)
        except Exception:
            unknown.append(key)
    if unknown and not any(getattr(perms, n, False) for n, _v in perms):
        return None
    return perms


def _colour_from_text(raw):
    text = str(raw or "").strip().lstrip("#")
    if not text:
        return None
    try:
        return discord.Colour(int(text, 16))
    except (TypeError, ValueError):
        return None


def _channel_label(channel) -> str:
    name = getattr(channel, "name", None) or str(getattr(channel, "id", "unknown"))
    return f"#{name} ({getattr(channel, 'id', '?')})"


async def _get_guild_channel(bot, channel_id):
    cid = _parse_snowflake(channel_id)
    if cid is None:
        return None, f"Error: invalid channel_id: {channel_id}"
    channel = bot.get_channel(cid)
    if channel is None:
        try:
            channel = await bot.fetch_channel(cid)
        except Exception as exc:
            return None, f"Error finding channel: {exc}"
    if not getattr(channel, "guild", None):
        return None, "Error: channel is not in a server"
    return channel, ""


def _parse_overwrite_pairs(raw) -> dict:
    parsed: dict = {}
    for part in str(raw or "").split(","):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        name = key.strip().lower().replace(" ", "_")
        flag = value.strip().lower()
        if not name:
            continue
        if flag in {"true", "allow", "yes", "1", "on"}:
            parsed[name] = True
        elif flag in {"false", "deny", "no", "0", "off"}:
            parsed[name] = False
        elif flag in {"none", "inherit", "reset", "clear"}:
            parsed[name] = None
    return parsed


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
    """Fast image generation using Pollinations (SDXL-Lightning)."""

    def get_description(self):
        return (
            "Generate an AI image (~2-5s) — the DEFAULT image tool, text-to-image only. "
            "It CANNOT take an input image: to edit/modify/restyle an existing image, use hd_image. "
            "Params: prompt (required). Posts the image to chat with a CDN URL you can reuse in sites."
        )

    async def execute(
        self, message: Message, prompt: str | None = None, **kwargs
    ) -> str:
        if not prompt:
            return "Error: prompt parameter is required"
        # Pollinations is the primary generator — keyless, fast, always up.
        # The long-dead NVIDIA Flux route was dropped (it hung ~6 min per
        # request before timing out).
        return await self._pollinations_generate(message, prompt)

    async def _deliver_generated_image(
        self, message: Message, prompt: str, image_bytes: bytes, *, prefix: str
    ) -> str:
        local_path, perm_url = _persist_public_image(
            self.bot, image_bytes, prefix=prefix
        )
        file = File(BytesIO(image_bytes), filename="generated_image.png")
        sent_msg = None
        self._signal_streaming(message)
        try:
            sent_msg = await message.channel.send(file=file)
        except discord.Forbidden:
            logger.warning(
                f"Cannot send image in {message.channel.id} — missing permissions"
            )
            return "Error: Cannot send image — missing permissions"
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

    async def _pollinations_generate(self, message: Message, prompt: str) -> str:
        # Model comes solely from config — which reads POLLINATIONS_MODEL from
        # .env (config default applies only when unset). No hardcoded fallback
        # here so we never silently shift models across code edits.
        model = str(getattr(self.bot.config, "POLLINATIONS_MODEL", "") or "").strip()
        seed = random.randint(0, 999999)
        url = (
            "https://image.pollinations.ai/prompt/"
            f"{quote(prompt[:1500], safe='')}"
            f"?width=1024&height=1024&nologo=true&model={quote(model, safe='')}"
            f"&seed={seed}"
        )
        session = await _get_shared_session()
        try:
            async with session.get(
                url,
                headers={"User-Agent": _IMAGE_FETCH_UA, "Accept": "image/*"},
                timeout=aiohttp.ClientTimeout(total=90),
                allow_redirects=True,
            ) as response:
                if response.status != 200:
                    body = await response.text()
                    logger.error(
                        "Pollinations image error: %s - %s",
                        response.status,
                        body[:300],
                    )
                    return (
                        f"Error generating image: Pollinations returned {response.status}."
                    )
                ctype = (
                    (response.headers.get("Content-Type") or "")
                    .split(";")[0]
                    .strip()
                    .lower()
                )
                raw = await _read_response_limited(response, 12 * 1024 * 1024)
        except asyncio.TimeoutError:
            return "Error: Pollinations image generation timed out."
        except Exception as e:
            logger.warning("Pollinations image error: %s", e)
            return f"Error generating image: {e}"
        looks_like_image = bool(
            raw
            and (
                raw.startswith(b"\x89PNG")
                or raw.startswith(b"\xff\xd8\xff")
                or raw.startswith(b"GIF8")
                or raw.startswith(b"RIFF")
                or ctype.startswith("image/")
            )
        )
        if not looks_like_image:
            logger.error(
                "Pollinations returned non-image payload (%s, %s bytes)",
                ctype,
                len(raw or b""),
            )
            return "Error: Pollinations did not return an image."
        logger.info(
            "Pollinations image generated successfully, size: %s bytes", len(raw)
        )
        return await self._deliver_generated_image(
            message, prompt, raw, prefix="pollinations"
        )


def _sniff_image_mime(raw: bytes) -> str:
    """Best-effort image MIME from magic bytes, defaulting to PNG."""
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw.startswith(b"GIF8"):
        return "image/gif"
    if raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


# Some image hosts reject a default aiohttp User-Agent with a 403.
_IMAGE_FETCH_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0.0.0 Safari/537.36"
)


class HDImageGeneratorTool(Tool):
    """HD image generation and editing via the Gemini image model.

    Talks to the OpenAI-compatible chat endpoint rather than
    /images/generations: only the chat route accepts an input image, so
    generate and edit are the same call with or without an `image` part.
    """

    # Discord's own limit is 25MB; inputs get downscaled well below it.
    MAX_INPUT_BYTES = 20 * 1024 * 1024
    # An empty response is usually a silent safety refusal, which repeats
    # deterministically (6/6 in testing on a photo of a real person). Retry
    # once for a genuinely flaky gateway, then stop burning ~20s a try.
    MAX_ATTEMPTS = 2
    _DATA_URI_RE = re.compile(
        r"data:image/(?P<ext>[A-Za-z0-9.+-]+);base64,(?P<b64>[A-Za-z0-9+/=]+)"
    )
    _IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")

    def get_description(self):
        return (
            "Generate OR edit an HD AI image with Gemini (~10-30s). Use for high quality/HD/HQ "
            "requests, and for ANY edit of an existing image ('make the car red', 'add a hat', "
            "'remove the background', 'combine these'). "
            "Params: prompt (required — for an edit, describe the change, not the whole scene); "
            "image (optional — an http(s) URL, a local path, or a list of up to 4 of them, to edit "
            "or use as reference). If image is omitted and the user attached images to the message, "
            "those are used automatically. Returns a Discord CDN URL plus a permanent URL for sites."
        )

    def _endpoint(self) -> tuple[str, str, str]:
        """(chat_completions_url, api_key, model), inheriting the primary endpoint."""
        cfg = self.bot.config
        base = (
            getattr(cfg, "GEMINI_IMAGE_BASE_URL", "")
            or getattr(cfg, "OLLAMA_BASE_URL", "")
            or ""
        ).rstrip("/")
        key = getattr(cfg, "GEMINI_IMAGE_API_KEY", "") or getattr(
            cfg, "OLLAMA_API_KEY", ""
        )
        model = getattr(cfg, "GEMINI_IMAGE_MODEL", "") or "gemini-3.1-flash-image"
        url = base if base.endswith("/chat/completions") else f"{base}/chat/completions"
        return url, key, model

    def _shrink(self, raw: bytes) -> tuple[bytes, str]:
        """Downscale an input image so the upload does not dominate latency.

        Best effort: without Pillow the original bytes go up untouched, which
        still works, just slower.
        """
        max_edge = int(getattr(self.bot.config, "GEMINI_IMAGE_MAX_INPUT_EDGE", 1024))
        try:
            from PIL import Image as _PILImage

            im = _PILImage.open(BytesIO(raw))
            im = im.convert("RGB")
            if max(im.size) > max_edge:
                ratio = max_edge / float(max(im.size))
                im = im.resize(
                    (max(1, int(im.width * ratio)), max(1, int(im.height * ratio))),
                    _PILImage.LANCZOS,
                )
            buf = BytesIO()
            im.save(buf, format="JPEG", quality=88)
            return buf.getvalue(), "image/jpeg"
        except Exception as e:
            # No Pillow (or an image it cannot open): send the bytes through
            # untouched, but label them from their magic number rather than
            # guessing — a JPEG announced as image/png gets rejected upstream.
            logger.debug(f"hd_image input downscale skipped: {e}")
            return raw, _sniff_image_mime(raw)

    async def _load_one(self, ref: str) -> tuple[bytes | None, str]:
        """Resolve a single image reference to bytes. Returns (bytes, error)."""
        ref = str(ref).strip()
        if not ref:
            return None, "empty image reference"

        # Already-inlined data URI — accept it as-is.
        m = self._DATA_URI_RE.search(ref)
        if m:
            try:
                return base64.b64decode(m.group("b64")), ""
            except Exception:
                return None, "malformed data: URI"

        if ref.startswith(("http://", "https://")):
            if not _is_safe_url(ref):
                return None, f"refusing to fetch private/internal URL {ref[:80]}"
            try:
                session = await _get_shared_session()
                # A redirect is not re-checked by _is_safe_url, so a public URL
                # could otherwise bounce us to link-local metadata. Refuse to
                # follow, same as SendMediaTool. Many image hosts (Wikimedia,
                # Reddit) also 403 a default aiohttp UA, hence the browser one.
                async with session.get(
                    ref,
                    timeout=aiohttp.ClientTimeout(total=60),
                    allow_redirects=False,
                    headers={
                        "User-Agent": _IMAGE_FETCH_UA,
                        "Accept": "image/*,*/*;q=0.8",
                    },
                ) as resp:
                    if resp.status in (301, 302, 303, 307, 308):
                        return None, (
                            f"{ref[:80]} redirects; pass the direct image URL"
                        )
                    if resp.status != 200:
                        return None, f"HTTP {resp.status} fetching {ref[:80]}"
                    return (
                        await _read_response_limited(resp, self.MAX_INPUT_BYTES)
                    ), ""
            except asyncio.TimeoutError:
                return None, f"timed out fetching {ref[:80]}"
            except Exception as e:
                return None, f"could not fetch {ref[:80]}: {e}"

        # Local path — only from the dirs Maxwell itself writes images to.
        try:
            img_dir, _ = _public_image_target(self.bot)
            allowed = [os.path.abspath(img_dir), os.path.abspath("temp")]
            path = os.path.abspath(ref)
            if not any(
                path == root or path.startswith(root + os.sep) for root in allowed
            ):
                return None, f"local path outside the allowed image dirs: {ref[:80]}"
            if not os.path.isfile(path):
                return None, f"no such file: {ref[:80]}"
            if os.path.getsize(path) > self.MAX_INPUT_BYTES:
                return None, f"file too large: {ref[:80]}"
            with open(path, "rb") as f:
                return f.read(), ""
        except Exception as e:
            return None, f"could not read {ref[:80]}: {e}"

    def _attached_images(self, message: Message) -> list[str]:
        """Image URLs attached to the triggering message."""
        urls = []
        for att in getattr(message, "attachments", None) or []:
            ctype = (getattr(att, "content_type", "") or "").lower()
            name = (getattr(att, "filename", "") or "").lower()
            if ctype.startswith("image/") or name.endswith(self._IMAGE_EXTS):
                url = getattr(att, "url", None)
                if url:
                    urls.append(url)
        return urls

    async def execute(
        self,
        message: Message,
        prompt: str | None = None,
        image: str | list | None = None,
        **kwargs,
    ) -> str:
        if not prompt:
            return "Error: prompt parameter is required"

        api_url, api_key, model = self._endpoint()
        if not api_url or api_url == "/chat/completions":
            return "Error: HD image generation is not configured (no GEMINI_IMAGE_BASE_URL or OLLAMA_BASE_URL)"

        # Normalize the image param: a single ref, a list, or a
        # comma/newline-separated string all mean the same thing.
        refs: list[str] = []
        if isinstance(image, (list, tuple)):
            refs = [str(x) for x in image if str(x).strip()]
        elif isinstance(image, str) and image.strip():
            text = image.strip()
            if self._DATA_URI_RE.search(text):
                refs = [text]
            elif text.startswith("["):
                # The schema advertises a JSON list for multiple images.
                try:
                    parsed = json.loads(text)
                    refs = [str(x).strip() for x in parsed if str(x).strip()]
                except Exception:
                    refs = [text]
            else:
                # Split on newlines, and on a comma only where the next ref
                # plainly begins — a bare comma split would corrupt any single
                # URL that carries one in its query string.
                refs = [
                    part.strip()
                    for line in re.split(r"\n+", text)
                    for part in re.split(r",\s*(?=https?://|/)", line)
                    if part.strip()
                ]
        if not refs:
            refs = self._attached_images(message)
        refs = refs[:4]  # keep the payload (and the latency) sane

        parts: list[dict] = [{"type": "text", "text": prompt}]
        loaded = 0
        for ref in refs:
            raw, err = await self._load_one(ref)
            if raw is None:
                logger.warning(f"hd_image input rejected: {err}")
                return f"Error: {err}"
            shrunk, mime = self._shrink(raw)
            parts.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{mime};base64,{base64.b64encode(shrunk).decode()}"
                    },
                }
            )
            loaded += 1

        payload = {"model": model, "messages": [{"role": "user", "content": parts}]}
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        timeout_s = int(getattr(self.bot.config, "GEMINI_IMAGE_TIMEOUT", 300))
        session = await _get_shared_session()

        # This model can spend its whole turn reasoning and return
        # finish_reason=stop with empty content — no refusal text, no image.
        # Editing a photo of a real person reproduces it every time; that is
        # a safety refusal the gateway does not label. Retry once anyway, in
        # case the gateway itself hiccuped.
        found: list[tuple[str, str]] = []
        said = ""
        last_error = ""
        for attempt in range(self.MAX_ATTEMPTS):
            try:
                async with session.post(
                    api_url,
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=timeout_s),
                ) as response:
                    body = await response.text()
                    if response.status != 200:
                        logger.error(
                            f"HD image API error: {response.status} - {body[:500]}"
                        )
                        if "quota" in body.lower():
                            return (
                                f"Error: the HD image model ({model}) has no quota "
                                "right now. Use image_generator instead."
                            )
                        last_error = (
                            "Error generating HD image: API returned status "
                            f"{response.status}"
                        )
                        if 500 <= response.status < 600:
                            continue
                        return last_error
                    try:
                        data = json.loads(body)
                    except Exception:
                        logger.error(f"HD image non-JSON response: {body[:300]}")
                        return "Error: HD image endpoint returned a non-JSON response"
            except asyncio.TimeoutError:
                logger.warning(
                    f"HD image timed out after {timeout_s}s "
                    f"(attempt {attempt + 1}/{self.MAX_ATTEMPTS})"
                )
                last_error = f"Error: HD image generation timed out after {timeout_s}s"
                continue
            except Exception as e:
                logger.error(f"HD image generation request error: {e}")
                return f"Error generating HD image: {e}"

            choices = data.get("choices") or []
            if not choices:
                logger.error(f"HD image response has no choices: {list(data.keys())}")
                last_error = "Error: No image data in HD response"
                continue
            msg = choices[0].get("message") or {}
            content = msg.get("content")
            if isinstance(content, list):
                # Some gateways hand back structured parts, not a string.
                content = " ".join(
                    p.get("text", "") if isinstance(p, dict) else str(p)
                    for p in content
                )
            content = content or ""

            found = self._DATA_URI_RE.findall(content)
            if found:
                break

            said = re.sub(r"\s+", " ", str(content)).strip()
            logger.warning(
                f"HD image attempt {attempt + 1}/{self.MAX_ATTEMPTS} returned no "
                f"image. Text: {said[:200]!r}"
            )
            if said:
                # Actual words back means a refusal or a misread instruction,
                # not the empty-response glitch — retrying just repeats it.
                return (
                    "Error: the HD image model returned text, not an image: "
                    f"{said[:300]}"
                )

        if not found:
            if last_error:
                return last_error
            if loaded:
                return (
                    "Error: the HD image model returned no image. It silently "
                    "refuses to edit photos of real people — say so if that is "
                    "what was asked. Otherwise reword the edit, or use "
                    "image_generator to make a fresh image."
                )
            return (
                "Error: the HD image model returned no image. Reword the prompt, "
                "or use image_generator instead."
            )

        ext, b64 = found[0]
        try:
            image_bytes = base64.b64decode(b64)
        except Exception as e:
            logger.error(f"HD image base64 decode failed: {e}")
            return "Error: HD image data was not decodable"

        ext = "jpg" if ext.lower() in ("jpeg", "jpg") else "png"
        file = File(BytesIO(image_bytes), filename=f"hd_generated_image.{ext}")
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
        local_path, perm_url = _persist_public_image(
            self.bot, image_bytes, ext=f".{ext}", prefix="hd"
        )

        verb = "Edited" if loaded else "Generated"
        await self.bot.memory.add_to_channel_memory(
            str(message.channel.id),
            {
                "author": "Tool",
                "content": f"{verb} HD image: {prompt[:200]}",
                "is_tool": True,
            },
        )
        result = f"HD image {verb.lower()} successfully: {prompt[:100]}"
        if loaded:
            result += f" (from {loaded} input image{'s' if loaded > 1 else ''})"
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
    """Delete a message. Own messages always; others need manage_messages."""

    def get_description(self):
        return (
            "Delete a message. Your own messages always. Someone else's needs "
            "manage_messages. Params: message_id (required), channel_id (optional, "
            "defaults to the current channel)."
        )

    async def execute(
        self,
        message: Message,
        message_id: str | None = None,
        channel_id: str | None = None,
        **kwargs,
    ) -> str:
        if not message_id:
            return "Error: message_id is required"
        channel = getattr(message, "channel", None)
        if channel_id:
            channel, error = await _get_guild_channel(self.bot, channel_id)
            if error:
                return error
        if channel is None or not hasattr(channel, "fetch_message"):
            return "Error: channel is unavailable"
        try:
            msg = await channel.fetch_message(int(str(message_id).strip()))
            mine = self.bot.user and msg.author.id == self.bot.user.id
            guild = getattr(channel, "guild", None)
            if not mine:
                if guild is None:
                    return "Error: I can only delete my own messages here"
                missing = _missing_cap(guild, "manage_messages")
                if missing:
                    return missing
            await msg.delete()
            who = "my" if mine else "that"
            return f"Deleted {who} message {message_id}"
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
    LLM turns — the triggering channel gets a 'max is sleeping,
    back in Xm' notice (deduped per user, never a DM). The 2026-07-19
    user directive: the bot kept spamming goodnight/goodbye in chat;
    a real sleep window is the structural fix. Use this when the
    conversation is genuinely winding down — not as a generic
    goodbye."""

    is_destructive: bool = False
    streams_output: bool = False

    def get_description(self):
        return (
            "Sleep 1-60 minutes (default 30). While asleep, LLM turns are skipped "
            "and the triggering channel gets one 'max is sleeping' notice. Use only "
            "at a real end-of-conversation, not as a goodbye. Calling again resets "
            "the window. Params: duration_minutes."
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
        result = self.bot.set_sleep(n)
        if asyncio.iscoroutine(result):
            return await result
        return result


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
        result = self.bot.clear_sleep()
        if asyncio.iscoroutine(result):
            return await result
        return result


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
            "to chunk a normal reply. If someone is typing, wait for them to send. "
            "Distinct from sleep (minutes, ends dispatch)."
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


def _find_guild(guilds: list, target: str) -> tuple[Any, str]:
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
            guild, err = _find_guild(list(self.bot.guilds or []), target)
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
        target = (server or "").strip()
        if not target:
            return "Error: leave_server requires a server name or ID"

        guild, err = _find_guild(list(self.bot.guilds or []), target)
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
        return (
            "Look up a Discord user by ID or mention. Params: user_id "
            "(required, numeric ID or @mention). Returns name, creation date, "
            "avatar, and whether they are in a voice channel."
        )

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
            member, voice_ch = _find_member_voice(
                self.bot, int(cleaned), getattr(message, "guild", None)
            )
            if voice_ch is not None:
                gname = getattr(getattr(voice_ch, "guild", None), "name", "?")
                info += (
                    f"\nVoice: in #{getattr(voice_ch, 'name', voice_ch.id)} "
                    f"({gname})"
                )
            else:
                info += "\nVoice: not in a voice channel (from cached members)"
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
        chan = getattr(message, "channel", None)
        if not message.guild and not chan:
            return "Error: Channel context unavailable"
        try:
            search_limit = max(1, min(int(limit), 25))
            results = []
            clean_query = str(query or "").strip().lower()

            # If query is empty or blank, fetch recent channel history
            if not clean_query:
                if chan and hasattr(chan, "history"):
                    async for msg in chan.history(limit=search_limit):
                        snippet = msg.content[:150] + ("..." if len(msg.content) > 150 else "")
                        results.append(f"[{msg.id}] {msg.author.display_name}: {snippet}")
                    if not results:
                        return "No recent messages found in this channel"
                    return f"Recent messages ({len(results)}):\n" + "\n".join(results)
                return "Error: query is required"

            # 1. First search recent history in current channel (bots can always read accessible channel history)
            if chan and hasattr(chan, "history"):
                try:
                    async for msg in chan.history(limit=100):
                        if clean_query in (msg.content or "").lower():
                            snippet = msg.content[:150] + ("..." if len(msg.content) > 150 else "")
                            results.append(f"[#{getattr(chan, 'name', 'chat')} - {msg.id}] {msg.author.display_name}: {snippet}")
                            if len(results) >= search_limit:
                                break
                except Exception:
                    pass

            # 2. If not enough results and in a guild, search across other accessible text channels
            if len(results) < search_limit and getattr(message, "guild", None):
                guild = message.guild
                channels_to_check = [
                    c for c in getattr(guild, "text_channels", [])
                    if c.id != getattr(chan, "id", None) and c.permissions_for(guild.me).read_messages
                ][:8]
                for c in channels_to_check:
                    if len(results) >= search_limit:
                        break
                    try:
                        async for msg in c.history(limit=50):
                            if clean_query in (msg.content or "").lower():
                                snippet = msg.content[:150] + ("..." if len(msg.content) > 150 else "")
                                results.append(f"[#{c.name} - {msg.id}] {msg.author.display_name}: {snippet}")
                                if len(results) >= search_limit:
                                    break
                    except Exception:
                        continue

            if not results:
                return f"No messages found matching '{query}'"
            return "Search results:\n" + "\n".join(results)
        except Exception as e:
            return f"Error searching messages: {e}"


class SetNicknameTool(Tool):
    """Change the bot's own nickname in the server"""

    def get_description(self):
        return (
            "Change your nickname in this server (that becomes your name here). "
            "Params: nickname (required, 'reset' to remove)."
        )

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
                return (
                    f"Nickname changed to '{nickname}'. "
                    f"Your name in this server is now {nickname}."
                )
            return (
                "Nickname removed. Your name in this server is your account name again."
            )
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
            "Inspect Discord roles/permissions. Shows which of YOUR roles grant "
            "mod/admin perms and which tools those unlock. No args: current "
            "server first, then every server where you have elevated perms. "
            "Params: guild_id (optional, one server in detail)."
        )

    async def execute(
        self, message: Message, guild_id: str | None = None, **kwargs
    ) -> str:
        current = getattr(message, "guild", None)
        wanted = _parse_snowflake(guild_id)
        if wanted is not None:
            guild = self.bot.get_guild(wanted)
            if guild is None:
                return f"Error: I am not in server {guild_id} or it is not cached"
            return _guild_access_detail(guild)

        blocks = []
        if current is not None:
            blocks.append("This server:\n" + _guild_access_detail(current))
        others = []
        for guild in getattr(self.bot, "guilds", []) or []:
            if current is not None and getattr(guild, "id", None) == getattr(
                current, "id", None
            ):
                continue
            caps, _reason = _admin_caps(guild)
            if not caps:
                continue
            others.append(_guild_access_detail(guild))
        if others:
            blocks.append(
                "Other servers with elevated perms:\n" + "\n\n".join(others[:20])
            )
        if not blocks:
            return (
                "No cached Discord member/permissions. I cannot see roles in "
                "any joined server right now."
            )
        if current is None and not others:
            return (
                "No servers with cached manage_channels/mod permissions. "
                "Don't try kick/ban/channel/role tools until this lists a target."
            )
        return "\n\n".join(blocks)


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


def _role_blocked(me, role) -> str:
    if int(getattr(role, "position", 0) or 0) >= _member_top_position(me):
        return (
            f"Error: role {getattr(role, 'name', role)} is equal/higher than "
            "my top role (hierarchy)"
        )
    return ""


class KickMemberTool(Tool):
    def get_description(self):
        return (
            "Kick a member from a server. Requires kick_members. "
            "Params: user_id (required), reason (optional), guild_id (optional)."
        )

    async def execute(
        self,
        message: Message,
        user_id: str | None = None,
        reason: str | None = None,
        guild_id: str | None = None,
        **kwargs,
    ) -> str:
        guild, error = await _resolve_guild(self.bot, message, guild_id)
        if error:
            return error
        missing = _missing_cap(guild, "kick_members")
        if missing:
            return missing
        member, error = await _resolve_member(guild, user_id)
        if error:
            return error
        blocked = _moderation_block(guild, _guild_me(guild), member, action="kick")
        if blocked:
            return blocked
        try:
            await member.kick(reason=_mod_reason(message) if not reason else str(reason)[:512])
            return f"Kicked {member} ({member.id}) from {guild.name}"
        except discord.Forbidden:
            return f"Error: Discord denied kicking {member}; hierarchy or missing kick_members"
        except Exception as e:
            return f"Error kicking member: {e}"


class BanMemberTool(Tool):
    def get_description(self):
        return (
            "Ban a member. Requires ban_members. Params: user_id (required), "
            "reason (optional), delete_message_seconds (optional 0-604800), "
            "guild_id (optional)."
        )

    async def execute(
        self,
        message: Message,
        user_id: str | None = None,
        reason: str | None = None,
        delete_message_seconds: str = "0",
        guild_id: str | None = None,
        **kwargs,
    ) -> str:
        guild, error = await _resolve_guild(self.bot, message, guild_id)
        if error:
            return error
        missing = _missing_cap(guild, "ban_members")
        if missing:
            return missing
        member, error = await _resolve_member(guild, user_id)
        if error:
            return error
        blocked = _moderation_block(guild, _guild_me(guild), member, action="ban")
        if blocked:
            return blocked
        try:
            seconds = max(0, min(int(_parse_duration_seconds(delete_message_seconds, 0) or 0), 604800))
        except (TypeError, ValueError):
            seconds = 0
        try:
            await guild.ban(
                member,
                reason=_mod_reason(message) if not reason else str(reason)[:512],
                delete_message_seconds=seconds,
            )
            return f"Banned {member} ({member.id}) from {guild.name}"
        except TypeError:
            try:
                await guild.ban(
                    member,
                    reason=_mod_reason(message) if not reason else str(reason)[:512],
                    delete_message_days=min(7, seconds // 86400),
                )
                return f"Banned {member} ({member.id}) from {guild.name}"
            except Exception as e:
                return f"Error banning member: {e}"
        except discord.Forbidden:
            return f"Error: Discord denied banning {member}; hierarchy or missing ban_members"
        except Exception as e:
            return f"Error banning member: {e}"


class UnbanMemberTool(Tool):
    def get_description(self):
        return (
            "Unban a user by id. Requires ban_members. "
            "Params: user_id (required), reason (optional), guild_id (optional)."
        )

    async def execute(
        self,
        message: Message,
        user_id: str | None = None,
        reason: str | None = None,
        guild_id: str | None = None,
        **kwargs,
    ) -> str:
        guild, error = await _resolve_guild(self.bot, message, guild_id)
        if error:
            return error
        missing = _missing_cap(guild, "ban_members")
        if missing:
            return missing
        uid = _parse_snowflake(user_id)
        if uid is None:
            return "Error: user_id is required"
        try:
            await guild.unban(
                discord.Object(id=uid),
                reason=_mod_reason(message) if not reason else str(reason)[:512],
            )
            return f"Unbanned {uid} in {guild.name}"
        except discord.NotFound:
            return f"Error: user {uid} is not banned in {guild.name}"
        except discord.Forbidden:
            return f"Error: Discord denied unbanning {uid} in {guild.name}"
        except Exception as e:
            return f"Error unbanning member: {e}"


class ListBansTool(Tool):
    def get_description(self):
        return (
            "List banned users in a server. Requires ban_members. "
            "Params: guild_id (optional), limit (optional, default 20)."
        )

    async def execute(
        self,
        message: Message,
        guild_id: str | None = None,
        limit: str = "20",
        **kwargs,
    ) -> str:
        guild, error = await _resolve_guild(self.bot, message, guild_id)
        if error:
            return error
        missing = _missing_cap(guild, "ban_members")
        if missing:
            return missing
        try:
            cap = max(1, min(int(limit or 20), 50))
        except (TypeError, ValueError):
            cap = 20
        rows = []
        try:
            async for entry in guild.bans(limit=cap):
                user = getattr(entry, "user", None) or entry
                why = getattr(entry, "reason", None) or "no reason"
                rows.append(f"{getattr(user, 'name', user)} ({getattr(user, 'id', '?')}): {why}")
        except discord.Forbidden:
            return f"Error: cannot list bans in {guild.name}"
        except Exception as e:
            return f"Error listing bans: {e}"
        if not rows:
            return f"No bans in {guild.name}"
        return f"Bans in {guild.name} ({len(rows)}):\n" + "\n".join(rows)


class TimeoutMemberTool(Tool):
    def get_description(self):
        return (
            "Timeout or untimeout a member. Requires moderate_members. "
            "Params: user_id (required), duration (e.g. 10m, 1h, 1d; 0/clear to remove), "
            "reason (optional), guild_id (optional)."
        )

    async def execute(
        self,
        message: Message,
        user_id: str | None = None,
        duration: str | None = None,
        reason: str | None = None,
        guild_id: str | None = None,
        **kwargs,
    ) -> str:
        guild, error = await _resolve_guild(self.bot, message, guild_id)
        if error:
            return error
        missing = _missing_cap(guild, "moderate_members")
        if missing:
            return missing
        member, error = await _resolve_member(guild, user_id)
        if error:
            return error
        blocked = _moderation_block(guild, _guild_me(guild), member, action="timeout")
        if blocked:
            return blocked
        seconds = _parse_duration_seconds(duration, None)
        if seconds is None:
            return "Error: duration like 10m, 1h, 2d, or 0/clear to remove"
        until = None
        if seconds > 0:
            seconds = max(60, min(seconds, 28 * 86400))
            until = datetime.now(timezone.utc) + timedelta(seconds=seconds)
        why = _mod_reason(message) if not reason else str(reason)[:512]
        try:
            if hasattr(member, "timeout"):
                await member.timeout(until, reason=why)
            else:
                await member.edit(timed_out_until=until, reason=why)
            if until is None:
                return f"Removed timeout from {member} in {guild.name}"
            return f"Timed out {member} in {guild.name} until {until.isoformat()}"
        except discord.Forbidden:
            return f"Error: Discord denied timing out {member}"
        except Exception as e:
            return f"Error timing out member: {e}"


class ManageRoleTool(Tool):
    def get_description(self):
        return (
            "Create/edit/delete roles or add/remove them on members. Requires manage_roles. "
            "Params: action (list|create|edit|delete|add|remove), guild_id (optional), "
            "name, role_id, user_id, color (hex), hoist, mentionable, permissions "
            "(comma perm names), confirm_name (required to delete)."
        )

    async def execute(
        self,
        message: Message,
        action: str | None = None,
        guild_id: str | None = None,
        name: str | None = None,
        role_id: str | None = None,
        user_id: str | None = None,
        color: str | None = None,
        hoist: str | None = None,
        mentionable: str | None = None,
        permissions: str | None = None,
        confirm_name: str | None = None,
        **kwargs,
    ) -> str:
        guild, error = await _resolve_guild(self.bot, message, guild_id)
        if error:
            return error
        missing = _missing_cap(guild, "manage_roles")
        if missing:
            return missing
        me = _guild_me(guild)
        act = str(action or "list").strip().lower()
        why = _mod_reason(message)
        if act == "list":
            roles = sorted(
                list(getattr(guild, "roles", []) or []),
                key=lambda r: int(getattr(r, "position", 0) or 0),
                reverse=True,
            )
            lines = []
            for role in roles[:40]:
                lines.append(_role_label(role))
            return f"Roles in {guild.name} ({len(roles)}):\n" + "\n".join(lines or ["none"])
        if act == "create":
            clean = _clean_discord_name(name)
            if not clean:
                return "Error: name is required to create a role"
            kwargs_create = {"name": clean, "reason": why}
            colour = _colour_from_text(color)
            if colour is not None:
                kwargs_create["colour"] = colour
            perms = _permissions_from_names(permissions)
            if perms is not None:
                kwargs_create["permissions"] = perms
            if hoist is not None:
                kwargs_create["hoist"] = parse_bool(hoist, False)
            if mentionable is not None:
                kwargs_create["mentionable"] = parse_bool(mentionable, False)
            try:
                role = await guild.create_role(**kwargs_create)
                return f"Created role {_role_label(role)} in {guild.name}"
            except discord.Forbidden:
                return f"Error: Discord denied creating a role in {guild.name}"
            except Exception as e:
                return f"Error creating role: {e}"
        role, error = _find_role(guild, role_id or name)
        if error:
            return error
        blocked = _role_blocked(me, role)
        if blocked and act != "list":
            return blocked
        if act == "edit":
            updates = {}
            if name:
                clean = _clean_discord_name(name)
                if clean:
                    updates["name"] = clean
            colour = _colour_from_text(color)
            if colour is not None:
                updates["colour"] = colour
            perms = _permissions_from_names(permissions)
            if perms is not None:
                updates["permissions"] = perms
            if hoist is not None:
                updates["hoist"] = parse_bool(hoist, False)
            if mentionable is not None:
                updates["mentionable"] = parse_bool(mentionable, False)
            if not updates:
                return "Error: provide a field to edit"
            try:
                await role.edit(**updates, reason=why)
                return f"Edited role {_role_label(role)}: {', '.join(sorted(updates))}"
            except discord.Forbidden:
                return f"Error: Discord denied editing {role.name}"
            except Exception as e:
                return f"Error editing role: {e}"
        if act == "delete":
            actual = getattr(role, "name", "")
            if str(confirm_name or "") != actual:
                return f"Error: confirm_name must exactly match '{actual}'"
            try:
                label = _role_label(role)
                await role.delete(reason=why)
                return f"Deleted role {label} from {guild.name}"
            except discord.Forbidden:
                return f"Error: Discord denied deleting {actual}"
            except Exception as e:
                return f"Error deleting role: {e}"
        if act in {"add", "remove"}:
            member, error = await _resolve_member(guild, user_id)
            if error:
                return error
            blocked = _moderation_block(guild, me, member, action="role")
            if blocked:
                return blocked
            try:
                if act == "add":
                    await member.add_roles(role, reason=why)
                    return f"Added {role.name} to {member} in {guild.name}"
                await member.remove_roles(role, reason=why)
                return f"Removed {role.name} from {member} in {guild.name}"
            except discord.Forbidden:
                return f"Error: Discord denied changing roles on {member}"
            except Exception as e:
                return f"Error changing roles: {e}"
        return "Error: action must be list, create, edit, delete, add, or remove"


class PurgeMessagesTool(Tool):
    def get_description(self):
        return (
            "Bulk-delete recent messages in a channel. Requires manage_messages. "
            "Params: limit (1-100, default 20), channel_id (optional), user_id (optional filter)."
        )

    async def execute(
        self,
        message: Message,
        limit: str = "20",
        channel_id: str | None = None,
        user_id: str | None = None,
        **kwargs,
    ) -> str:
        channel = getattr(message, "channel", None)
        if channel_id:
            channel, error = await _get_guild_channel(self.bot, channel_id)
            if error:
                return error
        guild = getattr(channel, "guild", None)
        if guild is None:
            return "Error: purge only works in servers"
        missing = _missing_cap(guild, "manage_messages")
        if missing:
            return missing
        if not hasattr(channel, "purge"):
            return "Error: this channel type cannot be purged"
        try:
            cap = max(1, min(int(limit or 20), 100))
        except (TypeError, ValueError):
            return "Error: limit must be a number"
        uid = _parse_snowflake(user_id)

        def _check(msg):
            if uid is None:
                return True
            return getattr(getattr(msg, "author", None), "id", None) == uid

        try:
            deleted = await channel.purge(limit=cap, check=_check, reason=_mod_reason(message))
            return f"Purged {len(deleted)} messages in {_channel_label(channel)}"
        except discord.Forbidden:
            return f"Error: Discord denied purging {_channel_label(channel)}"
        except Exception as e:
            return f"Error purging messages: {e}"


class PinMessageTool(Tool):
    def get_description(self):
        return (
            "Pin or unpin a message. Needs pin_messages or manage_messages. "
            "Params: message_id (required), channel_id (optional), unpin (optional bool)."
        )

    async def execute(
        self,
        message: Message,
        message_id: str | None = None,
        channel_id: str | None = None,
        unpin: str = "false",
        **kwargs,
    ) -> str:
        if not message_id:
            return "Error: message_id is required"
        channel = getattr(message, "channel", None)
        if channel_id:
            channel, error = await _get_guild_channel(self.bot, channel_id)
            if error:
                return error
        guild = getattr(channel, "guild", None)
        if guild is None:
            return "Error: pin only works in servers"
        if not (
            _has_guild_cap(guild, "pin_messages")
            or _has_guild_cap(guild, "manage_messages")
        ):
            return _missing_cap(guild, "pin_messages")
        try:
            msg = await channel.fetch_message(int(str(message_id).strip()))
            if parse_bool(unpin, False):
                await msg.unpin(reason=_mod_reason(message))
                return f"Unpinned message {message_id}"
            await msg.pin(reason=_mod_reason(message))
            return f"Pinned message {message_id}"
        except discord.NotFound:
            return f"Error: message {message_id} not found"
        except discord.Forbidden:
            return "Error: Discord denied pinning that message"
        except Exception as e:
            return f"Error pinning message: {e}"


class SetMemberNicknameTool(Tool):
    def get_description(self):
        return (
            "Change another member's nickname. Requires manage_nicknames. "
            "Params: user_id (required), nickname (required, 'reset' to clear), guild_id (optional)."
        )

    async def execute(
        self,
        message: Message,
        user_id: str | None = None,
        nickname: str | None = None,
        guild_id: str | None = None,
        **kwargs,
    ) -> str:
        if not nickname:
            return "Error: nickname is required"
        guild, error = await _resolve_guild(self.bot, message, guild_id)
        if error:
            return error
        missing = _missing_cap(guild, "manage_nicknames")
        if missing:
            return missing
        member, error = await _resolve_member(guild, user_id)
        if error:
            return error
        blocked = _moderation_block(guild, _guild_me(guild), member, action="nick")
        if blocked:
            return blocked
        nick = None if str(nickname).strip().lower() == "reset" else str(nickname)[:32]
        try:
            await member.edit(nick=nick, reason=_mod_reason(message))
            if nick:
                return f"Set {member}'s nickname to {nick}"
            return f"Cleared {member}'s nickname"
        except discord.Forbidden:
            return f"Error: Discord denied changing nickname for {member}"
        except Exception as e:
            return f"Error setting nickname: {e}"


class VoiceModTool(Tool):
    def get_description(self):
        return (
            "Server-mute, deafen, move, or disconnect a member in voice. "
            "Params: action (mute|unmute|deafen|undeafen|move|disconnect), "
            "user_id (required), channel_id (for move), guild_id (optional)."
        )

    async def execute(
        self,
        message: Message,
        action: str | None = None,
        user_id: str | None = None,
        channel_id: str | None = None,
        guild_id: str | None = None,
        **kwargs,
    ) -> str:
        guild, error = await _resolve_guild(self.bot, message, guild_id)
        if error:
            return error
        act = str(action or "").strip().lower()
        cap = {
            "mute": "mute_members",
            "unmute": "mute_members",
            "deafen": "deafen_members",
            "undeafen": "deafen_members",
            "move": "move_members",
            "disconnect": "move_members",
        }.get(act)
        if not cap:
            return "Error: action must be mute, unmute, deafen, undeafen, move, or disconnect"
        missing = _missing_cap(guild, cap)
        if missing:
            return missing
        member, error = await _resolve_member(guild, user_id)
        if error:
            return error
        blocked = _moderation_block(guild, _guild_me(guild), member, action="voice")
        if blocked:
            return blocked
        why = _mod_reason(message)
        try:
            if act == "mute":
                await member.edit(mute=True, reason=why)
                return f"Server-muted {member}"
            if act == "unmute":
                await member.edit(mute=False, reason=why)
                return f"Unmuted {member}"
            if act == "deafen":
                await member.edit(deafen=True, reason=why)
                return f"Server-deafened {member}"
            if act == "undeafen":
                await member.edit(deafen=False, reason=why)
                return f"Undeafened {member}"
            if act == "disconnect":
                await member.edit(voice_channel=None, reason=why)
                return f"Disconnected {member} from voice"
            dest, error = await _get_guild_channel(self.bot, channel_id)
            if error:
                return error
            if not isinstance(dest, discord.VoiceChannel):
                return "Error: move requires a voice channel_id"
            await member.edit(voice_channel=dest, reason=why)
            return f"Moved {member} to {_channel_label(dest)}"
        except discord.Forbidden:
            return f"Error: Discord denied voice mod on {member}"
        except Exception as e:
            return f"Error in voice_mod: {e}"


class LockChannelTool(Tool):
    def get_description(self):
        return (
            "Lock or unlock a channel for @everyone (deny/allow send or connect). "
            "Needs manage_channels or manage_roles. Params: channel_id (optional), "
            "unlock (optional bool)."
        )

    async def execute(
        self,
        message: Message,
        channel_id: str | None = None,
        unlock: str = "false",
        **kwargs,
    ) -> str:
        channel = getattr(message, "channel", None)
        if channel_id:
            channel, error = await _get_guild_channel(self.bot, channel_id)
            if error:
                return error
        guild = getattr(channel, "guild", None)
        if guild is None:
            return "Error: lock only works in servers"
        if not (
            _has_guild_cap(guild, "manage_channels")
            or _has_guild_cap(guild, "manage_roles")
        ):
            return _missing_cap(guild, "manage_channels")
        target = getattr(guild, "default_role", None)
        if target is None:
            return "Error: @everyone role is unavailable"
        locked = not parse_bool(unlock, False)
        try:
            if isinstance(channel, discord.VoiceChannel):
                await channel.set_permissions(
                    target, connect=False if locked else None, reason=_mod_reason(message)
                )
            else:
                await channel.set_permissions(
                    target,
                    send_messages=False if locked else None,
                    reason=_mod_reason(message),
                )
            state = "Locked" if locked else "Unlocked"
            return f"{state} {_channel_label(channel)} for @everyone"
        except discord.Forbidden:
            return f"Error: Discord denied locking {_channel_label(channel)}"
        except Exception as e:
            return f"Error locking channel: {e}"


class SetChannelPermissionsTool(Tool):
    def get_description(self):
        return (
            "Set or clear a channel permission overwrite for a role or member. "
            "Needs manage_roles. Params: channel_id (required), target (role/user id "
            "or 'everyone'), allow (comma perm=true/false/inherit), reset (bool)."
        )

    async def execute(
        self,
        message: Message,
        channel_id: str | None = None,
        target: str | None = None,
        allow: str | None = None,
        reset: str = "false",
        **kwargs,
    ) -> str:
        if not channel_id or not target:
            return "Error: channel_id and target are required"
        channel, error = await _get_guild_channel(self.bot, channel_id)
        if error:
            return error
        guild = channel.guild
        missing = _missing_cap(guild, "manage_roles")
        if missing:
            return missing
        spec = str(target).strip().lower()
        subject = None
        if spec in {"everyone", "@everyone", "default"}:
            subject = guild.default_role
        if subject is None:
            subject, _err = _find_role(guild, target)
        if subject is None:
            subject, error = await _resolve_member(guild, target)
            if error and subject is None:
                return f"Error: target '{target}' is not a role, member, or everyone"
        why = _mod_reason(message)
        try:
            if parse_bool(reset, False):
                await channel.set_permissions(subject, overwrite=None, reason=why)
                return f"Cleared overwrites for {target} on {_channel_label(channel)}"
            pairs = _parse_overwrite_pairs(allow)
            if not pairs:
                return "Error: provide allow like send_messages=false,view_channel=true"
            await channel.set_permissions(subject, reason=why, **pairs)
            return f"Updated overwrites for {target} on {_channel_label(channel)}: {pairs}"
        except discord.Forbidden:
            return f"Error: Discord denied editing overwrites on {_channel_label(channel)}"
        except Exception as e:
            return f"Error setting channel permissions: {e}"


class EditServerTool(Tool):
    def get_description(self):
        return (
            "Edit the server name or description. Requires manage_guild. "
            "Params: name (optional), description (optional), guild_id (optional)."
        )

    async def execute(
        self,
        message: Message,
        name: str | None = None,
        description: str | None = None,
        guild_id: str | None = None,
        **kwargs,
    ) -> str:
        guild, error = await _resolve_guild(self.bot, message, guild_id)
        if error:
            return error
        missing = _missing_cap(guild, "manage_guild")
        if missing:
            return missing
        updates = {}
        if name:
            clean = _clean_discord_name(name)
            if clean:
                updates["name"] = clean
        if description is not None:
            updates["description"] = str(description)[:120]
        if not updates:
            return "Error: provide name or description"
        try:
            await guild.edit(**updates, reason=_mod_reason(message))
            return f"Edited {guild.name}: {', '.join(sorted(updates))}"
        except discord.Forbidden:
            return f"Error: Discord denied editing {guild.name}"
        except Exception as e:
            return f"Error editing server: {e}"


class AuditLogTool(Tool):
    def get_description(self):
        return (
            "Read recent audit-log entries. Requires view_audit_log. "
            "Params: guild_id (optional), limit (optional, default 10)."
        )

    async def execute(
        self,
        message: Message,
        guild_id: str | None = None,
        limit: str = "10",
        **kwargs,
    ) -> str:
        guild, error = await _resolve_guild(self.bot, message, guild_id)
        if error:
            return error
        missing = _missing_cap(guild, "view_audit_log")
        if missing:
            return missing
        try:
            cap = max(1, min(int(limit or 10), 25))
        except (TypeError, ValueError):
            cap = 10
        rows = []
        try:
            async for entry in guild.audit_logs(limit=cap):
                actor = getattr(getattr(entry, "user", None), "name", "?")
                action = getattr(getattr(entry, "action", None), "name", entry.action)
                target = getattr(entry, "target", None)
                rows.append(f"{actor} {action} {target}")
        except discord.Forbidden:
            return f"Error: cannot read audit log in {guild.name}"
        except Exception as e:
            return f"Error reading audit log: {e}"
        if not rows:
            return f"No audit-log entries in {guild.name}"
        return f"Audit log for {guild.name}:\n" + "\n".join(rows)


class ManageEmojiTool(Tool):
    def get_description(self):
        return (
            "List, create, or delete custom emojis. Requires manage_expressions. "
            "Params: action (list|create|delete), name, url (image for create), "
            "emoji_id or name (for delete), guild_id (optional)."
        )

    async def execute(
        self,
        message: Message,
        action: str | None = None,
        name: str | None = None,
        url: str | None = None,
        emoji_id: str | None = None,
        guild_id: str | None = None,
        **kwargs,
    ) -> str:
        guild, error = await _resolve_guild(self.bot, message, guild_id)
        if error:
            return error
        missing = _missing_cap(guild, "manage_expressions")
        if missing:
            return missing
        act = str(action or "list").strip().lower()
        emojis = list(getattr(guild, "emojis", []) or [])
        if act == "list":
            if not emojis:
                return f"No custom emojis in {guild.name}"
            return (
                f"Emojis in {guild.name} ({len(emojis)}):\n"
                + "\n".join(f":{e.name}: ({e.id})" for e in emojis[:40])
            )
        if act == "create":
            clean = re.sub(r"[^A-Za-z0-9_]", "", str(name or ""))[:32]
            if len(clean) < 2:
                return "Error: emoji name must be 2-32 letters/numbers/underscore"
            if not url or not _is_safe_url(url):
                return "Error: a public image url is required"
            try:
                session = await _get_shared_session()
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=30), allow_redirects=False
                ) as resp:
                    if resp.status != 200:
                        return f"Error: could not download image (status {resp.status})"
                    image = await _read_response_limited(resp, 256 * 1024)
                emoji = await guild.create_custom_emoji(
                    name=clean, image=image, reason=_mod_reason(message)
                )
                return f"Created emoji :{emoji.name}: ({emoji.id}) in {guild.name}"
            except discord.Forbidden:
                return f"Error: Discord denied creating emoji in {guild.name}"
            except Exception as e:
                return f"Error creating emoji: {e}"
        if act == "delete":
            spec = str(emoji_id or name or "").strip().strip(":")
            eid = _parse_snowflake(spec)
            emoji = None
            if eid is not None:
                emoji = next((e for e in emojis if e.id == eid), None)
            if emoji is None:
                matches = [e for e in emojis if e.name.lower() == spec.lower()]
                emoji = matches[0] if len(matches) == 1 else None
            if emoji is None:
                return f"Error: emoji '{spec}' not found"
            try:
                label = f":{emoji.name}: ({emoji.id})"
                await emoji.delete(reason=_mod_reason(message))
                return f"Deleted emoji {label} from {guild.name}"
            except discord.Forbidden:
                return f"Error: Discord denied deleting :{emoji.name}:"
            except Exception as e:
                return f"Error deleting emoji: {e}"
        return "Error: action must be list, create, or delete"


class ChangeAvatarTool(Tool):
    """Change the bot's own profile picture"""

    def get_description(self):
        return (
            "Change your profile picture (admin only). Params: url (direct jpg/png/gif/webp). "
            "Discord rate-limits spam."
        )

    async def execute(self, message: Message, url: str | None = None, **kwargs) -> str:
        if not self.bot or not self.bot._is_admin(message.author.id):
            return "Error: Changing avatar is restricted to admins only."

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


# ── site file plumbing ────────────────────────────────────────────────────
# A site is a directory, not a single index.html. These helpers are what let
# create_site/edit_site write a stylesheet, a second page, a JSON fixture, or
# a service worker without any of it being special-cased in the tool bodies.

SITE_MAX_FILES = 60
SITE_MAX_TOTAL_BYTES = 12_000_000
# Extensions a static host will serve as-is. Anything executable server-side
# (.php, .cgi) is pointless here and only invites confusion about what runs.
SITE_BLOCKED_SUFFIXES = {".php", ".php5", ".phtml", ".cgi", ".pl", ".jsp", ".asp", ".aspx"}


def _safe_site_relpath(raw: Any) -> str | None:
    """Normalize a model-supplied path into a safe relative path inside a site.

    Returns None for anything that escapes, hides, or would not be served.
    """
    text = str(raw or "").strip().replace("\\", "/").lstrip("/")
    if not text or len(text) > 200:
        return None
    parts = []
    for part in text.split("/"):
        part = part.strip()
        if not part or part == ".":
            continue
        if part == ".." or part.startswith("."):
            return None
        if not re.fullmatch(r"[A-Za-z0-9._ -]{1,80}", part):
            return None
        parts.append(part)
    if not parts or len(parts) > 6:
        return None
    rel = "/".join(parts)
    if Path(rel).suffix.lower() in SITE_BLOCKED_SUFFIXES:
        return None
    return rel


def _site_child_path(site_dir: str, rel: str) -> Path | None:
    """Resolve rel under site_dir, refusing anything that lands outside it."""
    base = Path(site_dir).resolve()
    try:
        target = (base / rel).resolve()
    except (OSError, ValueError):
        return None
    if target != base and base not in target.parents:
        return None
    return target


def _decode_site_file(content: Any, encoding: str | None) -> tuple[bytes | None, str]:
    """(bytes, '') or (None, error). base64 keeps exact bytes for binaries."""
    mode = str(encoding or "text").strip().lower()
    if mode in {"base64", "b64"}:
        try:
            return base64.b64decode(str(content), validate=True), ""
        except Exception as e:
            return None, f"bad base64: {e}"
    if mode not in {"text", "utf8", "utf-8", ""}:
        return None, "encoding must be text or base64"
    if content is None:
        return None, "missing content"
    if not isinstance(content, str):
        content = json.dumps(content, indent=2, ensure_ascii=False)
    return content.encode("utf-8"), ""


def _parse_site_files(files: Any) -> tuple[list[dict], str]:
    """Accept the three shapes a model actually emits.

    ``{"style.css": "..."}``, ``[{"path": ..., "content": ...}]``, or either of
    those as a JSON string. Returns (entries, error).
    """
    if not files:
        return [], ""
    raw = files
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as e:
            return [], f"files must be JSON: {e}"
    entries: list[dict] = []
    if isinstance(raw, dict):
        # {"path": "content"} — but tolerate a single {"path":..,"content":..}
        if "path" in raw and ("content" in raw or "encoding" in raw):
            raw = [raw]
        else:
            raw = [{"path": k, "content": v} for k, v in raw.items()]
    if not isinstance(raw, list):
        return [], "files must be an object or a list"
    for item in raw:
        if not isinstance(item, dict):
            return [], "each file needs {path, content}"
        rel = _safe_site_relpath(item.get("path") or item.get("name"))
        if not rel:
            return [], f"unsafe or unsupported file path: {item.get('path')!r}"
        blob, err = _decode_site_file(item.get("content"), item.get("encoding"))
        if err:
            return [], f"{rel}: {err}"
        entries.append({"path": rel, "bytes": blob})
    if len(entries) > SITE_MAX_FILES:
        return [], f"too many files ({len(entries)}, max {SITE_MAX_FILES})"
    return entries, ""


async def _write_site_file(site_dir: str, rel: str, blob: bytes) -> str:
    """Atomic write of one file inside a site. Returns '' or an error."""
    target = _site_child_path(site_dir, rel)
    if target is None:
        return f"{rel}: path escapes the site directory"
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(target) + ".tmp"
    try:
        async with aiofiles.open(tmp, "wb") as f:
            await f.write(blob)
            await f.flush()
        os.replace(tmp, target)
    except Exception as e:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        return f"{rel}: write failed: {e}"
    return ""


def _site_tree(site_dir: str, limit: int = 60) -> list[tuple[str, int]]:
    """(relative path, bytes) for everything in a site, sorted, index first."""
    base = Path(site_dir)
    out: list[tuple[str, int]] = []
    if not base.is_dir():
        return out
    for path in sorted(base.rglob("*")):
        if not path.is_file() or path.name.endswith(".tmp"):
            continue
        try:
            rel = str(path.relative_to(base))
            out.append((rel, path.stat().st_size))
        except (ValueError, OSError):
            continue
        if len(out) >= limit:
            break
    out.sort(key=lambda item: (item[0] != "index.html", item[0]))
    return out


def _site_ttl_seconds(control: dict) -> float:
    """0 means sites never expire. Default 24h, admin-tunable, not baked in."""
    try:
        hours = float(control.get("site_ttl_hours", 24) or 0)
    except (TypeError, ValueError):
        hours = 24.0
    return max(0.0, hours) * 3600.0


def site_expiry_label(entry: dict, control: dict) -> str:
    """'6h 12m left' / 'permanent' — shared by list_sites and edit_site."""
    if entry.get("permanent"):
        return "permanent"
    ttl = _site_ttl_seconds(control)
    per_site = entry.get("ttl_hours")
    if per_site is not None:
        try:
            ttl = max(0.0, float(per_site)) * 3600.0
        except (TypeError, ValueError):
            pass
    if ttl <= 0:
        return "permanent"
    remaining = ttl - (
        datetime.now(timezone.utc).timestamp() - float(entry.get("created_at", 0) or 0)
    )
    if remaining <= 0:
        return "expiring now"
    return f"{int(remaining // 3600)}h {int((remaining % 3600) // 60)}m left"


# Optional hardening for operators whose static host does NOT set a CSP for
# generated pages. Off by default: a meta tag injected into the model's own
# document can only ever subtract from what the page was written to do, and
# the hosting layer is where this belongs. Flip `site_inject_csp` on in the
# dashboard if your deployment serves /bot without its own policy.
SITE_CSP_META = (
    '<meta http-equiv="Content-Security-Policy" '
    'content="default-src https: data: blob:; '
    "img-src https: data: blob:; "
    "style-src 'unsafe-inline' https:; "
    "script-src 'unsafe-inline' 'unsafe-eval' https:; "
    "font-src https: data:; "
    "connect-src https:; "
    'media-src https: data: blob:;">'
)


def _inject_site_csp(body: str) -> str:
    """Put SITE_CSP_META in the document head, unless the page set its own."""
    if re.search(r"http-equiv\s*=\s*[\"']?Content-Security-Policy", body, re.IGNORECASE):
        return body
    if re.search(r"<head[^>]*>", body, re.IGNORECASE):
        return re.sub(
            r"(<head[^>]*>)", r"\1\n" + SITE_CSP_META, body, count=1, flags=re.IGNORECASE
        )
    if re.search(r"<html[^>]*>", body, re.IGNORECASE):
        return re.sub(
            r"(<html[^>]*>)",
            r"\1\n<head>" + SITE_CSP_META + "</head>",
            body,
            count=1,
            flags=re.IGNORECASE,
        )
    return "<head>" + SITE_CSP_META + "</head>\n" + body


class CreateSiteTool(Tool):
    """Publish a website — one page or a whole directory, static or backed."""

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

    def _control(self) -> dict:
        return (
            getattr(self.bot, "control", {}) or getattr(self.bot, "_control", {}) or {}
        )

    def get_description(self):
        return (
            f"Publish a site at {self.base_url}/<name>/. "
            "Full visual freedom: invent a new design each time (layout, type, color, "
            "density, motion). Do not reuse a house style or clone a previous site "
            "unless the user asked for a specific look. "
            "Params: name (slug), title (listing/metadata, not a required on-page heading), "
            "body (complete HTML document for index.html; served byte-for-byte), "
            "files (extra files as {\"path\": \"content\"} — style.css, app.js, "
            "about/index.html, data.json, anything), "
            "backend (true for a live server-side store: named values + "
            "append-only lists at /api/site/<name>/, same origin, no key — "
            "use it for guestbooks, counters, saved state, submissions), "
            "encoding (text|base64), permanent (true to skip auto-expiry). "
            "Generate images in a prior turn and paste CDN URLs "
            "into the HTML — don't batch image_generator with create_site. "
            "Use edit_site to change a published site instead of re-sending it whole. "
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
        files: Any = None,
        backend: Any = None,
        permanent: Any = None,
        **kwargs,
    ) -> str:
        # Available to everyone (non-admins too). Quota + ownership checks apply.
        extra_files, files_err = _parse_site_files(files)
        if files_err:
            return f"Error: {files_err}"
        has_index = any(f["path"] == "index.html" for f in extra_files)
        if not name or not title or (body is None and not has_index):
            missing = []
            if not name:
                missing.append("name")
            if not title:
                missing.append("title")
            if body is None and not has_index:
                missing.append("body (or files with an index.html)")
            return (
                f"Error: missing required params — {', '.join(missing)}. "
                "name + title + body are the minimum for a site."
            )

        mode = str(encoding or "text").strip().lower()
        if body is None:
            body = ""
        elif mode in {"base64", "b64"}:
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

        control = self._control()
        max_sites = int(control.get("create_site_quota_per_user", 10))
        active_user_sites = [s for s in sites.values() if s.get("user_id") == user_id]
        already_ours = (
            isinstance(existing, dict)
            and str(existing.get("user_id") or "") == user_id
        )
        if not already_ours and len(active_user_sites) >= max_sites:
            return (
                f"Error: site quota reached ({len(active_user_sites)}/{max_sites} active sites). "
                "Use delete_site on an old slug first, or edit_site to reuse one."
            )

        if len(body) > self.MAX_CONTENT_SIZE:
            return f"Error: content too long ({len(body)} chars, max {self.MAX_CONTENT_SIZE})"
        extra_bytes = sum(len(f["bytes"] or b"") for f in extra_files)
        if len(body.encode("utf-8")) + extra_bytes > SITE_MAX_TOTAL_BYTES:
            return f"Error: site too large (max {SITE_MAX_TOTAL_BYTES // 1000}KB across all files)"

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

            # The page is served exactly as written. CSP belongs to the host
            # (see SITE_CSP_META) — turn `site_inject_csp` on only if yours
            # doesn't set one.
            if body and parse_bool(control.get("site_inject_csp", False), False):
                body = _inject_site_csp(body)

            written: list[str] = []
            if body:
                err = await _write_site_file(site_dir, "index.html", body.encode("utf-8"))
                if err:
                    return f"Error creating site: {err}"
                written.append("index.html")
            for entry in extra_files:
                err = await _write_site_file(site_dir, entry["path"], entry["bytes"] or b"")
                if err:
                    return f"Error creating site: {err}"
                written.append(entry["path"])

            wants_backend = parse_bool(backend, False)
            is_permanent = parse_bool(permanent, False)

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
                "backend": wants_backend,
                "permanent": is_permanent,
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
            if len(written) > 1:
                result += f"\nFiles: {', '.join(written)}"
            if wants_backend:
                result += "\n" + site_backend.client_guide(f"/api/site/{slug}")
            result += f"\nLifetime: {site_expiry_label(site_entry, control)}."
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
        max_sites = int(self._control().get("create_site_quota_per_user", 10))
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


class _SiteOwnedTool(Tool):
    """Shared lookup for tools that act on an already-published site."""

    def __init__(self, bot):
        super().__init__(bot)
        self.base_dir = getattr(bot.config, "MAXWELL_SITE_DIR", "public/bot")
        self.base_url = (
            getattr(
                bot.config, "MAXWELL_PUBLIC_BASE_URL", "https://maxwell.example.com"
            ).rstrip("/")
            + "/bot"
        )

    def _control(self) -> dict:
        return (
            getattr(self.bot, "control", {}) or getattr(self.bot, "_control", {}) or {}
        )

    def _resolve(self, message: Message, name: str | None):
        """(slug, entry, site_dir, None) or (None, None, None, error string)."""
        slug = re.sub(r"[^a-z0-9-]", "-", str(name or "").lower().strip())[:30].strip("-")
        if not slug:
            return None, None, None, "Error: name is required (the site slug)."
        if hasattr(self.bot, "_load_sites"):
            self.bot._load_sites(quiet=True)
        entry = (self.bot._sites or {}).get(slug)
        if not isinstance(entry, dict):
            return None, None, None, (
                f"Error: no site named '{slug}'. Call list_sites to see the slugs you own."
            )
        owner = str(entry.get("user_id") or "")
        if (
            owner
            and owner != str(message.author.id)
            and not self.bot._is_admin(message.author.id)
        ):
            return None, None, None, f"Error: site '{slug}' belongs to someone else."
        return slug, entry, os.path.join(self.base_dir, slug), None

    def _save_entry(self, slug: str, entry: dict) -> None:
        path = Path(self.bot.config.DATA_DIR) / "sites.json"
        with FileLock(path, timeout=15.0):
            sites = {}
            try:
                if path.exists():
                    data = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        sites = {k: v for k, v in data.items() if isinstance(v, dict)}
            except (json.JSONDecodeError, OSError, ValueError):
                sites = dict(self.bot._sites or {})
            if entry is None:
                sites.pop(slug, None)
            else:
                sites[slug] = entry
            _atomic_json_write_sync(path, sites)
            self.bot._sites = sites
            with contextlib.suppress(OSError):
                self.bot._sites_mtime = path.stat().st_mtime


class EditSiteTool(_SiteOwnedTool):
    """Change a published site in place — a file, a line, or its settings."""

    def get_description(self):
        return (
            "Edit a site you already published, at its existing URL. "
            "action=list (files + sizes), read (one file back), write (replace or "
            "add a file — path defaults to index.html), replace (swap the first "
            "occurrence of `find` with `replace` in one file; the cheap way to "
            "fix a typo, color, or line without resending the page), delete "
            "(remove a file), rename (slug stays, `title` changes), backend "
            "(on/off/status/clear the site's server-side store), extend (reset "
            "the expiry clock; permanent=true to stop it expiring). "
            "Params: name (slug), action, path, content, find, replace, title, "
            "encoding (text|base64), backend, permanent. "
            "Prefer this over re-running create_site for a tweak."
        )

    async def execute(
        self,
        message: Message,
        name: str | None = None,
        action: str = "list",
        path: str | None = None,
        content: Any = None,
        find: str | None = None,
        replace: str | None = None,
        title: str | None = None,
        encoding: str = "text",
        backend: Any = None,
        permanent: Any = None,
        **kwargs,
    ) -> str:
        slug, entry, site_dir, err = self._resolve(message, name)
        if err:
            return err
        act = str(action or "list").strip().lower()
        url = f"{self.base_url}/{slug}/"

        if act in {"list", "ls", "files", "status"}:
            tree = _site_tree(site_dir)
            if not tree:
                return f"{url} has no files on disk (it may have expired)."
            lines = [f"  • {rel} ({size} bytes)" for rel, size in tree]
            out = f"{url}\n" + "\n".join(lines)
            out += f"\nLifetime: {site_expiry_label(entry, self._control())}."
            if entry.get("backend"):
                out += "\nBackend: on — " + site_backend.summarize(
                    self.bot.config.DATA_DIR, slug
                )
            return out

        if act in {"read", "cat", "get"}:
            rel = _safe_site_relpath(path or "index.html")
            if not rel:
                return f"Error: bad path {path!r}"
            target = _site_child_path(site_dir, rel)
            if target is None or not target.is_file():
                return f"Error: {rel} not found in {slug}. Use action=list."
            try:
                text = target.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as e:
                return f"Error reading {rel}: {e}"
            if len(text) > 60000:
                return (
                    f"{rel} is {len(text)} chars — too big to return whole. "
                    "Use action=replace with a `find` string to patch it."
                )
            return f"{rel} ({len(text)} chars):\n{text}"

        if act in {"write", "put", "set", "update"}:
            rel = _safe_site_relpath(path or "index.html")
            if not rel:
                return f"Error: bad path {path!r}"
            blob, derr = _decode_site_file(content, encoding)
            if derr:
                return f"Error: {derr}"
            if rel.endswith(".html") and str(encoding or "text").lower() in {
                "text",
                "utf8",
                "utf-8",
                "",
            }:
                blob = _normalize_site_body_text_escapes(
                    blob.decode("utf-8", "replace")
                ).encode("utf-8")
            werr = await _write_site_file(site_dir, rel, blob or b"")
            if werr:
                return f"Error: {werr}"
            return f"Wrote {rel} ({len(blob or b'')} bytes) → {url}"

        if act in {"replace", "patch", "sub"}:
            rel = _safe_site_relpath(path or "index.html")
            if not rel:
                return f"Error: bad path {path!r}"
            if not find:
                return "Error: replace needs `find` (the exact text to swap out)."
            target = _site_child_path(site_dir, rel)
            if target is None or not target.is_file():
                return f"Error: {rel} not found in {slug}."
            try:
                text = target.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as e:
                return f"Error reading {rel}: {e}"
            if find not in text:
                return (
                    f"Error: `find` text is not in {rel} — it must match byte-for-byte. "
                    "Use action=read to see the current file."
                )
            hits = text.count(find)
            updated = text.replace(find, replace or "", 1)
            werr = await _write_site_file(site_dir, rel, updated.encode("utf-8"))
            if werr:
                return f"Error: {werr}"
            extra = f" ({hits - 1} more occurrence(s) left alone)" if hits > 1 else ""
            return f"Patched {rel}{extra} → {url}"

        if act in {"delete", "rm", "remove"}:
            rel = _safe_site_relpath(path or "")
            if not rel:
                return "Error: delete needs a path. To remove the whole site use delete_site."
            if rel == "index.html":
                return "Error: refusing to delete index.html — write a new one instead."
            target = _site_child_path(site_dir, rel)
            if target is None or not target.is_file():
                return f"Error: {rel} not found in {slug}."
            try:
                target.unlink()
            except OSError as e:
                return f"Error deleting {rel}: {e}"
            return f"Deleted {rel} from {slug}."

        if act in {"rename", "title", "retitle"}:
            if not title:
                return "Error: rename needs `title`."
            entry = dict(entry)
            entry["title"] = str(title)[:200]
            await asyncio.to_thread(self._save_entry, slug, entry)
            return f"Retitled {slug} → '{entry['title']}' ({url})"

        if act in {"backend", "store", "data"}:
            mode = str(backend if backend is not None else "status").strip().lower()
            data_dir = self.bot.config.DATA_DIR
            if mode in {"clear", "wipe", "reset"}:
                await asyncio.to_thread(site_backend.wipe, data_dir, slug)
                return f"Cleared the backend store for {slug}."
            if mode in {"status", "", "none"}:
                if not entry.get("backend"):
                    return (
                        f"{slug} has no backend. Turn it on with "
                        "edit_site(action=backend, backend=true)."
                    )
                return f"{slug} backend: " + site_backend.summarize(data_dir, slug)
            enabled = parse_bool(mode, False)
            entry = dict(entry)
            entry["backend"] = enabled
            await asyncio.to_thread(self._save_entry, slug, entry)
            if not enabled:
                return f"Backend off for {slug} (data kept; /api/site/{slug} now 404s)."
            return f"Backend on for {slug}.\n" + site_backend.client_guide(
                f"/api/site/{slug}"
            )

        if act in {"extend", "renew", "keep"}:
            entry = dict(entry)
            entry["created_at"] = datetime.now(timezone.utc).timestamp()
            if permanent is not None:
                entry["permanent"] = parse_bool(permanent, False)
            await asyncio.to_thread(self._save_entry, slug, entry)
            return f"{slug}: {site_expiry_label(entry, self._control())} ({url})"

        return (
            f"Error: unknown action '{act}'. Use list, read, write, replace, "
            "delete, rename, backend, or extend."
        )


class SiteServerTool(_SiteOwnedTool):
    """Give a site a real backend: its own Python server in its own container."""

    def get_description(self):
        return (
            "Run a real backend server for one of your sites — your own Python, "
            "your own routes, your own database, your own secrets, in its own "
            "sandboxed container reached at /bot/<name>/api/... "
            "Use this when the site needs server-side logic: user accounts and "
            "login, WebSockets for multiplayer or live chat, a hidden API key, "
            "anything a static page cannot enforce. "
            "action=write (files={\"app.py\": ...} then it starts), start, stop, "
            "restart, status, logs, read, env (set secrets), delete. "
            "app.py listens on 0.0.0.0:$PORT. flask+waitress for plain HTTP, "
            "fastapi+uvicorn when you need WebSockets; sqlalchemy, bcrypt, pyjwt, "
            "httpx, pillow are installed, packages=[...] adds more. Only /data is "
            "writable and only /data persists. "
            "Write the page with create_site, the server with this."
        )

    async def execute(
        self,
        message: Message,
        name: str | None = None,
        action: str = "status",
        files: Any = None,
        env: Any = None,
        packages: Any = None,
        path: str | None = None,
        lines: int = 40,
        **kwargs,
    ) -> str:
        slug, entry, _site_dir, err = self._resolve(message, name)
        if err:
            return err
        data_dir = self.bot.config.DATA_DIR
        act = str(action or "status").strip().lower()
        try:
            if act in {"write", "deploy", "code", "create"}:
                if not files:
                    return (
                        "Error: write needs files, e.g. "
                        'files={"app.py": "..."}.\n' + site_server.contract(slug)
                    )
                parsed = await asyncio.to_thread(site_server.parse_files, files)
                if "app.py" not in parsed:
                    return "Error: the entry file must be called app.py."
                new_env = (
                    await asyncio.to_thread(site_server.parse_env, env)
                    if env
                    else None
                )
                extra = await asyncio.to_thread(site_server.parse_packages, packages)
                written = await asyncio.to_thread(
                    site_server.write_code, data_dir, slug, parsed
                )
                await site_server.start(
                    data_dir, slug, env=new_env, packages=extra or None
                )
                await self._mark_server(slug, entry, True)
                return (
                    f"Backend server live: {self.base_url}/{slug}/api/ "
                    f"(wrote {', '.join(written)})\n" + site_server.contract(slug)
                )

            if act in {"start", "restart", "reload"}:
                await site_server.start(data_dir, slug)
                await self._mark_server(slug, entry, True)
                return f"Backend server running at {self.base_url}/{slug}/api/"

            if act in {"stop", "pause"}:
                existed = await site_server.stop(data_dir, slug)
                await self._mark_server(slug, entry, False)
                return (
                    f"Stopped the backend for {slug} (code, data, and secrets kept)."
                    if existed
                    else f"{slug} had no backend server running."
                )

            if act in {"status", "info", "list"}:
                return await site_server.status(data_dir, slug)

            if act in {"logs", "log", "tail"}:
                return f"{slug} backend logs:\n" + await site_server.logs(
                    data_dir, slug, lines
                )

            if act in {"read", "cat"}:
                return await asyncio.to_thread(
                    site_server.read_code, data_dir, slug, path or "app.py"
                )

            if act in {"env", "secrets", "config"}:
                if not env:
                    current = (site_server.get_entry(data_dir, slug) or {}).get("env") or {}
                    return (
                        f"{slug} env: " + (", ".join(sorted(current)) or "none")
                        + "\nValues are never shown. Pass env={...} to replace them."
                    )
                parsed_env = await asyncio.to_thread(site_server.parse_env, env)
                await site_server.start(data_dir, slug, env=parsed_env)
                await self._mark_server(slug, entry, True)
                return (
                    f"Set {len(parsed_env)} env var(s) on {slug} and restarted it: "
                    + ", ".join(sorted(parsed_env))
                )

            if act in {"delete", "remove", "destroy"}:
                await site_server.destroy(data_dir, slug)
                await self._mark_server(slug, entry, False)
                return f"Deleted the backend server for {slug} — code, database, and secrets."

            return (
                f"Error: unknown action '{act}'. Use write, start, stop, restart, "
                "status, logs, read, env, or delete."
            )
        except site_server.SiteServerError as e:
            return f"Error: {e}"

    async def _mark_server(self, slug: str, entry: dict, on: bool) -> None:
        """Record on the site itself that it has a server, for list_sites."""
        updated = dict(entry or {})
        if bool(updated.get("server")) == on:
            return
        updated["server"] = on
        await asyncio.to_thread(self._save_entry, slug, updated)


class DeleteSiteTool(_SiteOwnedTool):
    """Take a published site down."""

    def get_description(self):
        return (
            "Delete a site you published: removes the files, the metadata, and "
            "its backend store, and frees a slot against your site quota. "
            "Params: name (slug). Irreversible — the URL 404s immediately."
        )

    async def execute(self, message: Message, name: str | None = None, **kwargs) -> str:
        slug, entry, site_dir, err = self._resolve(message, name)
        if err:
            return err
        base = Path(self.base_dir).resolve()
        try:
            target = Path(site_dir).resolve()
        except (OSError, ValueError):
            target = None
        if target is not None and (base in target.parents) and target.is_dir():
            await asyncio.to_thread(shutil.rmtree, target, True)
        await asyncio.to_thread(self._save_entry, slug, None)
        with contextlib.suppress(Exception):
            await asyncio.to_thread(
                site_backend.destroy, self.bot.config.DATA_DIR, slug
            )
        # Container, server code, database, and secrets go too.
        with contextlib.suppress(Exception):
            await site_server.destroy(self.bot.config.DATA_DIR, slug)
        return f"Deleted site '{slug}' ({entry.get('title') or 'untitled'}). URL is gone."


class ListSitesTool(Tool):
    """List your published sites, with slug, lifetime, and backend state."""

    def get_description(self):
        return (
            "List the sites you published: slug, URL, title, time left, and "
            "whether each has a backend store. The slug is what edit_site and "
            "delete_site take. No params."
        )

    async def execute(self, message: Message, all_users: bool = False, **kwargs) -> str:
        user_id = str(message.author.id)
        if hasattr(self.bot, "_load_sites"):
            self.bot._load_sites(quiet=True)
        sites = getattr(self.bot, "_sites", {}) or {}

        # If user is admin/owner or explicitly requests all_users, show all sites
        is_admin = False
        if hasattr(self.bot, "_is_admin") and self.bot._is_admin(user_id):
            is_admin = True

        if is_admin or all_users:
            selected_sites = sites
        else:
            selected_sites = {k: v for k, v in sites.items() if str(v.get("user_id", "")) == user_id}

        if not selected_sites:
            return "No active sites found."

        control = (
            getattr(self.bot, "control", {}) or getattr(self.bot, "_control", {}) or {}
        )
        base_url = getattr(
            self.bot.config,
            "MAXWELL_PUBLIC_BASE_URL",
            "https://maxwell.z3ki.dev",
        ).rstrip("/")
        lines = []
        for slug, data in selected_sites.items():
            title = data.get("title", "untitled")
            marks = []
            if data.get("server"):
                marks.append("server")
            elif data.get("backend"):
                marks.append("store")
            owner_label = ""
            if (is_admin or all_users) and data.get("user_id"):
                owner_uid = str(data.get("user_id"))
                owner_label = f" [owner: {owner_uid}]"
            tail = f" [{', '.join(marks)}]" if marks else ""
            lines.append(
                f"  • {slug} — {base_url}/bot/{slug}/ — '{title}' "
                f"({site_expiry_label(data, control)}){owner_label}{tail}"
            )
        header = "All active sites:\n" if (is_admin or all_users) else "Your active sites:\n"
        return header + "\n".join(lines)


_WEB_REPLY_CTX_RE = re.compile(r"\[Latest message replies to[^\]]*\]", re.IGNORECASE)


_WEB_SNIPPET_CHARS = 400


def _sanitize_web_query(query: str | None) -> str:
    """Drop Discord reply-context glue so searches stay on the user's words."""
    q = str(query or "")
    q = _WEB_REPLY_CTX_RE.sub(" ", q)
    q = re.split(r"\n?\[Latest message replies to", q, maxsplit=1, flags=re.IGNORECASE)[
        0
    ]
    q = re.sub(r"\[RESPOND TO THIS\]\s*", "", q, flags=re.IGNORECASE)
    return " ".join(q.split()).strip()[:160]


def _normalize_web_hit(raw: Any) -> dict[str, str]:
    """ddgs engines mix `href`/`url` and `body`/`excerpt`; one shape for us."""
    r = raw if isinstance(raw, dict) else {}
    href = str(r.get("href") or r.get("url") or r.get("link") or "").strip()
    body = str(
        r.get("body") or r.get("excerpt") or r.get("content") or r.get("snippet") or ""
    ).strip()
    title = str(r.get("title") or "No title").strip() or "No title"
    return {"title": title, "href": href, "body": body}


def _format_web_hits(hits: list[dict[str, str]]) -> str:
    lines = []
    for i, r in enumerate(hits, 1):
        title = r.get("title") or "No title"
        href = r.get("href") or ""
        body = (r.get("body") or "")[:_WEB_SNIPPET_CHARS]
        lines.append(f"{i}. {title}\n   {href}\n   {body}".rstrip())
    return "\n\n".join(lines)


class WebSearchTool(Tool):
    """Search the web using DuckDuckGo"""

    def get_description(self):
        return (
            "Search the live web. Default to this when you are unsure, the "
            "topic changes (news, scores, prices, versions, people), or they "
            "asked you to check — do not guess from memory. Skip only pure "
            "banter with nothing to look up. After a hit, fetch_url the page "
            "if you need more than the snippet. Params: query (required), "
            "max_results (optional, default 5, max 10)."
        )

    async def execute(
        self,
        message: Message,
        query: str | None = None,
        max_results: str = "5",
        engine: str | None = None,
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

        backend = str(engine or "auto").strip() or "auto"
        if not re.fullmatch(r"[a-z0-9_.,-]+", backend, flags=re.I):
            backend = "auto"

        # Web search returns untrusted content. Mark the current turn as
        # tainted so subsequent destructive tools (shell, sub_agent) prompt
        # for confirmation. This is the second line of defense against
        # indirect prompt injection from search snippets.
        if self.bot is not None and hasattr(self.bot, "mark_message_tainted"):
            self.bot.mark_message_tainted(message)

        try:
            loop = asyncio.get_running_loop()
            # Bound the search: DDGS uses sync requests internally with a
            # short per-engine wait, so a hung backend would still occupy a
            # default-executor thread. Outer wait_for is the hard cap.
            results = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    lambda: list(
                        ddgs_cls(timeout=20).text(
                            query, max_results=limit, backend=backend
                        )
                    ),
                ),
                timeout=30,
            )

            hits = [_normalize_web_hit(r) for r in (results or [])]
            hits = [h for h in hits if h["href"] or h["body"]]
            if not hits:
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
                        results=list(hits),
                        guild_id=guild_id,
                    )
                    if n:
                        logger.info(
                            f"web_search stored {n} results for query={query!r}"
                        )
            except Exception as e:
                logger.debug(f"web_search RAG persistence skipped: {e}")

            return _format_web_hits(hits)
        except Exception as e:
            logger.error(f"Web search error: {e}")
            err = str(e).strip() or type(e).__name__
            # ddgs raises DDGSException("No results found.") instead of
            # returning []. Treat that as empty, not a tool failure — otherwise
            # the circuit breaker opens and the model learns search is broken.
            if re.search(r"no results", err, re.I):
                return f"No results found for '{query}'"
            return f"Error searching: {e}"


@contextlib.asynccontextmanager
async def _tool_reply_typing(bot, message, content: str = ""):
    """Use the bot's send-time typing helper when present; otherwise no-op."""
    helper = getattr(bot, "_reply_typing", None) if bot is not None else None
    if callable(helper):
        async with helper(getattr(message, "channel", None), content, message=message):
            yield
        return
    yield


_REPLY_HINT_NONE = {"no", "none", "false", "off", "0"}
_REPLY_HINT_THIS = {"this", "here", "current", "latest", "last", "them"}
_REPLY_HINT_PREV = {"previous", "earlier", "before", "prev"}
_REPLY_BOOL_WORDS = {"true", "false", "yes", "no", "on", "off", "0", "1"}


def normalize_reply_hint(value) -> str:
    return " ".join(str(value or "").strip().lower().split())


def score_reply_candidate(hint: str, *, author: str = "", content: str = "") -> int:
    """How well a recent line matches a short quote or name. No ids."""
    hint_n = normalize_reply_hint(hint)
    if not hint_n:
        return 0
    author_n = normalize_reply_hint(author)
    content_n = normalize_reply_hint(content)
    content_n = re.sub(r"^\[at [^\]]+\]\s*", "", content_n)
    if "(" in author_n:
        author_n = author_n.split("(", 1)[0].strip()
    score = 0
    if len(hint_n) <= 3:
        if content_n == hint_n:
            return 100
        if re.search(rf"\b{re.escape(hint_n)}\b", content_n):
            score = 80
        if author_n == hint_n:
            score = max(score, 75)
        return score
    if content_n == hint_n:
        score = 100
    elif content_n.startswith(hint_n):
        score = 85
    elif hint_n in content_n:
        score = 60 + min(20, int(20 * len(hint_n) / max(len(content_n), 1)))
    if author_n == hint_n or author_n.startswith(hint_n + " "):
        score = max(score, 75)
    elif hint_n in author_n:
        score = max(score, 55)
    return score


def _message_author_label(message) -> str:
    author = getattr(message, "author", None)
    if author is None:
        return ""
    return str(
        getattr(author, "display_name", None)
        or getattr(author, "name", None)
        or getattr(author, "id", "")
        or ""
    )


_CHANNEL_HISTORY_TIMEOUT = 2.5
_FETCH_MESSAGE_TIMEOUT = 2.0


async def _iter_recent_channel_messages(message, bot=None, limit: int = 40):
    """Live Discord history only.

    Do not fetch_message() every RAG/memory row. That path 429s Discord and
    stalled send_message for ~50s in busy rooms.
    """
    del bot  # memory fallback is a single fetch in resolve_send_reply_target
    channel = getattr(message, "channel", None)
    history = getattr(channel, "history", None)
    if not callable(history):
        return
    collected: list[Any] = []

    async def _collect():
        async for msg in history(limit=limit):
            collected.append(msg)
            if len(collected) >= limit:
                break

    try:
        await asyncio.wait_for(_collect(), timeout=_CHANNEL_HISTORY_TIMEOUT)
    except Exception:
        pass
    for msg in collected:
        yield msg


async def _fetch_channel_message(channel, message_id):
    fetch = getattr(channel, "fetch_message", None)
    if not callable(fetch) or not message_id:
        return None
    try:
        return await asyncio.wait_for(
            fetch(int(message_id)), timeout=_FETCH_MESSAGE_TIMEOUT
        )
    except Exception:
        return None


async def _memory_reply_candidate(message, hint_n: str, bot=None):
    """Score channel memory in-process, then fetch at most one Discord message."""
    if not hint_n or bot is None:
        return None
    mem = getattr(bot, "memory", None)
    getter = getattr(mem, "get_channel_memory", None) if mem is not None else None
    channel = getattr(message, "channel", None)
    cid = str(getattr(channel, "id", "") or "")
    if not callable(getter) or not cid:
        return None
    try:
        rows = await getter(cid)
    except Exception:
        return None
    trigger_id = str(getattr(message, "id", "") or "")
    best_row = None
    best_score = 0
    for row in reversed(list(rows or [])):
        if not isinstance(row, dict):
            continue
        mid = str(row.get("message_id") or "")
        if not mid or mid == trigger_id:
            continue
        score = score_reply_candidate(
            hint_n,
            author=str(row.get("author") or ""),
            content=str(row.get("content") or ""),
        )
        if score > best_score:
            best_score = score
            best_row = row
    if best_row is None or best_score < 55:
        return None
    return await _fetch_channel_message(channel, best_row.get("message_id"))


async def resolve_send_reply_target(message, reply=True, reply_to=None, bot=None):
    """Pick which Discord message to reply to from a quote or name."""
    hint = reply_to
    use_reply = reply
    if hint is None and isinstance(reply, str):
        raw = str(reply).strip()
        if raw and normalize_reply_hint(raw) not in _REPLY_BOOL_WORDS:
            hint = raw
            use_reply = True
    hint_n = normalize_reply_hint(hint)
    reply_on = parse_bool(use_reply, True)
    if hint_n in _REPLY_HINT_NONE:
        return None
    if not reply_on and not hint_n:
        return None
    if not hint_n or hint_n in _REPLY_HINT_THIS:
        return message

    recent: list[Any] = []
    async for msg in _iter_recent_channel_messages(message, bot=bot):
        recent.append(msg)
    if not recent:
        recent = [message]

    if hint_n in _REPLY_HINT_PREV:
        trigger_id = getattr(message, "id", None)
        for msg in recent:
            if getattr(msg, "id", None) != trigger_id:
                return msg
        return message

    best = None
    best_score = 0
    for msg in recent:
        score = score_reply_candidate(
            hint_n,
            author=_message_author_label(msg),
            content=str(getattr(msg, "content", "") or ""),
        )
        if score > best_score:
            best_score = score
            best = msg
    if best is not None and best_score >= 55:
        return best
    remembered = await _memory_reply_candidate(message, hint_n, bot=bot)
    if remembered is not None:
        return remembered
    return message


class _ShellProgressTurn:
    """One Discord `$ cmd` message for a single user-message turn."""

    __slots__ = ("posted", "parts", "lock", "last_flush_at")

    def __init__(self) -> None:
        self.posted = None
        self.parts: list[str] = []
        self.lock = asyncio.Lock()
        self.last_flush_at = 0.0


def _shell_progress_turn_key(message) -> str:
    """Key shell progress by channel + triggering user message (one turn)."""
    channel_id = str(getattr(getattr(message, "channel", None), "id", "") or "")
    message_id = str(getattr(message, "id", "") or "")
    if not message_id:
        message_id = str(id(message))
    return f"{channel_id}:{message_id}"


def _shell_progress_store(bot, message) -> dict:
    owner = bot if bot is not None else message
    store = getattr(owner, "_shell_progress_by_turn", None)
    if store is None:
        store = {}
        owner._shell_progress_by_turn = store
    return store


def _get_shell_progress_turn(bot, message) -> _ShellProgressTurn:
    store = _shell_progress_store(bot, message)
    key = _shell_progress_turn_key(message)
    sess = store.get(key)
    if sess is None:
        sess = _ShellProgressTurn()
        store[key] = sess
    return sess


def forget_shell_progress(bot, message) -> None:
    """Drop this turn's in-memory shell-progress session.

    The Discord message is left in the channel; a later user message is a
    new turn and posts a fresh shell progress message.
    """
    if message is None:
        return
    key = _shell_progress_turn_key(message)
    for owner in (bot, message):
        if owner is None:
            continue
        store = getattr(owner, "_shell_progress_by_turn", None)
        if store:
            store.pop(key, None)


class SendMessageTool(Tool):
    """Send a reply to the current message with Discord markdown formatting."""

    def get_description(self):
        return (
            "Send a message to the current chat. Default: one call per turn with the full reply. "
            "You can call this more than once if you actually want separate Discord messages; do not split a normal reply. "
            "Content supports Discord markdown: **bold**, *italic*, `code`, ```code blocks```, > quotes, bullet lists. "
            "Params: content (required), reply (optional bool, default true — Discord "
            "quote-reply is on; pass false only for a standalone line with no quote), "
            "reply_to (optional short quote or who said it, like nah or alice — not an id)."
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
        self,
        message: Message,
        content: str | None = None,
        reply: bool = True,
        reply_to: str | None = None,
        channel_id: str | None = None,
        user_id: str | None = None,
        **kwargs,
    ) -> str:
        text = str(content or "").strip()
        if not text:
            return "Error: content is required"
        sent_any = False
        sent_chunks: list[str] = []
        try:
            target_channel = getattr(message, "channel", None)
            raw_dest = channel_id if channel_id is not None else (user_id if user_id is not None else kwargs.get("recipient_id"))
            target_dest = str(raw_dest or "").strip()
            if target_dest and self.bot:
                dest_id = _safe_int(target_dest)
                if dest_id:
                    # Check if it's a channel first
                    ch = self.bot.get_channel(dest_id)
                    if not ch and hasattr(self.bot, "fetch_channel"):
                        try:
                            ch = await self.bot.fetch_channel(dest_id)
                        except Exception:
                            ch = None
                    # If not channel, check if it's a user for DM
                    if not ch:
                        usr = self.bot.get_user(dest_id)
                        if not usr and hasattr(self.bot, "fetch_user"):
                            try:
                                usr = await self.bot.fetch_user(dest_id)
                            except Exception:
                                usr = None
                        if usr:
                            try:
                                ch = usr.dm_channel or await usr.create_dm()
                            except Exception:
                                ch = None
                    if ch:
                        target_channel = ch
                        if target_channel != getattr(message, "channel", None):
                            reply = False

            guild = getattr(target_channel, "guild", None)
            stickers = []
            if self.bot and hasattr(self.bot, "_render_custom_emojis"):
                text = self.bot._render_custom_emojis(text, guild)
            if self.bot and hasattr(self.bot, "_extract_stickers_from_text"):
                text, stickers = self.bot._extract_stickers_from_text(text, guild)

            chunks = self._chunks(text)
            if not chunks and stickers:
                chunks = [""]
            target = None
            if reply and target_channel == getattr(message, "channel", None):
                target = await resolve_send_reply_target(
                    message,
                    reply=reply,
                    reply_to=reply_to if reply_to is not None else kwargs.get("reply_to"),
                    bot=self.bot,
                )
            use_reply = target is not None
            reply_to_message = target if target is not None else message
            # If reply_to_message is a mock/SimpleNamespace without .reply, don't attempt direct .reply()
            if not callable(getattr(reply_to_message, "reply", None)):
                use_reply = False
            send_fn = (
                getattr(self.bot, "_send_with_slowmode", None) if self.bot else None
            )
            async with _tool_reply_typing(self.bot, message, text):
                for i, chunk in enumerate(chunks):
                    chunk_stickers = stickers if i == 0 else None
                    try:
                        extra = {k: v for k, v in kwargs.items() if k not in ("content", "body", "message")}
                        if chunk_stickers:
                            extra["stickers"] = chunk_stickers
                        if callable(send_fn):
                            sent = await send_fn(
                                target_channel,
                                chunk,
                                reply_to=(
                                    reply_to_message
                                    if (i == 0 and use_reply)
                                    else None
                                ),
                                **extra,
                            )
                            if sent is None and not sent_any:
                                return "Error: missing permissions to send message"
                        elif i == 0 and use_reply:
                            try:
                                await reply_to_message.reply(chunk, **extra)
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
                                await target_channel.send(chunk, **extra)
                        else:
                            await target_channel.send(chunk, **extra)
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


class MoreToolsTool(Tool):
    """Unlock the full tool catalog on a turn that started out conversational.

    Ordinary chat turns ship a small tool set (see CHAT_CORE_TOOL_NAMES) so a
    "lol" doesn't drag sixty schemas through the context window. When a turn
    turns out to need something else, this is the door: it marks the turn as
    expanded, returns the whole catalog, and the next turn has every tool
    attached for real. One extra hop, only on the turns that need it.
    """

    def get_description(self):
        return (
            "Unlock your full tool set for this turn. This turn is carrying the "
            "short conversational list; everything else — servers, moderation, "
            "roles/channels, shell, sub_agent, sites, files, email, voice, "
            "avatar/status, memory edits — is one call away. Call this the "
            "moment you want to DO something you can't see a tool for, say what "
            "you need in `need`, and the next turn has all of them."
        )

    async def execute(self, message: Message, need: str | None = None, **kwargs) -> str:
        with contextlib.suppress(Exception):
            message._tools_expanded = True
        bot = self.bot
        names = sorted(
            n
            for n in (getattr(bot, "tools", {}) or {})
            if n != "more_tools"
            and n not in set((getattr(bot, "_control", {}) or {}).get("disabled_tools", []) or [])
        )
        logger.info("more_tools: expanding catalog (need=%r)", str(need or "")[:120])
        return (
            "Full tool set attached for the rest of this turn"
            + (f" (you asked for: {str(need)[:160]})" if need else "")
            + ".\nAvailable now: "
            + ", ".join(names)
            + "\nCall the one you need — the schemas are on your next turn. "
            "Don't call more_tools again."
        )


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


async def _run_docker_cmd(*args: str, timeout: int = 30):
    """Run one `docker` command. Returns ``((stdout, stderr), returncode)``.

    Shared by every sandboxed tool — the shell and the sub-agent both need
    exactly this and had no business each owning a copy.
    """
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


# Image shared by the shell sandbox and the sub-agent sandbox. Built from
# docker/Dockerfile on first use by whichever tool needs it first.
SANDBOX_IMAGE_NAME = "maxwell-shell"
SANDBOX_DOCKERFILE_DIR = os.path.join(os.path.dirname(__file__), "docker")


async def _ensure_sandbox_image(image: str = SANDBOX_IMAGE_NAME) -> None:
    """Build the sandbox image if it is not present. Idempotent."""
    try:
        (_stdout, _stderr), code = await _run_docker_cmd(
            "image", "inspect", image, timeout=15
        )
    except FileNotFoundError as exc:
        raise RuntimeError("docker is not installed or not on PATH") from exc
    except asyncio.TimeoutError as exc:
        raise RuntimeError("docker did not respond") from exc
    if code == 0:
        return
    (_stdout, stderr), build_code = await _run_docker_cmd(
        "build", "-t", image, SANDBOX_DOCKERFILE_DIR, timeout=900
    )
    if build_code != 0:
        raise RuntimeError(
            stderr.decode(errors="replace").strip() or "docker build failed"
        )


def _taint_gate_blocks(tool: Any, message: Any, kwargs: dict) -> bool:
    """True when a destructive call must be refused on an untrusted turn.

    bot.py's dispatcher is the primary enforcement point and injects
    ``_confirmed`` when the user has actually confirmed. Tools keep their own
    check because that dispatcher is not the only caller — the autonomy tick
    invokes ``tool.execute`` directly — but the two must agree on
    ``DISABLE_TAINT_GATE``, or turning the gate off in .env leaves the
    per-tool copy refusing anyway and the switch reads as broken.
    """
    bot = getattr(tool, "bot", None)
    if bot is None or kwargs.get("_confirmed", False):
        return False
    if getattr(getattr(bot, "config", None), "DISABLE_TAINT_GATE", False):
        return False
    checker = getattr(bot, "is_message_tainted", None)
    return bool(checker and checker(message))


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
        return await _run_docker_cmd(*args, timeout=timeout)

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

    _PROGRESS_TICK_SECONDS = 0.8

    async def _run_shell_command(self, command: str, on_progress=None):
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
            stdout_buf = bytearray()
            stderr_buf = bytearray()
            started = time.monotonic()
            last_tick = 0.0

            async def _emit(force: bool = False) -> None:
                nonlocal last_tick
                if on_progress is None:
                    return
                now = time.monotonic()
                if not force and last_tick and now - last_tick < self._PROGRESS_TICK_SECONDS:
                    return
                last_tick = now
                with contextlib.suppress(Exception):
                    await on_progress(
                        bytes(stdout_buf),
                        bytes(stderr_buf),
                        now - started,
                    )

            async def _pump(stream, buf: bytearray) -> None:
                if stream is None:
                    return
                while True:
                    chunk = await stream.read(4096)
                    if not chunk:
                        break
                    buf.extend(chunk)
                    await _emit()

            async def _heartbeat() -> None:
                try:
                    while True:
                        await asyncio.sleep(self._PROGRESS_TICK_SECONDS)
                        if proc.returncode is not None:
                            return
                        await _emit()
                except asyncio.CancelledError:
                    return

            beat = asyncio.create_task(_heartbeat())
            try:
                await _emit(force=True)
                await asyncio.wait_for(
                    asyncio.gather(
                        _pump(proc.stdout, stdout_buf),
                        _pump(proc.stderr, stderr_buf),
                        proc.wait(),
                    ),
                    timeout=self._timeout_seconds(),
                )
                return bytes(stdout_buf), bytes(stderr_buf), proc.returncode
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
                beat.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await beat
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

    def _shell_running_text(
        self, command: str, stdout: bytes, stderr: bytes, elapsed: float
    ) -> str:
        secs = int(elapsed)
        status = "… running" if secs < 1 else f"… running {secs}s"
        out = stdout.decode(errors="replace").strip()
        err = stderr.decode(errors="replace").strip()
        body = out
        if err:
            body = f"{body}\n[stderr] {err}" if body else f"[stderr] {err}"
        if body:
            return self._shell_echo_text(command, status, body)
        return self._shell_echo_text(command, status)

    def _truncate_shell_preview(self, text: str, limit: int) -> str:
        """Keep `$ cmd` / running status plus the newest tail when truncating."""
        notice = "\n... (truncated for channel)"
        if limit <= 0 or len(text) <= limit:
            return text
        keep = max(0, limit - len(notice))
        if keep <= 0:
            return notice[-limit:]
        first_nl = text.find("\n")
        header_end = first_nl if first_nl >= 0 else min(len(text), keep)
        if first_nl >= 0:
            second_nl = text.find("\n", first_nl + 1)
            second = text[first_nl + 1 : second_nl if second_nl >= 0 else len(text)]
            if second.startswith("… "):
                header_end = second_nl if second_nl >= 0 else len(text)
        header = text[:header_end]
        body = text[header_end:]
        if len(header) >= keep:
            return header[:keep] + notice
        room = keep - len(header)
        if len(body) <= room:
            return header + body
        return header + notice + body[-room:]

    def _format_ansi_message(self, text: str) -> str:
        """Format shell status message under Discord's 2000 cap."""
        return str(text or "")[:1990]

    async def _flush_shell_progress_unlocked(
        self, message: Message, sess: _ShellProgressTurn
    ) -> None:
        rendered = "\n\n".join(part for part in sess.parts if part)
        formatted = self._format_ansi_message(rendered)
        if sess.posted is None:
            sess.posted = await message.channel.send(formatted)
            sess.last_flush_at = time.monotonic()
            return
        if getattr(sess.posted, "content", None) == formatted:
            return
        edit = getattr(sess.posted, "edit", None)
        if not callable(edit):
            sess.posted = await message.channel.send(formatted)
            sess.last_flush_at = time.monotonic()
            return
        try:
            await edit(content=formatted)
            sess.last_flush_at = time.monotonic()
        except Exception:
            # Rate-limits / transient Discord errors: keep the existing
            # message and let the next tick retry. Do not post a second dump.
            return

    async def _begin_shell_progress(self, message: Message, text: str):
        sess = _get_shell_progress_turn(self.bot, message)
        async with sess.lock:
            slot = len(sess.parts)
            sess.parts.append(text)
            await self._flush_shell_progress_unlocked(message, sess)
            return sess, slot

    async def _finish_shell_progress(
        self, message: Message, sess: _ShellProgressTurn, slot: int, text: str
    ) -> None:
        async with sess.lock:
            while len(sess.parts) <= slot:
                sess.parts.append("")
            sess.parts[slot] = text
            await self._flush_shell_progress_unlocked(message, sess)

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
        if _taint_gate_blocks(self, message, kwargs):
            preview = normalized[:200] + ("..." if len(normalized) > 200 else "")
            return (
                "Error: shell refused: this turn read content from a fetched "
                "URL/web search that may carry prompt-injection payloads. "
                "The user must confirm out-of-band with `,confirm` "
                "before this can run.\n"
                f"Command preview: {preview}"
            )

        try:
            stdout, stderr, exit_code = await self._run_shell_command(normalized)
        except asyncio.TimeoutError:
            return f"Error: Command timed out after {self._timeout_seconds()}s"
        except Exception as e:
            return f"Error executing command: {e}"

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

        max_out = self._max_output()
        if max_out and len(combined) > max_out:
            combined = combined[:max_out] + "\n... (truncated)"

        # Finish progress with a clean status message, no terminal dump
        await _publish("working on it…")

        result = combined if combined else "(command produced no output)"

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


def _shorten(text, n: int) -> str:
    """Collapse to one line and truncate. Shared by the live-message renderers."""
    t = " ".join(str(text or "").split())
    return t[:n] + ("…" if len(t) > n else "")


class _SubChan:
    """Bidirectional message relay between the main agent and a sub-agent run.

    The sub-agent can push a message up (``message_main``) and the main agent
    can push one down (``sub_agent_message``). The sub-agent's loop injects any
    unseen main -> sub messages at the top of each step so they land in its
    context and it can respond. Kept in-process for the life of the run.
    """

    def __init__(self, run_id: str):
        self.run_id = run_id
        self.msgs: list[dict] = []  # {"src": "main"|"sub", "text": str, "ts": float}
        self.injected: set[int] = set()
        self.target = None  # the channel/dm the run delivers to, set at run start
        self.channel_id = ""  # originating channel, so the main agent can find runs
        self.surfaced: set[int] = set()  # sub->main msgs already shown to Maxwell

    def push(self, src: str, text: str) -> None:
        self.msgs.append({"src": src, "text": str(text or "")[:2000], "ts": time.time()})

    def unseen_main(self) -> list[tuple[int, dict]]:
        out = []
        for i, m in enumerate(self.msgs):
            if m["src"] == "main" and i not in self.injected:
                out.append((i, m))
        return out

    def mark_injected(self, indices) -> None:
        self.injected.update(indices)

    def transcript(self, limit: int = 20) -> list[dict]:
        return list(self.msgs[-limit:])

    def pending_to_main(self) -> list[tuple[int, str]]:
        """Sub->main messages not yet surfaced to the main agent."""
        return [
            (i, m["text"])
            for i, m in enumerate(self.msgs)
            if m["src"] == "sub" and i not in self.surfaced
        ]





class SubAgentMessageTool(Tool):
    """Main agent -> sub-agent. Push a message into a running sub-agent's inbox.

    The message is injected at the top of the sub-agent's next step, so it can
    answer it (or call finish if it changes the task). Returns the conversation
    so far so Maxwell sees the thread. Looks up the live run via the
    SubAgentTool instance registered on the bot.
    """

    def get_description(self) -> str:
        return (
            "Reply to a running sub-agent. Pass the `run_id` it gave you when it "
            "started and `text` to send. Use it to answer a question it raised, "
            "add a requirement, or steer a long job mid-run. Returns the "
            "conversation so far."
        )

    async def execute(self, message: Message, **kwargs) -> str:
        run_id = str(kwargs.get("run_id") or "").strip()
        text = str(kwargs.get("text") or "").strip()
        if not run_id:
            return "error: sub_agent_message needs a `run_id`."
        if not text:
            return "error: sub_agent_message needs `text`."
        sub = getattr(self.bot, "tools", {}).get("sub_agent")
        find_chan = getattr(sub, "_find_chan", None)
        chan = find_chan(run_id) if callable(find_chan) else None
        if chan is None:
            return (
                f"error: no sub-agent with run_id {run_id} (not running and not "
                f"recently finished; it may have ended too long ago)."
            )
        chan.push("main", text)
        thread = "\n".join(
            f"[{m['src']}] {m['text']}" for m in chan.transcript()
        )
        return (
            "Sent to the sub-agent — it'll see it on the next step."
            + ("\n\nConversation so far:\n" + thread if thread else "")
        )


class SubAgentStatusTool(Tool):
    """Main agent -> inspect a sub-agent run's live status, actions and questions.

    Maxwell can't see inside a running sub-agent by default — it only gets the
    final report when the run ends. This is the window into the middle: whether
    it's actually working, what step it's on, what it just did, what it wrote,
    and anything it's waiting on. Complements ``sub_agent_message`` (send it a
    message) and ``message_main`` (it DMs you). ``run_id`` inspects one run;
    omit it to list every live run.
    """

    @staticmethod
    def _event_line(e) -> str:
        t = getattr(e, "type", "")
        data = getattr(e, "data", {}) or {}
        if t == agent_events.EV_STEP:
            return f"step: {data.get('label') or ''}"
        if t == agent_events.EV_TOOL_CALL:
            return f"tool: {data.get('tool') or data.get('name') or data.get('label') or ''}"
        if t == agent_events.EV_TOOL_RESULT:
            return f"result: {(data.get('tail') or data.get('label') or '')[:80]}"
        if t == agent_events.EV_NOTE:
            return f"note: {data.get('label') or ''}"
        if t == agent_events.EV_FINISH:
            return f"finished: {data.get('summary') or ''}"
        if t == agent_events.EV_ERROR:
            return f"error: {data.get('summary') or ''}"
        return f"{t}: {data.get('label') or ''}"

    @staticmethod
    def _lookup(bot):
        """(sub_agent tool, event bus) for this bot, each safely Optional."""
        sub = getattr(bot, "tools", {}).get("sub_agent")
        return sub, agent_events.bus_for(bot)

    def get_description(self) -> str:
        return (
            "Look inside a sub-agent run: is it actually working, what step it's "
            "on, commands it ran, files it wrote, its latest action, and any "
            "question it's waiting on for you. Pass `run_id` to inspect one run, "
            "or omit it to list every live run. Use it to confirm a background "
            "job is making progress (or caught in a loop) before trusting the "
            "report, then steer it with `sub_agent_message(run_id, text)`."
        )

    async def execute(self, message: Message, **kwargs) -> str:
        run_id = str(kwargs.get("run_id") or "").strip()
        sub, bus = self._lookup(self.bot)
        if not run_id:
            return self._list_live(bus)
        return self._describe_one(bus, sub, run_id)

    @staticmethod
    def _list_live(bus) -> str:
        if bus is None:
            return "no sub-agent telemetry on this bot (no event bus)."
        runs = bus.snapshot(include_finished=False)
        if not runs:
            return "no sub-agent is running right now."
        lines = ["Live sub-agent runs:"]
        for d in runs:
            lines.append(
                f"- {d['run_id']} | {d['status']} | step {d['steps']}/"
                f"{d['max_steps'] or '?'} | {d['elapsed_seconds']}s | "
                f"{d['task']}"
            )
        lines.append("For detail pass run_id=<one of the above>.")
        return "\n".join(lines)

    @staticmethod
    def _describe_one(bus, sub, run_id: str) -> str:
        run = bus.get(run_id) if bus else None
        chan = None
        if sub is not None and callable(getattr(sub, "_find_chan", None)):
            chan = sub._find_chan(run_id)
        if run is None and chan is None:
            return (
                f"no sub-agent with run_id {run_id} (not running and not "
                f"recently finished)."
            )
        lines = []
        if run is not None:
            d = run.as_dict()
            lines.append(f"run_id: {run_id}")
            lines.append(f"status: {d['status']}")
            lines.append(f"task: {d['task']}")
            lines.append(f"elapsed: {d['elapsed_seconds']}s")
            lines.append(f"steps: {d['steps']}/{d['max_steps'] or '?'}")
            lines.append(f"commands run: {d['commands_run']}")
            lines.append(
                "files written: " + (", ".join(d["files_written"]) or "none so far")
            )
            lines.append(f"last activity: {d['last_activity'] or '—'}")
            if d.get("summary"):
                lines.append(f"summary: {d['summary'][:200]}")
            recent = [e for e in (run.events or [])][-8:]
            lines.append("recent actions:")
            if recent:
                for e in recent:
                    lines.append("  " + SubAgentStatusTool._event_line(e))
            else:
                lines.append("  (none yet)")
        if chan is not None:
            pending = chan.pending_to_main()
            if pending:
                lines.append("waiting on you (sub-agent asked):")
                for _i, t in pending[-5:]:
                    lines.append(f"  - {t}")
            transcript = chan.transcript(limit=8)
            if transcript:
                lines.append("conversation so far:")
                for m in transcript[-8:]:
                    lines.append(f"  [{m['src']}] {m['text'][:160]}")
        return "\n".join(lines)


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

    # ─── execution sandbox ────────────────────────────────────────────
    #
    # The sub-agent's `run_command` used to be `bash -lc` on the host, in the
    # bot process's own environment and working directory tree. That put the
    # bot's `.env` — Discord token, provider API keys — one `cat` away from
    # anything the model decided to try, and the `shell` tool next door had
    # been in a container for months. Same trust class, same isolation.
    #
    # Each run gets its own container off the shared sandbox image, with the
    # run's workspace bind-mounted and nothing else. State persists across
    # commands within a run (an installed package, a built binary) because it
    # is one long-lived container, and it is torn down when the run ends.
    #
    # `SUBAGENT_SANDBOX=host` opts back out. It is a real choice for someone
    # running the bot in a VM they already treat as disposable, and it is why
    # this refuses rather than silently falling back when Docker is missing:
    # quietly downgrading isolation is how you end up thinking you are
    # sandboxed when you are not.
    SANDBOX_CONTAINER_PREFIX = "maxwell-subagent-"
    SANDBOX_WORKDIR = "/home/maxwell/work"

    @staticmethod
    def _sandbox_mode() -> str:
        """'docker' (default) or 'host'."""
        raw = str(getattr(Config, "SUBAGENT_SANDBOX", "docker") or "docker")
        return "host" if raw.strip().lower() in {"host", "off", "none", "0", "false"} else "docker"

    # Bot tools the sub-agent may call directly on the host via ``bot_call``.
    # Safe, productive tools for sites, files, search, and message lookups.
    # Each still runs under its own ownership / quota checks as the requesting user.
    _BOT_TOOLS = frozenset(
        {
            "create_site",
            "edit_site",
            "delete_site",
            "list_sites",
            "site_server",
            "send_file",
            "web_search",
            "search_messages",
            "fetch_url",
        }
    )

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
        "- You have local command execution in your workdir PLUS access to host "
        "bot tools via `bot_call` (e.g. create_site, edit_site, list_sites, send_file, "
        "web_search, search_messages). When tasked with making or deploying a website, "
        "you can build it and call `bot_call` with `create_site` directly, verify the result, "
        "and return the link.\n"
        "- Decide what you can. If a decision would be risky or genuinely "
        "cannot be made (a missing requirement, a blocker only the operator "
        "can resolve, a choice with real consequences), call `message_main` "
        "to DM Maxwell directly — he sees it on his next turn wherever he is, "
        "and his reply lands here on your next step. No channel spam. Don't "
        "spam it for trivia.\n"
        "- Stay inside the workdir and keep commands short-lived. No "
        "interactive programs, no servers that never exit, no `sudo`.\n"
        "- When the task is done (or genuinely cannot be finished), call "
        "`finish` with a report: what you built, links/URLs created, which files matter, "
        "what you verified, and anything left undone.\n"
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
        {
            "type": "function",
            "function": {
                "name": "message_main",
                "description": (
                    "DM the main agent (Maxwell) privately — no channel post. "
                    "Use it when you genuinely cannot decide: a blocker only the "
                    "operator can resolve, a missing requirement, or a choice "
                    "with real consequences. Maxwell sees it on his next turn and "
                    "can reply; the reply reaches you on your next step. Don't "
                    "spam it for trivia — decide what you can."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "text": {
                            "type": "string",
                            "description": (
                                "What you need. Be specific — a question, a "
                                "decision, or the piece you're missing."
                            ),
                        }
                    },
                    "required": ["text"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "bot_call",
                "description": (
                    "Call a bot tool directly on the host. Available: "
                    + ", ".join(sorted(_BOT_TOOLS))
                    + ". Use it to finish a job that needs a bot/host capability "
                    "— publish a site you built (create_site), edit it "
                    "(edit_site), list sites, or hand a file to the user "
                    "(send_file). Pass `name` and `arguments` as a JSON object "
                    "with exactly that tool's params (name/title/body/files for "
                    "create_site, etc.). It runs as the person who asked, so "
                    "ownership and quota checks apply."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Bot tool name"},
                        "arguments": {
                            "type": "object",
                            "description": "The tool's params as a JSON object.",
                            "additionalProperties": True,
                        },
                    },
                    "required": ["name", "arguments"],
                },
            },
        },
    ]

    def __init__(self, bot):
        super().__init__(bot)
        # Cap how many background (fire-and-forget) sub-agents run at once.
        # Each holds a provider loop and a sandbox container, so this bounds
        # both provider load and how many throwaway containers are alive.
        try:
            limit = max(1, int(getattr(Config, "SUBAGENT_MAX_CONCURRENT", 2) or 2))
        except (TypeError, ValueError):
            limit = 2
        try:
            self._bg_max_queued = max(
                1, int(getattr(Config, "SUBAGENT_MAX_QUEUED", 8) or 8)
            )
        except (TypeError, ValueError):
            self._bg_max_queued = 8
        self._bg_sem = asyncio.Semaphore(limit)
        # Submitted background sub-agents not yet finished (running + queued on
        # the slot). Capped so a flood across many channels can't grow the
        # in-memory queue without bound. Only touched from the event loop.
        self._bg_inflight = 0
        # Live bidirectional channels for running sub-agents (main <-> sub).
        # Keyed by run_id; created in execute().
        self._chans: dict[str, _SubChan] = {}
        # Finished runs kept briefly so sub_agent_message can still find them
        # (Maxwell often replies after a run ends) and so pending notes are
        # readable. {"run_id": (chan, finished_monotonic)}. Pruned lazily.
        self._finished_chans: dict[str, tuple[_SubChan, float]] = {}
        self._chan_grace = float(getattr(Config, "SUBAGENT_CHAN_GRACE_SECONDS", 600) or 600)

    def get_description(self) -> str:
        return (
            "Hand a self-contained task to a sub-agent (another instance of "
            "you) that works it to completion in its own scratch directory and "
            "reports back. This is your DEFAULT engine for any heavy, "
            "multi-step job — building a whole site, writing and debugging a "
            "program/script, a data-crunching or file-conversion task, anything "
            "that takes several build-and-test rounds. Don't grind such a job "
            "through a long inline chain; hand it over and get one report. It "
            "cannot ask questions, so put the full goal, inputs, and how to "
            "verify the result into `task`. `mode=background` (the default for "
            "heavy work) returns immediately and posts the result when the run is "
            "done; `mode=foreground` blocks for the report now. `deliver` controls "
            "where the result lands: `channel` (default) or `dm` to the requester. "
            "Not for a one-liner you could just run with `shell`. `workdir` pins "
            "the scratch dir; `max_steps` caps a runaway job."
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
        if self._sandbox_mode() == "docker":
            return await self._run_command_docker(workspace, command)
        return await self._run_command_host(workspace, command)

    async def _run_command_host(self, workspace: Path, command: str) -> str:
        """Unsandboxed fallback — only when SUBAGENT_SANDBOX=host."""
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

    def _container_name(self, workspace: Path) -> str:
        # The workspace directory name already carries a random suffix, so it
        # is unique per run and makes an orphaned container traceable back to
        # the work it was doing.
        return f"{self.SANDBOX_CONTAINER_PREFIX}{workspace.name}"[:60]

    async def _ensure_sandbox(self, workspace: Path) -> str:
        """Start (or reuse) this run's container. Returns its name."""
        name = self._container_name(workspace)
        (stdout, _stderr), code = await _run_docker_cmd(
            "inspect", "-f", "{{.State.Running}}", name, timeout=10
        )
        if code == 0 and stdout.decode(errors="replace").strip().lower() == "true":
            return name
        if code == 0:
            # Exists but stopped — a previous run of the same workspace.
            await _run_docker_cmd("rm", "-f", name, timeout=15)

        await _ensure_sandbox_image()
        run_args = [
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            "--label",
            "maxwell.subagent=1",
            "--memory",
            "4g",
            "--cpus",
            "2.0",
            "--pids-limit",
            "1024",
            "--tmpfs",
            "/tmp:rw,exec,nosuid,size=256m",
            # The only host path the agent can see is the scratch directory it
            # was given. No bot source, no .env, no host root.
            "-v",
            f"{workspace.resolve()}:{self.SANDBOX_WORKDIR}:rw",
            "-w",
            self.SANDBOX_WORKDIR,
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
            SANDBOX_IMAGE_NAME,
        ]
        (_stdout, stderr), run_code = await _run_docker_cmd(*run_args, timeout=60)
        if run_code != 0:
            raise RuntimeError(
                stderr.decode(errors="replace").strip() or "docker run failed"
            )
        return name

    async def _stop_sandbox(self, workspace: Path) -> None:
        """Tear the run's container down. Best effort — never raises.

        Started with --rm, so a successful stop removes it. A container left
        behind by a crashed bot is reaped by the `inspect`/`rm -f` path in
        `_ensure_sandbox` the next time that workspace name comes round, and
        is findable meanwhile by its `maxwell.subagent` label.
        """
        with contextlib.suppress(Exception):
            await _run_docker_cmd(
                "stop", "-t", "2", self._container_name(workspace), timeout=30
            )

    async def _run_command_docker(self, workspace: Path, command: str) -> str:
        try:
            name = await self._ensure_sandbox(workspace)
        except FileNotFoundError:
            return (
                "error: the sub-agent sandbox needs Docker, and docker is not "
                "installed or not on PATH. Install Docker, or set "
                "SUBAGENT_SANDBOX=host in .env to run commands directly on the "
                "host (no isolation)."
            )
        except Exception as e:
            return f"error: could not start the sandbox container: {e}"

        try:
            (out, err), code = await _run_docker_cmd(
                "exec",
                "-w",
                self.SANDBOX_WORKDIR,
                name,
                "bash",
                "-lc",
                command,
                timeout=Config.SUBAGENT_COMMAND_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            return (
                f"error: command timed out after "
                f"{Config.SUBAGENT_COMMAND_TIMEOUT_SECONDS}s (it was killed)"
            )
        except Exception as e:
            return f"error: could not run the command in the sandbox: {e}"
        text = (out or b"").decode("utf-8", errors="replace")
        errtext = (err or b"").decode("utf-8", errors="replace")
        if errtext:
            text = (text + "\n" + errtext) if text else errtext
        if len(text) > 12000:
            text = text[:12000] + "\n… (output truncated)"
        return f"exit={code}\n{text or '(no output)'}"

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

    async def _dispatch(
        self, workspace: Path, name: str, args: dict, run_id: str = "", message=None
    ) -> str:
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
        if name == "message_main":
            return await self._message_main(run_id, args.get("text", ""))
        if name == "bot_call":
            return await self._bot_call(
                message, args.get("name", ""), args.get("arguments", {})
            )
        return f"error: unknown tool {name!r}"

    async def _bot_call(self, message, name, arguments) -> str:
        """Route a whitelisted bot tool call to the real bot tool on the host.

        The sub-agent is sandboxed for run_command, but it can still finish a
        job that needs a bot/host capability by calling create_site / edit_site /
        send_file etc. here. Each tool runs as the originating user, so its own
        ownership / quota / permission checks apply.
        """
        name = str(name or "").strip()
        if name not in self._BOT_TOOLS:
            return (
                f"error: bot_call '{name}' is not available to sub-agents. "
                f"Available: {', '.join(sorted(self._BOT_TOOLS))}."
            )
        tool = (getattr(self.bot, "tools", None) or {}).get(name)
        if tool is None:
            return f"error: tool '{name}' is not registered on this bot."
        args = arguments
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                return f"error: `arguments` must be a JSON object, got: {args[:120]}"
        if not isinstance(args, dict):
            return "error: `arguments` must be a JSON object."
        if message is None:
            return "error: no message context for bot_call — cannot run bot tools."
        try:
            result = await tool.execute(message, **args)
        except TypeError as e:
            return f"error: bad arguments to {name}: {e}"
        except Exception as e:
            logger.warning("sub-agent bot_call %s failed: %s", name, e)
            return f"error: {name} failed: {e}"
        return str(result)

    async def _message_main(self, run_id: str, text: str) -> str:
        """The sub-agent talks back to the main agent — quietly.

        Never posts to the channel: the message goes onto the run's relay, where
        the main agent picks it up on its next turn in that channel and can reply
        with sub_agent_message. No public chat spam.
        """
        text = str(text or "").strip()
        if not text:
            return "error: empty message - say what you need."
        chan = self._chans.get(run_id)
        if chan is None:
            return "error: this run is no longer active."
        chan.push("sub", text)
        return "Noted — sent to Maxwell (not posted to chat). He'll see it on his " \
            "next turn here and can reply. Keep working; watch the reply on your " \
            "next step."

    # ─── the loop ─────────────────────────────────────────────────────

    def _retain_chan(self, run_id: str) -> None:
        """Keep a finished run's channel briefly so replies still find it."""
        chan = self._chans.pop(run_id, None)
        if chan is None:
            return
        self._finished_chans[run_id] = (chan, time.monotonic())
        self._prune_finished_chans()

    def _prune_finished_chans(self) -> None:
        now = time.monotonic()
        for rid in list(self._finished_chans):
            if now - self._finished_chans[rid][1] > self._chan_grace:
                self._finished_chans.pop(rid, None)

    def _find_chan(self, run_id: str) -> "_SubChan | None":
        """The live or recently-finished channel for a run id."""
        chan = self._chans.get(run_id)
        if chan is not None:
            return chan
        finished = self._finished_chans.get(run_id)
        if finished is not None:
            return finished[0]
        return None

    def drain_notes_for(self, channel_id: str) -> list[str]:
        """Pending sub->main notes, GLOBAL — this is the sub-agent's DM to Maxwell.

        Called by the main agent when it builds a turn. A quiet ``message_main``
        from a sub-agent is a direct message to Maxwell, not a channel post, so it
        must reach him wherever his next turn lands — not only when he happens to
        be in the run's originating channel. ``channel_id`` is kept only for
        backward-compat callers and no longer filters. Marks each returned note as
        surfaced so it is delivered exactly once.
        """
        notes: list[str] = []
        for chan in list(self._chans.values()) + [c for c, _t in self._finished_chans.values()]:
            pending = chan.pending_to_main()
            if not pending:
                continue
            for i, text in pending:
                notes.append(f"[sub-agent {chan.run_id} note] {text}")
                chan.surfaced.add(i)
        return notes

    async def execute(self, message: Message, **kwargs) -> str:
        task = str(kwargs.get("task") or "").strip()
        if not task:
            return "sub_agent needs a `task` describing the work."

        # The bot's LLM is bound as `ai_provider` (see bot.py). `provider` is
        # only the name the test harness and older call sites used — keep the
        # fallback so the tests and any lightweight bot object keep working.
        provider = (
            getattr(self.bot, "ai_provider", None)
            or getattr(self.bot, "provider", None)
        )
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
        # Default the sub-agent to the SAME model the main bot uses, not to an
        # implicit provider default that could drift. Blank SUBAGENT_MODEL = main
        # OLLAMA_MODEL; only set SUBAGENT_MODEL to run sub-agent work on a
        # different (e.g. cheaper/faster) model.
        model = Config.SUBAGENT_MODEL or Config.OLLAMA_MODEL or None

        # Open a run on the event bus so the channel progress message and the
        # dashboard can both watch this happen instead of staring at silence
        # for four minutes. Publishing is fire-and-forget and never raises, so
        # a missing bus (tests, a bot built without one) changes nothing.
        bus = agent_events.bus_for(self.bot)
        author = getattr(message, "author", None)
        run = (
            bus.start_run(
                task,
                requested_by=str(getattr(author, "display_name", "") or ""),
                channel_id=str(getattr(getattr(message, "channel", None), "id", "") or ""),
                workdir=str(workspace),
                max_steps=max_steps,
            )
            if bus
            else None
        )
        run_id = run.run_id if run else ""

        # Live bidirectional channel (main <-> sub) for this run. Created here so
        # both the foreground and background paths can push/pull, and removed in
        # the run's finally so a finished run doesn't leak a channel.
        chan = self._chans.setdefault(run_id, _SubChan(run_id)) if run_id else None
        if chan is not None:
            chan.channel_id = str(
                getattr(getattr(message, "channel", None), "id", "") or ""
            )

        # Fire-and-forget: ``mode=background`` returns immediately and hands the
        # whole run to a background task that posts the result to this channel
        # when done. The model stays responsive — no minutes of silence while
        # the main loop waits on a nested agent. ``mode=foreground`` (the old
        # behaviour, and what the tests exercise) blocks until the report.
        mode = str(kwargs.get("mode") or kwargs.get("background") or "").strip().lower()
        background = mode in {
            "background",
            "bg",
            "async",
            "fire_and_forget",
            "fire-and-forget",
            "fire",
            "true",
            "1",
            "yes",
            "on",
        }
        if background:
            # Refuse rather than grow without bound when a channel flood keeps
            # submitting heavy work faster than runs finish.
            if self._bg_inflight >= self._bg_max_queued:
                return (
                    f"A background sub-agent is already queued/running ("
                    f"{self._bg_inflight}/{self._bg_max_queued}). I won't pile "
                    f"on more right now — say the word and I'll run it once the "
                    f"queue drains, or use mode=foreground to block for it now."
                )
            self._bg_inflight += 1
            deliver = (
                str(kwargs.get("deliver") or kwargs.get("notify") or "channel")
                .strip()
                .lower()
            )
            _spawn_background(
                self._run_background(
                    message,
                    task,
                    workspace,
                    max_steps,
                    model,
                    provider,
                    bus,
                    run_id,
                    deliver,
                    chan,
                )
            )
            return (
                f"Started sub-agent (run {run_id}) on: {self._short(task, 60)}. "
                f"On it — I'll report back when it's done."
            )

        # Mirror the run onto the channel's live progress message so a
        # four-minute run reads as "step 3/24: running: pytest -q" instead of
        # a silent typing indicator. Backgrounded rather than inlined: the
        # agent must not wait on a Discord edit.
        watcher = None
        progress = self._channel_progress(message)
        if bus and run_id and progress is not None:
            watcher = asyncio.create_task(
                self._mirror_events_to_progress(bus, run_id, progress)
            )
        if chan is not None and chan.target is None:
            chan.target = await self._resolve_channel(
                str(getattr(getattr(message, "channel", None), "id", "") or "")
            )
        try:
            report = await self._agent_loop(
                task,
                workspace,
                max_steps=max_steps,
                deadline=deadline,
                model=model,
                provider=provider,
                bus=bus,
                run_id=run_id,
                conv=chan,
                message=message,
            )
            if bus and run_id:
                bus.finish_run(run_id, agent_events.STATUS_DONE, report[:2000])
            return report
        except Exception as e:
            if bus and run_id:
                bus.finish_run(run_id, agent_events.STATUS_FAILED, str(e)[:2000])
            raise
        finally:
            if watcher is not None:
                watcher.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await watcher
            if run_id:
                self._retain_chan(run_id)
            # Always reap the container, including on the timeout, error and
            # cancellation paths — an orphaned sandbox holds 4GB of limit and
            # a bind mount on a workspace nobody is using.
            if self._sandbox_mode() == "docker":
                await self._stop_sandbox(workspace)

    # ─── background (fire-and-forget) runs ───────────────────────────

    @staticmethod
    def _short(text: str, n: int = 120) -> str:
        t = " ".join(str(text or "").split())
        return t[:n] + ("…" if len(t) > n else "")

    async def _resolve_channel(self, channel_id) -> Any:
        """A sendable text channel by id, or None. Falls back to fetch on cold cache."""
        if not channel_id:
            return None
        try:
            cid = int(channel_id)
        except (TypeError, ValueError):
            return None
        bot = self.bot
        with contextlib.suppress(Exception):
            ch = bot.get_channel(cid)
            if ch is not None:
                return ch
        with contextlib.suppress(Exception):
            return await asyncio.wait_for(bot.fetch_channel(cid), timeout=6)
        return None

    async def _resolve_dm(self, user_id) -> Any:
        """A sendable DM channel for a user, or None (DMs closed/blocked, gone)."""
        if not user_id:
            return None
        try:
            uid = int(user_id)
        except (TypeError, ValueError):
            return None
        bot = self.bot
        user = bot.get_user(uid)
        if user is None:
            with contextlib.suppress(Exception):
                user = await asyncio.wait_for(bot.fetch_user(uid), timeout=6)
        if user is None:
            return None
        dm = getattr(user, "dm_channel", None)
        if dm is None:
            with contextlib.suppress(Exception):
                dm = await user.create_dm()
        return dm

    async def _resolve_delivery(self, message, deliver: str) -> Any:
        """Channel or DM to deliver to. Falls back to channel when a DM isn't reachable."""
        if str(deliver).strip().lower() in {"dm", "dm_only", "direct", "dm-only"}:
            author = getattr(message, "author", None)
            dm = await self._resolve_dm(str(getattr(author, "id", "") or ""))
            if dm is not None:
                return dm
            # DM not reachable — fall back to the asking channel so the result
            # is never silently lost to a private channel Maxwell can't open.
            return await self._resolve_channel(
                str(getattr(getattr(message, "channel", None), "id", "") or "")
            )
        return await self._resolve_channel(
            str(getattr(getattr(message, "channel", None), "id", "") or "")
        )

    async def _post_report(self, target, message, task, report):
        """Fallback: post a finished sub-agent's report to the delivery target.

        Only used when there is no relay (no event bus / no run_id) to hand the
        report to Maxwell — e.g. tests, a bot built without telemetry. The real
        background path hands the report to Maxwell (``_handoff_report``) and he
        composes the reply. Threaded back to the triggering message (``reference``)
        when it lands in the same channel — a plain ``reference`` does NOT ping the
        author. Never raises.
        """
        if target is None:
            return
        body = self._report_body(task, report)
        try:
            if message is not None and getattr(target, "id", None) == getattr(
                getattr(message, "channel", None), "id", None
            ):
                await target.send(body, reference=message)
            else:
                await target.send(body)
        except Exception as e:  # noqa: BLE001 - a lost report must not crash the run
            logger.warning("failed to post sub-agent report: %s", e)

    @staticmethod
    def _report_body(task, report):
        report = str(report or "").strip() or "(sub-agent returned nothing)"
        head = _shorten(task, 48) or "sub-agent"
        cap = 1800
        body = f"done: {head}\n\n{report[:cap]}"
        if len(report) > cap:
            body += "\n…(report truncated)"
        return body

    @staticmethod
    def _synthetic_message(chan, author, content):
        """A minimal Message-like object the reply pipeline accepts.

        Mirrors the shape bot._message_from_raw_update builds (id, channel,
        guild, author, content, empty media + mention lists, reference, etc.),
        plus a ``reply()`` shim so the reply threads. ``chan`` MUST be a real
        sendable channel; ``author`` the user the reply is addressed to.
        """
        if author is None:
            author = types.SimpleNamespace(
                id="0", display_name="User", name="User", bot=False
            )
        msg = types.SimpleNamespace(
            id=str(uuid.uuid4().hex[:12]),
            channel=chan,
            guild=getattr(chan, "guild", None),
            author=author,
            content=content,
            embeds=[],
            attachments=[],
            stickers=[],
            mentions=[],
            role_mentions=[],
            mention_everyone=False,
            reference=None,
            components=[],
            poll=None,
            type=0,
        )

        def _reply(reply_content=None, **kwargs):
            return chan.send(content=reply_content, reference=msg, **kwargs)

        setattr(msg, "reply", _reply)
        return msg

    async def _post_subagent_reply(self, target, message, run_id, task, report):
        """Have Maxwell compose and post the user-facing reply right now.

        The background turn already ended after the 'started' ack, so we
        re-enter the bot's reply pipeline with a synthetic message carrying the
        finished report. Maxwell reads it and replies in the run's channel — no
        'report on a later turn' gap. Fully defensive: if re-entry is not
        possible (tests, a bare bot) or raises, fall back to posting the report
        so the result is never lost.
        """
        bot = getattr(self, "bot", None)
        handle = getattr(bot, "_handle_message", None)
        chan = target if target is not None else getattr(getattr(message, "channel", None), "id", None)
        if not callable(handle) or chan is None:
            await self._post_report(target, message, task, report)
            return
        head = _shorten(task, 60) or "sub-agent"
        body = (
            f"The sub-agent (run {run_id}) you asked me to run just finished.\n\n"
            f"TASK: {head}\n"
            f"REPORT:\n{str(report or '').strip()[:1600]}\n\n"
            f"Reply to the person who asked with a clean, natural answer, in this "
            f"channel. Do NOT paste the raw report, the task headline, step counts, "
            f"or workdir path — synthesize it into the answer. Keep it short and plain."
        )
        synthetic = self._synthetic_message(chan, getattr(message, "author", None), body)
        try:
            await handle(synthetic, body)
        except Exception as e:  # noqa: BLE001 - a failed re-entry must not lose the result
            logger.warning("sub-agent immediate reply failed (%s); posting report", e)
            await self._post_report(target, message, task, report)

    async def _handoff_report(self, target, message, chan, run_id, task, report):
        """Hand a finished background sub-agent's result to Maxwell.

        No raw dump to chat: Maxwell composes the user-facing reply. When there
        is no bot reply pipeline to re-enter (tests / no telemetry), fall back to
        posting the report so the result isn't lost.
        """
        if chan is not None or run_id:
            await self._post_subagent_reply(target, message, run_id, task, report)
            return
        await self._post_report(target, message, task, report)


    async def _run_background(
        self,
        message,
        task: str,
        workspace: Path,
        max_steps: int,
        model,
        provider,
        bus,
        run_id: str,
        deliver: str = "channel",
        chan: "_SubChan | None" = None,
    ) -> None:
        """Run a sub-agent to completion in the background, then hand it to Maxwell.

        Never blocks the turn. There is no channel heartbeat and no raw report
        dump: when the run ends, the report is pushed to Maxwell via the run's
        relay (``_handoff_report``) and he surfaces it on his next turn in that
        channel to compose the user-facing reply. Never raises out of here — the
        only thing on the far side of this task is the user's channel, and a
        failure should be a reported message, not an unhandled task exception.

        The time budget starts here, not when the request was queued, so a run
        that waited for a concurrency slot still gets its full budget.
        """
        async with self._bg_sem:
            # Budget relative to the work actually starting, not the request
            # being queued behind a concurrency slot.
            deadline = time.monotonic() + Config.SUBAGENT_TIMEOUT_SECONDS
            target = await self._resolve_delivery(message, deliver)
            if chan is not None:
                chan.target = target
            try:
                report = await self._agent_loop(
                    task,
                    workspace,
                    max_steps=max_steps,
                    deadline=deadline,
                    model=model,
                    provider=provider,
                    bus=bus,
                    run_id=run_id,
                    conv=chan,
                    message=message,
                )
                if bus and run_id:
                    bus.finish_run(run_id, agent_events.STATUS_DONE, report[:2000])
                await self._handoff_report(target, message, chan, run_id, task, report)
            except asyncio.CancelledError:
                if bus and run_id:
                    bus.finish_run(run_id, agent_events.STATUS_FAILED, "cancelled")
                raise
            except Exception as e:  # noqa: BLE001 - report it to the channel
                logger.warning("background sub-agent %s failed: %s", run_id, e)
                if bus and run_id:
                    bus.finish_run(run_id, agent_events.STATUS_FAILED, str(e)[:2000])
                await self._handoff_report(
                    target, message, chan, run_id, task, f"❌ sub-agent failed:\n{e}"
                )
            finally:
                if self._bg_inflight > 0:
                    self._bg_inflight -= 1
                if run_id:
                    self._retain_chan(run_id)
                # Reap the sandbox on every exit path — an orphaned container
                # holds 4GB of limit and a bind mount nobody is using.
                if self._sandbox_mode() == "docker":
                    await self._stop_sandbox(workspace)

    def _channel_progress(self, message):
        """The live progress message for this channel, if a batch owns one."""
        per_chan = getattr(self.bot, "_current_progress_by_channel", None)
        if not isinstance(per_chan, dict):
            return None
        channel = getattr(message, "channel", None)
        return per_chan.get(str(getattr(channel, "id", "") or ""))

    @staticmethod
    async def _mirror_events_to_progress(bus, run_id: str, progress) -> None:
        """Feed run events into the channel progress message until it ends.

        Only the events a human would want to read: which step, and what it is
        doing right now. Tool *results* are deliberately not mirrored — the
        tail of a command's stderr scrolling through a Discord edit is noise,
        and it is in the dashboard's event list for anyone who wants it.
        """
        try:
            async for event in bus.stream(run_id):
                label = str(event.data.get("label") or "")
                if not label or event.type not in (
                    agent_events.EV_STEP,
                    agent_events.EV_TOOL_CALL,
                    agent_events.EV_NOTE,
                ):
                    continue
                with contextlib.suppress(Exception):
                    await progress.update("sub_agent", reasoning=label)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # pragma: no cover - telemetry only
            logger.debug("sub_agent progress mirror stopped: %s", e)

    async def _provider_call(self, provider, messages, model, deadline):
        """Call the sub-agent's provider with retry on transient failure.

        A single dropped provider call used to end a sub-agent run — the loop
        bailed with "stopped: the model call failed". For a self-hosted or
        proxied model that is a routine hiccup (network blip, 5xx, transient
        timeout), not a reason to torch a minutes-long run. Retry a couple of
        times with a short backoff, then give up. Always respects the overall
        deadline so a retry storm can't run past the run's budget.
        """
        retries = int(getattr(Config, "SUBAGENT_PROVIDER_RETRIES", 2) or 0)
        attempt = 0
        while True:
            remaining = int(max(30, deadline - time.monotonic()))
            try:
                return await provider.generate_chat_completion(
                    messages=messages,
                    tools=self._TOOLS,
                    model=model,
                    timeout=remaining,
                )
            except Exception as e:
                attempt += 1
                logger.warning(
                    "sub_agent provider call failed (try %d/%d): %s",
                    attempt,
                    retries + 1,
                    e,
                )
                # Out of budget, or retries exhausted — let the caller decide.
                if attempt > retries or time.monotonic() >= deadline:
                    raise
                backoff = min(1.5 * (2 ** (attempt - 1)), 6.0)
                await asyncio.sleep(backoff)

    async def _agent_loop(
        self,
        task: str,
        workspace: Path,
        *,
        max_steps: int,
        deadline: float,
        model,
        provider,
        bus=None,
        run_id: str = "",
        conv: "_SubChan | None" = None,
        message=None,
    ) -> str:
        """The step loop itself. Returns the report string.

        ``conv`` is the live bidirectional channel to the main agent: any
        ``main`` messages pushed in are injected at the top of the next step so
        the sub-agent sees them and can answer.
        """

        def _emit(event_type: str, **data):
            if bus and run_id:
                bus.publish(run_id, event_type, **data)

        def _note_run(**fields):
            run_obj = bus.get(run_id) if (bus and run_id) else None
            if run_obj is None:
                return
            for key, value in fields.items():
                setattr(run_obj, key, value)

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
        duds = 0
        dud_tolerance = int(getattr(Config, "SUBAGENT_DUD_TOLERANCE", 2) or 0)
        while steps < max_steps:
            # Pull in any main-agent messages that arrived since the last step so
            # the sub-agent can answer them in this turn's reasoning.
            if conv is not None:
                unseen = conv.unseen_main()
                if unseen:
                    indices = [i for i, _m in unseen]
                    for _i, _m in unseen:
                        messages.append(
                            {
                                "role": "user",
                                "content": (
                                    "[Message from Maxwell/main agent] "
                                    + str(_m.get("text") or "")
                                    + "\nAnswer this in your next action, or call "
                                    "finish if it changes the task."
                                ),
                            }
                        )
                    conv.mark_injected(indices)
            if time.monotonic() > deadline:
                _emit(
                    agent_events.EV_NOTE,
                    label=f"time budget exhausted after {steps} step(s)",
                )
                return self._report(
                    task,
                    workspace,
                    steps,
                    commands_run,
                    files_written,
                    f"stopped: hit the {Config.SUBAGENT_TIMEOUT_SECONDS}s time budget",
                )
            steps += 1
            _emit(
                agent_events.EV_STEP,
                step=steps,
                max_steps=max_steps,
                label=f"step {steps}/{max_steps}: thinking",
            )
            _note_run(steps=steps)
            try:
                reply = await self._provider_call(provider, messages, model, deadline)
            except Exception as e:
                logger.warning("sub_agent provider call failed: %s", e)
                _emit(agent_events.EV_ERROR, label="model call failed", error=str(e)[:400])
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
            if not calls and not content:
                # A model that returns neither a tool call nor any text is a
                # dud turn — it stalled, or the endpoint glitched. Rather than
                # end the run with "stopped without a report", nudge it to act
                # a couple of times, then give up. Consumes a step per nudge.
                duds += 1
                _emit(
                    agent_events.EV_NOTE,
                    label=f"empty model reply ({duds}/{dud_tolerance}); nudging",
                )
                if duds > dud_tolerance:
                    return self._report(
                        task,
                        workspace,
                        steps,
                        commands_run,
                        files_written,
                        "the sub-agent stopped without a report (repeated empty replies)",
                    )
                messages.append({"role": "assistant", "content": "", "tool_calls": []})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "<reminder> You returned no action and no text. "
                            "Don't stop: run a command, read/write a file, or "
                            "call `finish` with your report. A bare empty reply "
                            "is not a result.</reminder>"
                        ),
                    }
                )
                continue
            if not calls:
                # No tool call but real content: the model is done talking, or
                # it drifted into prose. Either way its text is the best report
                # we have.
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
                    _emit(agent_events.EV_NOTE, label="agent called finish")
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
                # The label is what a human reads in the progress message, so
                # it carries the actual command / path rather than the tool
                # name — "running: pytest -q" beats "run_command".
                _emit(
                    agent_events.EV_TOOL_CALL,
                    tool=name,
                    step=steps,
                    label=self._call_label(name, args),
                )
                result = await self._dispatch(workspace, name, args, run_id, message)
                _emit(
                    agent_events.EV_TOOL_RESULT,
                    tool=name,
                    step=steps,
                    ok=not result.startswith("error:"),
                    # A tail, not a head: the interesting part of a failing
                    # command is the error at the end, not the banner.
                    preview=result[-400:],
                )
                _note_run(commands_run=commands_run)
                if name == "write_file" and not result.startswith("error:"):
                    written = str(args.get("path") or "").strip()
                    if written and written not in files_written:
                        files_written.append(written)
                        _note_run(files_written=list(files_written))
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

        _emit(agent_events.EV_NOTE, label=f"step budget exhausted ({max_steps})")
        return self._report(
            task,
            workspace,
            steps,
            commands_run,
            files_written,
            f"stopped: used all {max_steps} steps without calling finish",
        )

    @staticmethod
    def _call_label(name: str, args: dict) -> str:
        """One human-readable line for what the agent is about to do."""
        if name == "run_command":
            return "running: " + " ".join(str(args.get("command") or "").split())[:120]
        if name in ("write_file", "read_file"):
            verb = "writing" if name == "write_file" else "reading"
            return f"{verb}: {str(args.get('path') or '')[:120]}"
        if name == "list_files":
            return "listing the workdir"
        return str(name)[:120]

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


_FETCH_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_MAX_FETCH_REDIRECTS = 5
_FETCH_HEADERS = {
    "User-Agent": _IMAGE_FETCH_UA,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "application/json,text/plain;q=0.8,*/*;q=0.5"
    ),
}


async def _fetch_public_url(
    url: str,
    *,
    max_bytes: int,
    timeout: float = 30.0,
) -> tuple[str, str, bytes]:
    """GET a public URL, following a few SSRF-checked redirects.

    Each hop is re-checked with `_is_safe_url`. The shared session's
    `_SafeResolver` also refuses DNS that lands on private/link-local IPs.
    Returns `(final_url, content_type, body)`. Raises ValueError with a
    user-facing message on refusal, HTTP errors, or timeout.
    """
    current = url
    try:
        session = await _get_shared_session()
        for _hop in range(_MAX_FETCH_REDIRECTS + 1):
            if not _is_safe_url(current):
                raise ValueError("Cannot fetch from private/internal URLs")
            async with session.get(
                current,
                timeout=aiohttp.ClientTimeout(total=timeout),
                allow_redirects=False,
                headers=_FETCH_HEADERS,
            ) as resp:
                if resp.status in _FETCH_REDIRECT_STATUSES:
                    loc = resp.headers.get("Location")
                    if not loc:
                        raise ValueError(f"HTTP {resp.status}")
                    current = urljoin(current, loc)
                    continue
                if resp.status != 200:
                    raise ValueError(f"HTTP {resp.status}")
                content_type = resp.headers.get("Content-Type", "") or ""
                raw = await _read_response_limited(resp, max_bytes)
                return current, content_type, raw
        raise ValueError("too many redirects")
    except ValueError:
        raise
    except asyncio.TimeoutError as e:
        raise ValueError(f"timed out fetching {url}") from e
    except Exception as e:
        msg = str(e)
        if "blocked unsafe" in msg.lower():
            raise ValueError("Cannot fetch from private/internal URLs") from e
        raise ValueError(msg) from e


class FetchUrlTool(Tool):
    """Fetch and extract text content from a URL"""

    MAX_CONTENT = 15000
    MAX_BYTES = 1024 * 1024

    def get_description(self):
        return (
            "Fetch a public http(s) URL and return readable text (HTML, JSON, "
            "plain). Use after web_search when a snippet is thin, or whenever "
            "they gave a specific page to read. Not for private/internal URLs. "
            "Images and GIFs (including Tenor/Giphy pages): see_image. "
            "Direct videos: see_video. Audio/video bytes are media, not text. "
            "YouTube: youtube. Params: url (required), max_length (optional, "
            "default 15000)."
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

        # Direct images and GIF-host pages: attach pixels, don't decode binary
        # as text. fetch_url used to return mojibake and the model still
        # couldn't see the picture.
        if SeeImageTool.looks_visual(url) and self.bot is not None:
            visual = await SeeImageTool(self.bot).execute(message, url=url)
            if visual and not str(visual).startswith("Error"):
                return visual
            # A visual URL must never fall through to the text decoder, even
            # when image processing is disabled or the download failed.
            return visual or f"Error: could not load an image from {url}"
        # Direct video links need ffmpeg frame extraction just like uploaded
        # videos. Never let the text fetcher fall through to raw.decode() for
        # an mp4/webm/mov payload.
        if (
            SeeVideoTool.looks_video(url)
            and self.bot is not None
            and hasattr(self.bot, "_download_embed_media")
        ):
            visual = await SeeVideoTool(self.bot).execute(message, url=url)
            if visual and not str(visual).startswith("Error"):
                return visual
            return visual or f"Error: could not load video from {url}"

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
            url, content_type, raw = await _fetch_public_url(
                url, max_bytes=self.MAX_BYTES
            )
        except ValueError as e:
            msg = str(e)
            if msg.startswith("Cannot fetch"):
                return f"Error: {msg}"
            if msg.startswith("HTTP") or msg.startswith("timed out"):
                return f"Error: {msg}"
            return f"Error fetching URL: {msg}"
        except asyncio.TimeoutError:
            return f"Error: timed out fetching {url}"
        except Exception as e:
            return f"Error fetching URL: {e}"

        mime = (content_type or "").split(";", 1)[0].strip().lower()
        if mime.startswith("image/"):
            if self.bot is not None:
                return await SeeImageTool(self.bot).result_from_blob(
                    raw, mime, url, message
                )
            return "Error: URL contains image media, not readable text; use see_image"
        url_ext = Path(urlparse(url).path).suffix.lower()
        if mime.startswith("video/") or url_ext in SeeVideoTool.VIDEO_EXTS:
            if self.bot is not None:
                return await SeeVideoTool(self.bot).result_from_blob(
                    raw, mime or "video/mp4", url, message
                )
            return "Error: URL contains video media, not readable text; use see_video"
        if mime.startswith("audio/") or url_ext in SeeVideoTool.AUDIO_EXTS:
            return (
                "Error: URL contains audio media, not readable text. "
                "Attach or post the audio URL so Maxwell can hear it."
            )

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


class SeeImageTool(Tool):
    """Download an image/GIF URL and attach it as vision on the next turn."""

    def get_description(self):
        return (
            "Look at an image or GIF by URL and attach it to your next turn so "
            "you can actually see it. Use for Tenor/Giphy/imgur GIF pages, "
            "Discord CDN links, or any direct jpg/png/gif/webp that was not "
            "already attached to the message. Prefer this over fetch_url for "
            "pictures. Params: url (required)."
        )

    @classmethod
    def looks_visual(cls, url: str) -> bool:
        return is_gif_page_url(url) or is_direct_image_url(url)

    async def result_from_blob(
        self,
        blob: bytes,
        mime: str,
        url: str,
        message: Message | None = None,
        filename: str = "",
    ) -> str:
        if not blob:
            return "Error: empty image"
        control = getattr(self.bot, "_control", None) or {}
        if not parse_bool(control.get("process_images"), True):
            return "Error: image processing is disabled"
        ext = Path(urlparse(url).path).suffix.lower() or {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/gif": ".gif",
            "image/webp": ".webp",
            "video/mp4": ".mp4",
            "video/webm": ".webm",
        }.get(mime, ".bin")
        filename = filename or f"see-image{ext}"
        max_size = 10 * 1024 * 1024
        if self.bot is not None and hasattr(self.bot, "_max_media_bytes"):
            with contextlib.suppress(Exception):
                max_size = self.bot._max_media_bytes()
        if (
            self.bot is not None
            and hasattr(self.bot, "_normalize_gif")
            and (
                mime in {"image/gif", "video/mp4", "video/webm"}
                or ext in {".gif", ".mp4", ".webm"}
            )
        ):
            normalized = await self.bot._normalize_gif(blob, filename, max_size)
            if normalized:
                blob, mime, filename = normalized
        if not mime.startswith("image/"):
            return (
                f"Error: URL was {mime or 'unknown type'}, not an image I can look at"
            )
        encoded = base64.b64encode(blob).decode("ascii")
        if (
            self.bot is not None
            and message is not None
            and hasattr(self.bot, "_cache_media_context")
            and hasattr(self.bot, "_media_item")
        ):
            with contextlib.suppress(Exception):
                channel_id = str(
                    getattr(getattr(message, "channel", None), "id", "") or ""
                )
                item = self.bot._media_item(
                    b64=encoded,
                    mime_type=mime,
                    filename=filename,
                    is_image=True,
                    message_id=getattr(message, "id", None),
                    source="see_image",
                    url=url,
                )
                if channel_id:
                    self.bot._cache_media_context(channel_id, [item])
        return (
            f"Attached {filename} ({mime}) for visual inspection.\n"
            f"Source: {url}\n"
            f"__IMAGE_B64__{encoded}__END_IMAGE_B64__"
        )

    async def execute(
        self, message: Message, url: str | None = None, **kwargs
    ) -> str:
        if not url:
            return "Error: url is required"
        if not _is_safe_url(url):
            return "Error: Cannot fetch from private/internal URLs"
        control = getattr(self.bot, "_control", None) or {}
        if not parse_bool(control.get("process_images"), True):
            return "Error: image processing is disabled"
        if self.bot is None or not hasattr(self.bot, "_download_embed_media"):
            return "Error: see_image is unavailable"
        max_size = 10 * 1024 * 1024
        if hasattr(self.bot, "_max_media_bytes"):
            with contextlib.suppress(Exception):
                max_size = self.bot._max_media_bytes()
        item = await self.bot._download_embed_media(
            url, "see-image", max_size, getattr(message, "id", None)
        )
        if not item or not item.get("b64"):
            return f"Error: could not load an image from {url}"
        channel_id = str(getattr(getattr(message, "channel", None), "id", "") or "")
        if channel_id and hasattr(self.bot, "_cache_media_context"):
            with contextlib.suppress(Exception):
                self.bot._cache_media_context(channel_id, [item])
        return (
            f"Attached {item.get('filename', 'image')} ({item.get('mime_type')}) "
            f"for visual inspection.\n"
            f"Source: {item.get('url') or url}\n"
            f"__IMAGE_B64__{item['b64']}__END_IMAGE_B64__"
        )


class SeeVideoTool(Tool):
    """Download a video URL and attach ffmpeg-derived frames for vision."""

    VIDEO_EXTS = frozenset({".mp4", ".webm", ".mov", ".mkv", ".avi"})
    AUDIO_EXTS = frozenset({".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac"})

    def get_description(self):
        return (
            "Look at a direct video URL by extracting representative frames "
            "with ffmpeg, and include its audio when audio input is enabled. "
            "Use for mp4/webm/mov links or video embeds. YouTube links belong "
            "to youtube, not this tool. Params: url (required)."
        )

    @classmethod
    def looks_video(cls, url: str) -> bool:
        try:
            parsed = urlparse(str(url or ""))
            if parsed.scheme not in {"http", "https"}:
                return False
            # Keep the dedicated YouTube tool authoritative even if a pasted
            # URL has an unusual path suffix.
            host = (parsed.hostname or "").lower()
            if host in {
                "youtu.be",
                "youtube.com",
                "youtube-nocookie.com",
            } or host.endswith((".youtube.com", ".youtube-nocookie.com")):
                return False
            return Path(parsed.path).suffix.lower() in cls.VIDEO_EXTS
        except Exception:
            return False

    def _audio_enabled(self) -> bool:
        control = getattr(self.bot, "_control", None) or {}
        if isinstance(control, dict) and "process_audio" in control:
            return parse_bool(control.get("process_audio"), False)
        return parse_bool(
            getattr(getattr(self.bot, "config", None), "ENABLE_AUDIO_INPUT", False),
            False,
        )

    def _max_size(self) -> int:
        max_size = 10 * 1024 * 1024
        if self.bot is not None and hasattr(self.bot, "_max_media_bytes"):
            with contextlib.suppress(Exception):
                max_size = self.bot._max_media_bytes()
        return max_size

    async def result_from_blob(
        self,
        blob: bytes,
        mime: str,
        url: str,
        message: Message | None = None,
        filename: str = "",
    ) -> str:
        if not blob:
            return "Error: empty video"
        if self.bot is None or not hasattr(
            self.bot, "_extract_video_derivatives"
        ):
            return "Error: see_video is unavailable"
        control = getattr(self.bot, "_control", None) or {}
        if not parse_bool(
            getattr(getattr(self.bot, "config", None), "ENABLE_VIDEO_INPUT", True),
            True,
        ):
            return "Error: video input is disabled"
        process_images = parse_bool(control.get("process_images"), True)
        if not process_images and not self._audio_enabled():
            return "Error: image and audio processing are disabled"
        max_size = self._max_size()
        if len(blob) > max_size:
            return "Error: video exceeds the configured media size limit"
        name = filename or f"see-video{Path(urlparse(url).path).suffix or '.mp4'}"
        derived = await self.bot._extract_video_derivatives(
            blob,
            name,
            getattr(message, "id", None),
            max_size,
            source_url=url,
            include_frames=process_images,
            source_prefix="see_video",
        )
        if not derived:
            return f"Error: could not extract frames/audio from {url}"
        if self.bot is not None and message is not None:
            channel_id = str(
                getattr(getattr(message, "channel", None), "id", "") or ""
            )
            if channel_id and hasattr(self.bot, "_cache_media_context"):
                with contextlib.suppress(Exception):
                    self.bot._cache_media_context(
                        channel_id,
                        [item for item in derived if item.get("is_image")],
                    )
        lines = [
            f"Extracted {sum(1 for item in derived if item.get('is_image'))} "
            f"video frame(s) from {url}."
        ]
        if any(
            str(item.get("mime_type") or "").startswith("audio/")
            for item in derived
        ):
            lines.append("An audio track was extracted for audio-capable input.")
        for item in derived:
            if item.get("is_image") and item.get("b64"):
                lines.append(
                    f"__IMAGE_B64__{item['b64']}__END_IMAGE_B64__"
                )
            elif (
                str(item.get("mime_type") or "").startswith("audio/")
                and item.get("b64")
            ):
                # Tool follow-up parsing turns this into an input_audio media
                # part; keep the marker out of the user-facing transcript.
                lines.append(
                    f"__AUDIO_B64__{item['b64']}__END_AUDIO_B64__"
                )
        return "\n".join(lines)

    async def execute(
        self, message: Message, url: str | None = None, **kwargs
    ) -> str:
        if not url:
            return "Error: url is required"
        if not _is_safe_url(url):
            return "Error: Cannot fetch from private/internal URLs"
        # The transcript/frame extractor handles YouTube URLs and has access
        # to yt-dlp/cookies; generic ffmpeg fetching must never steal them.
        try:
            from bot_tools import YouTubeTool

            if YouTubeTool._is_youtube_url(url):
                return "Error: use youtube for YouTube links"
        except Exception:
            pass
        control = getattr(self.bot, "_control", None) or {}
        if not parse_bool(control.get("process_images"), True) and not self._audio_enabled():
            return "Error: image and audio processing are disabled"
        if self.bot is None or not hasattr(self.bot, "_download_embed_media"):
            return "Error: see_video is unavailable"
        max_size = self._max_size()
        item = await self.bot._download_embed_media(
            url,
            "see-video" + (Path(urlparse(url).path).suffix or ".mp4"),
            max_size,
            getattr(message, "id", None),
        )
        if not item or not item.get("b64"):
            return f"Error: could not load a video from {url}"
        mime = str(item.get("mime_type") or "")
        if not mime.startswith("video/"):
            return f"Error: URL was {mime or 'unknown type'}, not a video"
        try:
            blob = base64.b64decode(item["b64"], validate=True)
        except (ValueError, TypeError):
            return "Error: downloaded video was invalid"
        return await self.result_from_blob(
            blob,
            mime,
            url,
            message,
            filename=str(item.get("filename") or ""),
        )


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


def _is_voice_channel(ch) -> bool:
    if ch is None:
        return False
    try:
        if isinstance(ch, discord.VoiceChannel):
            return True
        stage = getattr(discord, "StageChannel", None)
        if stage is not None and isinstance(ch, stage):
            return True
    except Exception:
        pass
    return type(ch).__name__ in {"VoiceChannel", "StageChannel"}


def _find_member_voice(bot, user_id: int, prefer_guild=None):
    """Return (member, voice_channel) if that user is in a VC we can see."""
    guilds = []
    if prefer_guild is not None:
        guilds.append(prefer_guild)
    for guild in getattr(bot, "guilds", None) or []:
        if prefer_guild is not None and getattr(guild, "id", None) == getattr(
            prefer_guild, "id", None
        ):
            continue
        guilds.append(guild)
    for guild in guilds:
        member = None
        getter = getattr(guild, "get_member", None)
        if callable(getter):
            member = getter(user_id)
        if member is None:
            continue
        voice = getattr(member, "voice", None)
        channel = getattr(voice, "channel", None) if voice is not None else None
        if channel is not None:
            return member, channel
    return None, None


def _resolve_voice_channel(bot, message, channel_id=None, channel_name=None, user_id=None):
    """Find a VoiceChannel from an id, name, or a user who is already in one."""
    if user_id:
        cleaned = re.sub(r"[^0-9]", "", str(user_id))
        if cleaned:
            _member, channel = _find_member_voice(
                bot, int(cleaned), getattr(message, "guild", None)
            )
            if channel is not None:
                return channel
    cid = re.sub(r"[^0-9]", "", str(channel_id or ""))
    if cid:
        ch = bot.get_channel(int(cid))
        if _is_voice_channel(ch):
            return ch
    name = str(channel_name or "").strip().lstrip("#").lower()
    guild = getattr(message, "guild", None)
    if name and guild is not None:
        for ch in getattr(guild, "voice_channels", []) or []:
            if str(getattr(ch, "name", "")).lower() == name:
                return ch
    return None


def _vc_listen_text_channel(message, guild):
    channel = getattr(message, "channel", None)
    if channel is not None and hasattr(channel, "send"):
        return channel
    if guild is None:
        return None
    text_channels = list(getattr(guild, "text_channels", []) or [])
    return text_channels[0] if text_channels else None


class InboxListTool(Tool):
    """List unread inbox items (friend requests, notices)."""

    def get_description(self):
        return (
            "List unread inbox items: friend requests, new email, and other "
            "notices. No params. Use inbox_act to accept, decline, dismiss, or "
            "mark read. For an email item, email_get_message with the item's "
            "uid gives you the full body."
        )

    async def execute(self, message: Message, **kwargs) -> str:
        store = getattr(self.bot, "inbox", None)
        if store is None:
            return "Error: inbox is not available"
        items = store.actionable(await store.load_items())
        if not items:
            return "Inbox is empty."
        # Same ordering the planner tail uses, but the tool shows more of each
        # item — he asked for the list, so give him the whole thing.
        ordered = store.planner_items(items)
        lines = [f"Inbox ({len(items)} actionable):"]
        for item in ordered[:20]:
            lines.append(store.render_item(item, summary_chars=300))
        if len(items) > len(ordered):
            lines.append(f"… {len(items) - len(ordered)} more not shown")
        return "\n".join(lines)


class InboxActTool(Tool):
    """Accept, decline, or dismiss an inbox item."""

    def get_description(self):
        return (
            "Act on an inbox item. Params: action (required: accept, decline, "
            "dismiss, or read), item_id (inbox id like friend_123 or "
            "email_412) or user_id (the requester's Discord id). accept and "
            "decline are friend requests only; read keeps a notice in the "
            "inbox but stops it being brought to your attention again, "
            "dismiss clears it for good."
        )

    async def execute(
        self,
        message: Message,
        action: str | None = None,
        item_id: str | None = None,
        user_id: str | None = None,
        **kwargs,
    ) -> str:
        from inbox import apply_inbox_action

        return await apply_inbox_action(
            self.bot,
            action=str(action or ""),
            item_id=str(item_id or ""),
            user_id=re.sub(r"[^0-9]", "", str(user_id or "")),
        )


class JoinVcTool(Tool):
    """Join a voice channel, optionally by following a user."""

    def get_description(self):
        return (
            "Join a voice channel. Params: voice_channel_id (Discord snowflake), "
            "channel_name in this server, or user_id to hop into that person's "
            "current VC. Then you can hear and talk."
        )

    async def execute(
        self,
        message: Message,
        voice_channel_id: str | None = None,
        channel_id: str | None = None,
        channel_name: str | None = None,
        user_id: str | None = None,
        **kwargs,
    ) -> str:
        if not getattr(getattr(self.bot, "config", None), "ENABLE_VC", True):
            return "Error: voice is disabled (ENABLE_VC=false)"
        target = _resolve_voice_channel(
            self.bot,
            message,
            voice_channel_id or channel_id,
            channel_name,
            user_id,
        )
        if target is None:
            return (
                "Error: no voice channel found. Pass channel_id, channel_name, "
                "or user_id of someone already in a VC."
            )
        guild = getattr(target, "guild", None) or getattr(message, "guild", None)
        text_channel = _vc_listen_text_channel(message, guild)
        try:
            vc = None
            if hasattr(self.bot, "_vc_get_client"):
                vc = self.bot._vc_get_client(guild, target)
            if vc and vc.is_connected():
                if getattr(getattr(vc, "channel", None), "id", None) != getattr(
                    target, "id", None
                ):
                    await vc.move_to(target)
            else:
                if not hasattr(self.bot, "_vc_connect_channel"):
                    return "Error: voice connect is not available on this bot"
                vc = await self.bot._vc_connect_channel(target)
            listening = False
            if hasattr(self.bot, "_vc_start_listening") and guild is not None:
                listening = await self.bot._vc_start_listening(
                    guild, text_channel, target
                )
            return (
                f"Joined #{getattr(target, 'name', target.id)} "
                f"(listening: {bool(listening)})"
            )
        except Exception as e:
            return f"Error joining voice: {e}"


class VcStatusTool(Tool):
    """Show Maxwell's current voice channel and who else is there."""

    def get_description(self):
        return (
            "Show whether you are in a voice channel and who else is there. "
            "No params. Uses the current server when possible."
        )

    async def execute(self, message: Message, **kwargs) -> str:
        guild = getattr(message, "guild", None)
        clients = list(getattr(self.bot, "voice_clients", None) or [])
        if guild is not None:
            clients = [c for c in clients if getattr(c, "guild", None) == guild] or clients
        vc = next((c for c in clients if getattr(c, "is_connected", lambda: False)()), None)
        if vc is None or not vc.is_connected():
            extra = ""
            if guild is not None:
                occupied = []
                for ch in getattr(guild, "voice_channels", []) or []:
                    members = [
                        getattr(m, "display_name", str(getattr(m, "id", "?")))
                        for m in (getattr(ch, "members", None) or [])
                    ]
                    if members:
                        occupied.append(
                            f"#{getattr(ch, 'name', ch.id)}: {', '.join(members[:8])}"
                        )
                if occupied:
                    extra = "\nOccupied channels:\n- " + "\n- ".join(occupied[:8])
            return "Not connected to a voice channel." + extra
        channel = getattr(vc, "channel", None)
        members = list(getattr(channel, "members", None) or [])
        names = [
            getattr(m, "display_name", str(getattr(m, "id", "?"))) for m in members[:15]
        ]
        listening = False
        if hasattr(self.bot, "_vc_is_listening"):
            listening = bool(self.bot._vc_is_listening(vc))
        return (
            f"Connected to #{getattr(channel, 'name', getattr(channel, 'id', '?'))} "
            f"in {getattr(getattr(channel, 'guild', None), 'name', '?')} "
            f"(listening: {listening})\n"
            f"Members ({len(members)}): {', '.join(names) or '(empty)'}"
        )


class VcWhereTool(Tool):
    """Find which voice channel a user is in."""

    def get_description(self):
        return (
            "Find whether a user is in a voice channel and which one. "
            "Params: user_id (required, numeric id or mention)."
        )

    async def execute(self, message: Message, user_id: str | None = None, **kwargs) -> str:
        cleaned = re.sub(r"[^0-9]", "", str(user_id or ""))
        if not cleaned:
            return "Error: user_id is required"
        member, channel = _find_member_voice(
            self.bot, int(cleaned), getattr(message, "guild", None)
        )
        if channel is None:
            return f"User {cleaned} is not in a voice channel I can see."
        others = [
            getattr(m, "display_name", str(getattr(m, "id", "?")))
            for m in (getattr(channel, "members", None) or [])
            if str(getattr(m, "id", "")) != cleaned
        ][:8]
        extra = f" with {', '.join(others)}" if others else ""
        return (
            f"{getattr(member, 'display_name', cleaned)} is in "
            f"#{getattr(channel, 'name', channel.id)} "
            f"({getattr(getattr(channel, 'guild', None), 'name', '?')}){extra}"
        )


class LeaveVcTool(Tool):
    """Leave the active voice channel"""

    def get_description(self):
        return "Disconnect from the active voice channel in this server."

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
        # UID SEARCH, not SEARCH: sequence numbers are renumbered by any
        # expunge, so an id handed to the model could point at a different
        # message minutes later. UIDs are stable for the life of the mailbox
        # and are the same ids the background mail poller files in the inbox.
        typ, data = M.uid("SEARCH", None, criteria)
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
            typ, msgdata = M.uid("FETCH", mid, "(ENVELOPE)")
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
    """Digits only, so nothing can be smuggled into an IMAP command line.

    An "email_412" inbox item id is accepted and reduced to 412: that is the
    id the model sees in its inbox, and making it retype the numeric half was
    a trap with no upside.
    """
    s = str(message_id or "").strip()
    if s.startswith("email_"):
        s = s[len("email_") :]
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
        return "Error: message_id must be a numeric IMAP id"
    M = _imap_connect_sync(host, port, user, password)
    try:
        M.select("INBOX")
        # UID first — that is what the list/search tools and the inbox notices
        # hand out. Fall back to a sequence-number fetch so ids the model
        # cached from an older run still resolve instead of hard-failing.
        typ, data = M.uid("FETCH", seq, "(RFC822)")
        if typ != "OK" or not data or not data[0]:
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
        typ, data = M.uid("SEARCH", None, f'TEXT "{safe}"')
        if typ != "OK" or not data or not data[0]:
            return f"No messages matched: {query!r}"
        ids = data[0].split()[-limit:]
        if not ids:
            return f"No messages matched: {query!r}"

        # ENVELOPE for each so the model has subject/from without a second
        # round-trip. Same shape as in the list tool above.
        lines = [f"Search results for {query!r} ({len(ids)} match(es)):"]
        for mid in ids:
            typ, msgdata = M.uid("FETCH", mid, "(ENVELOPE)")
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
        if _taint_gate_blocks(self, message, kwargs):
            preview = str(body)[:200] + ("..." if len(str(body)) > 200 else "")
            return (
                "Error: email_send refused: this turn read content from a "
                "fetched URL/web search that may carry prompt-injection "
                "payloads. The user must confirm out-of-band with `,confirm` "
                "before this can run.\n"
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
            "Fetch one email by id (from email_read_inbox, email_search, or "
            "an inbox email notice — both 412 and email_412 work). "
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
# X (Twitter). Reading is free and needs no account; posting uses the
# session cookies of a browser logged in as him. Both live in x_client.py —
# these two tools are the model-facing surface and nothing more.
# ---------------------------------------------------------------------------


def _x_client(bot):
    """The bot's live XClient, or None when the feature is off."""
    return getattr(bot, "x_client", None)


def _x_unavailable() -> str:
    return (
        "Error: X is not available on this install (ENABLE_X=false, or "
        "x_client failed to start). Check `python3 doctor.py`."
    )


class XReadTool(Tool):
    """Read X: a timeline, a search, an account, or one post."""

    def get_description(self) -> str:
        return (
            "Read X/Twitter. Params: action (home, user, search, mentions, "
            "tweet), handle (for action=user), query (for action=search — X "
            "search operators work: 'from:nasa', '-filter:replies', "
            "'min_faves:100'), tweet_id or a post URL (for action=tweet), "
            "limit (default 15, max 50). home and mentions need the logged-in "
            "session; user, search and tweet work without one. Returns the "
            "posts with their ids, so you can reply to or quote one with "
            "x_post."
        )

    async def execute(
        self,
        message: Message,
        action: str = "home",
        handle: str | None = None,
        query: str | None = None,
        tweet_id: str | None = None,
        limit: str | int = 15,
        **kwargs,
    ) -> str:
        client = _x_client(self.bot)
        if client is None:
            return _x_unavailable()
        from x_client import XError, render_tweets

        act = str(action or "home").strip().lower()
        # The model reaches for the verb it means rather than the enum, and a
        # rejected call costs a whole turn. Map the obvious synonyms instead.
        act = {
            "timeline": "home",
            "feed": "home",
            "profile": "user",
            "account": "user",
            "mention": "mentions",
            "notifications": "mentions",
            "status": "tweet",
            "post": "tweet",
            "get": "tweet",
        }.get(act, act)
        # A handle in the query slot and a query in the handle slot are both
        # common; so is passing a URL as the handle.
        if act == "user" and not handle and query:
            handle = query
        if act == "search" and not query and handle:
            query = handle
        if act == "tweet" and not tweet_id and (query or handle):
            tweet_id = query or handle
        try:
            count = max(1, min(int(limit), 50))
        except (TypeError, ValueError):
            count = 15

        try:
            tweets = await client.read(
                act, handle=handle, query=query, tweet_id=tweet_id, limit=count
            )
        except XError as e:
            return f"Error: {e}"
        except Exception as e:  # pragma: no cover - defensive
            return f"Error: X read failed: {type(e).__name__}: {e}"

        header = {
            "home": "X — home timeline",
            "user": f"X — @{str(handle or '').lstrip('@')}",
            "search": f"X — search: {query}",
            "mentions": "X — mentions of you",
            "tweet": "X — one post",
        }.get(act, "X")
        # Same posture as fetch_url/web_search: this is arbitrary text written
        # by strangers, so the turn is tainted and destructive tools need an
        # out-of-band confirm before they run.
        if self.bot is not None:
            self.bot.mark_message_tainted(message)
        return render_tweets(tweets, header=f"{header} ({len(tweets)}):")


class XPostTool(Tool):
    """Post, reply, quote, delete, like, or repost on X."""

    # A public post is the least reversible thing he can do with a tool, and
    # the obvious target of anything injected through a fetched page. On a
    # tainted turn the user confirms first.
    is_destructive: bool = True

    def get_description(self) -> str:
        return (
            "Post on X/Twitter as yourself. Params: action (post, reply, "
            "quote, delete, like, repost — default post), text (the post; "
            "required for post/reply/quote), reply_to or tweet_id (the post "
            "id or URL you are answering/quoting/liking/deleting). Posts are "
            "public and permanent-ish: say something worth saying. There is "
            "an hourly budget, so do not narrate every thought."
        )

    async def execute(
        self,
        message: Message,
        action: str = "post",
        text: str | None = None,
        reply_to: str | None = None,
        quote: str | None = None,
        tweet_id: str | None = None,
        **kwargs,
    ) -> str:
        client = _x_client(self.bot)
        if client is None:
            return _x_unavailable()
        from x_client import XError

        act = str(action or "post").strip().lower()
        act = {
            "tweet": "post",
            "send": "post",
            "publish": "post",
            "retweet": "repost",
            "favorite": "like",
            "fav": "like",
            "remove": "delete",
        }.get(act, act)
        if act not in {"post", "reply", "quote", "delete", "like", "repost"}:
            return (
                f"Error: unknown action {act!r}. Use post, reply, quote, "
                "delete, like, or repost."
            )

        # Indirect-prompt-injection gate, same contract as email_send/shell:
        # a turn that read a web page or a search result cannot publish
        # without the user confirming out of band.
        if _taint_gate_blocks(self, message, kwargs):
            preview = str(text or tweet_id or reply_to or "")[:200]
            return (
                "Error: x_post refused: this turn read content from the web "
                "(a page, a search, or X itself) that may carry "
                "prompt-injection payloads, and posting is public. The user "
                "must confirm out-of-band with `,confirm`.\n"
                f"Action: {act}\nContent: {preview}"
            )

        try:
            if act in {"post", "reply", "quote"}:
                target = reply_to or (tweet_id if act == "reply" else None)
                quoted = quote or (tweet_id if act == "quote" else None)
                result = await client.post(
                    str(text or ""), reply_to=target, quote=quoted
                )
                label = {"post": "Posted", "reply": "Replied", "quote": "Quoted"}[act]
                return f"{label} on X: {result.get('url') or result.get('id') or 'ok'}"
            result = await client.act(act, str(tweet_id or reply_to or quote or ""))
        except XError as e:
            return f"Error: {e}"
        except Exception as e:  # pragma: no cover - defensive
            return f"Error: X {act} failed: {type(e).__name__}: {e}"
        done = {"delete": "Deleted", "like": "Liked", "repost": "Reposted"}[act]
        return f"{done} on X: {result.get('url') or result.get('id') or 'ok'}"


# ---------------------------------------------------------------------------
# Self-modification tools. These let Maxwell rewrite its own base
# personality + per-server prompts at runtime. The runtime load is hot —
# _load_control() reads mtime, so a write to bot_control.json is picked up
# on the next prompt assembly without a restart. server prompts are read
# on every prompt build, also hot.
# ---------------------------------------------------------------------------


class UpdateBasePersonalityTool(Tool):
    """Rewrite the global base_personality that ships in every prompt.

    This is what the model reads as its tone / do-don'ts / identity safety
    section. The MAXWELL_BASE_KNOWLEDGE block (identity, slang, voice, memes)
    is ALWAYS-ON and lives in code — it is NOT editable through this tool.
    This tool only rewrites the per-runtime personality paragraph that lives
    in bot_control.json under `base_personality`.
    """

    is_destructive: bool = True

    def get_description(self) -> str:
        return (
            "Rewrite global base_personality (tone/do-don'ts in every prompt). "
            "Base Knowledge in code is not editable. Params: text (100-2000 chars)."
        )

    async def execute(
        self,
        message: Message,
        text: str | None = None,
        **kwargs,
    ) -> str:
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

    Same effect as the `,prompt <text>` command but invokable from
    inside an LLM turn — Maxwell can edit its own per-server instructions
    when it has a reason. Pass server_id (numeric snowflake) or pass 'DM'
    for the DM default. Pass empty text to clear the per-server prompt.
    """

    is_destructive: bool = True

    def get_description(self) -> str:
        return (
            "Rewrite or clear the per-server custom prompt (same as `,prompt`). "
            "Params: server_id (snowflake or 'DM'), text (empty or '__CLEAR__' "
            "to clear)."
        )

    async def execute(
        self,
        message: Message,
        server_id: str | None = None,
        text: str | None = None,
        **kwargs,
    ) -> str:
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


# --------------------------------------------------------------------------- #
# Chess
#
# The chess_* tools let Maxwell play a real game against whoever started it in
# a channel. Exactly one active game per channel; only the player who started
# it can move. Maxwell "sees" the board two ways: the tool result carries the
# board as ASCII + FEN + legal moves, and the posted PNG is returned as base64
# (__IMAGE_B64__) so the vision path attaches it to the next model turn. The
# same PNG is posted to the channel so the player sees it too.
# --------------------------------------------------------------------------- #


def _chess_color_name(color) -> str:
    return "white" if _chess and color == _chess.WHITE else "black"


def _chess_render_safe(game) -> bytes | None:
    """Render the board PNG, or None if Pillow/chess rendering is unavailable.

    The board is oriented for the human player: if they're on black, black sits
    at the bottom so the image reads like their own board, not the standard
    white-at-the-bottom view. Callers degrade to a text-only result rather than
    failing the whole tool.
    """
    try:
        perspective = (
            "black"
            if _chess and getattr(game, "player_color", None) == _chess.BLACK
            else "white"
        )
        return _chess_render_board_png(game.board, perspective=perspective)
    except Exception as exc:  # pragma: no cover - non-fatal
        logger.warning("chess board render failed: %s", exc)
        return None


def _chess_state_text(game) -> str:
    """The board + metadata the model needs to play, as plain text."""
    lines: list[str] = []
    lines.append("CHESS BOARD (text — see attached image for the real board):")
    lines.append(_chess_board_ascii(game.board))
    lines.append("")
    lines.append(f"FEN: {game.fen}")
    move_hist = " ".join(game.history_san) or "none"
    lines.append(f"Move history (SAN): {move_hist}")
    lines.append(
        f"Maxwell={_chess_color_name(game.bot_color)} · "
        f"{game.player_name}={_chess_color_name(game.player_color)}"
    )
    result = game.result
    if result:
        lines.append(f"GAME OVER: {result}")
    else:
        legal = game.legal_san
        lines.append(f"To move: {game.turn_label}")
        shown = ", ".join(legal[:48])
        if len(legal) > 48:
            shown += f" … (+{len(legal) - 48} more)"
        lines.append(f"Legal moves ({len(legal)}): {shown}")
        who = "Maxwell" if game.bot_turn else game.player_name
        lines.append(f"It is {who}'s move.")
    return "\n".join(lines)


def _chess_save_png(bot, game) -> str | None:
    """Best-effort write of the board PNG to data/exports/chess for reuse."""
    try:
        png = _chess_render_safe(game)
        if not png:
            return None
        data_dir = os.path.abspath(
            getattr(getattr(bot, "config", None), "DATA_DIR", "data") or "data"
        )
        out_dir = os.path.join(data_dir, "exports", "chess")
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, f"{game.game_id}.png")
        with open(path, "wb") as fh:
            fh.write(png)
        return path
    except Exception as exc:  # pragma: no cover - non-fatal
        logger.warning("Could not persist chess board png: %s", exc)
        return None


async def _chess_post_board(bot, message, game) -> tuple[str, str, bytes | None]:
    """Render, send the PNG to the channel, return ``(cdn_url, local_path, png)``."""
    png = _chess_render_safe(game)
    local_path = _chess_save_png(bot, game)
    cdn_url = ""
    if not png:
        return cdn_url, local_path, None
    try:
        file = File(BytesIO(png), filename=f"chess-{game.game_id}.png")
        sent = await message.channel.send(file=file)
        if sent is not None and getattr(sent, "attachments", None):
            cdn_url = sent.attachments[0].url
    except discord.Forbidden:
        logger.warning("Cannot post chess board in %s — missing permissions", getattr(message.channel, "id", "?"))
    except discord.HTTPException as exc:
        logger.warning("Failed to post chess board: %s", exc)
    return cdn_url, local_path, png


def _chess_append_image(result: str, png: bytes) -> str:
    if not png:
        return result
    b64 = base64.b64encode(png).decode("ascii")
    return result + f"\n__IMAGE_B64__{b64}__END_IMAGE_B64__\n"


async def _chess_record(bot, message, text: str) -> None:
    """Record a small chess line to channel memory so the model keeps continuity."""
    try:
        mem = getattr(bot, "memory", None)
        if mem is not None and hasattr(mem, "add_to_channel_memory"):
            await mem.add_to_channel_memory(
                str(getattr(message.channel, "id", "") or ""),
                {
                    "author": "Tool",
                    "content": text,
                    "is_tool": True,
                },
            )
    except Exception:  # pragma: no cover
        pass


def _chess_game_result(game, *, posted: bool, cdn_url: str = "", local_path: str = "", png: bytes | None = None) -> str:
    text = _chess_state_text(game)
    extra: list[str] = []
    if posted:
        extra.append("Board image posted to the channel.")
    if cdn_url:
        extra.append(f"Board image URL: {cdn_url}")
    if local_path:
        extra.append(f"Board image local path: {local_path}")
    if extra:
        text += "\n" + "\n".join(extra)
    if png:
        text = _chess_append_image(text, png)
    return text


class ChessStartTool(Tool):
    """Start a new chess game in this channel with the invoking player."""

    def get_description(self):
        return (
            "Start a chess game in this channel against the player who asked. "
            "One game per channel; once started, only that player may move. "
            "Posts the starting board image and returns the full board state, "
            "FEN, and legal moves. Params: bot_side (white|black|auto, default "
            "white), depth (search depth 1-4, default 3). If Maxwell is white it "
            "plays its first move automatically and then it is the player's turn."
        )

    async def execute(
        self,
        message: Message,
        bot_side: str | None = "white",
        depth: int | None = None,
        **kwargs,
    ) -> str:
        if not __CHESS_IMPORTED__:
            return "Error: chess is not available (python-chess is missing)."
        channel_id = str(getattr(message.channel, "id", "") or "")
        author_id = str(getattr(message.author, "id", "") or "")
        author_name = str(getattr(message.author, "display_name", "") or "") or str(
            getattr(message.author, "name", "") or "player"
        )
        if not channel_id:
            return "Error: no channel context."

        manager = _chess_get_manager()
        existing = manager.active(channel_id)
        if existing is not None:
            return (
                f"Error: a chess game is already active in this channel. "
                f"It is between Maxwell and {existing.player_name}. "
                f"Use chess_state to see it, or chess_resign to end it first."
            )

        side = str(bot_side or "auto").strip().lower()
        if side in ("auto", "random"):
            bot_color = None
        elif side == "white":
            bot_color = _chess.WHITE if hasattr(_chess, "WHITE") else True
        elif side == "black":
            bot_color = _chess.BLACK if hasattr(_chess, "BLACK") else False
        else:
            return "Error: bot_side must be 'white', 'black', or 'auto'."

        max_depth = int(depth or 3)
        if max_depth < 1:
            max_depth = 1
        if max_depth > 4:
            max_depth = 4

        game = manager.start(
            channel_id,
            author_id,
            author_name,
            bot_color=bot_color,
            max_depth=max_depth,
            jitter=0.35,
        )

        # If Maxwell is white it opens; otherwise the player moves first.
        played: list[str] = []
        if game.bot_turn and not game.is_over:
            try:
                mv, san = _chess_choose_bot_move(game.board, depth=game.max_depth, jitter=game.jitter)
                game.apply_move(mv)
                played.append(san)
            except Exception as exc:  # pragma: no cover
                logger.warning("chess_start engine move failed: %s", exc)
            manager.persist()

        self._signal_streaming(message)
        cdn_url, local_path, png = await _chess_post_board(self.bot, message, game)
        manager.persist()
        await _chess_record(
            self.bot,
            message,
            f"started a chess game. Maxwell is "
            f"{_chess_color_name(game.bot_color)}, "
            f"{game.player_name} is {_chess_color_name(game.player_color)}."
            + (f" Maxwell opened with {played[-1]}." if played else ""),
        )

        result = _chess_game_result(game, posted=True, cdn_url=cdn_url, local_path=local_path, png=png)
        if played:
            result += (
                "\n\nGame started. Maxwell ("
                + _chess_color_name(game.bot_color)
                + ") opened with '"
                + played[-1]
                + "'. Tell "
                + game.player_name
                + " it is their move and prompt them to play."
            )
        elif game.bot_turn and not game.is_over:
            result += (
                "\n\nGame started. It is Maxwell's move — call chess_move"
                " (or pass move=) to play."
            )
        else:
            result += (
                "\n\nGame started. It is "
                + game.player_name
                + "'s move — prompt them to play."
            )
        result += " Use chess_move to advance the game."
        return result


class ChessStateTool(Tool):
    """Show the current chess board, FEN, and legal moves (no board change)."""

    def get_description(self):
        return (
            "Get the current chess board state (text board, FEN, legal moves, "
            "whose move it is) for the active game in this channel. Does NOT "
            "change the board or post an image; use it to re-sync when you lose "
            "track of the position. Only the player who started the game may "
            "call it."
        )

    async def execute(self, message: Message, **kwargs) -> str:
        if not __CHESS_IMPORTED__:
            return "Error: chess is not available (python-chess is missing)."
        channel_id = str(getattr(message.channel, "id", "") or "")
        author_id = str(getattr(message.author, "id", "") or "")
        manager = _chess_get_manager()
        try:
            game = manager.game_for(channel_id, author_id)
        except ValueError as exc:
            return f"Error: {exc}"
        except PermissionError as exc:
            return f"Error: {exc}"
        png = _chess_render_safe(game)
        return _chess_game_result(game, posted=False, png=png)


class ChessMoveTool(Tool):
    """Play a chess move: the player's move or Maxwell's own move."""

    def get_description(self):
        return (
            "Advance the chess game by one move (or a full round). Pass move= "
            "in SAN (e4, Nf3, O-O, exd5, Qh5) or UCI (e2e4, e7e8q). If it is "
            "the player's turn, this relays their move; if it is Maxwell's turn "
            "and move is omitted, Maxwell picks a move itself. respond=true "
            "(default) makes Maxwell reply automatically after a player move. "
            "Posts the updated board image and returns FEN + legal moves. Only "
            "the player who started the game may call it."
        )

    async def execute(
        self,
        message: Message,
        move: str | None = None,
        respond: bool = True,
        **kwargs,
    ) -> str:
        if not __CHESS_IMPORTED__:
            return "Error: chess is not available (python-chess is missing)."
        channel_id = str(getattr(message.channel, "id", "") or "")
        author_id = str(getattr(message.author, "id", "") or "")
        manager = _chess_get_manager()
        try:
            game = manager.game_for(channel_id, author_id)
        except ValueError as exc:
            return f"Error: {exc}"
        except PermissionError as exc:
            return f"Error: {exc}"

        played: list[str] = []
        error_text: str | None = None
        try:
            if game.bot_turn:
                # Maxwell to move. Either the model supplies its chosen move,
                # or the engine picks one.
                if move:
                    mv = game.parse_move(move)
                    played.append(game.apply_move(mv))
                else:
                    mv, san = _chess_choose_bot_move(game.board, depth=game.max_depth, jitter=game.jitter)
                    game.apply_move(mv)
                    played.append(san)
            else:
                # Player to move. They must supply a move; it must be legal for
                # the side to move (parse_move enforces that).
                if not move:
                    return (
                        f"Error: it is {game.player_name}'s move. "
                        "Pass move= (e.g. move=...'e4') with the move they just "
                        "played. Leave move unset and it stays on the player."
                    )
                mv = game.parse_move(move)
                played.append(game.apply_move(mv))
        except ValueError as exc:
            error_text = str(exc)
        except Exception as exc:
            error_text = f"could not apply move: {exc}"

        if error_text:
            return f"Error: {error_text}"

        # If the human just moved and it is now Maxwell's turn, respond.
        if respond and game.bot_turn and not game.is_over:
            try:
                mv2, san2 = _chess_choose_bot_move(game.board, depth=game.max_depth, jitter=game.jitter)
                game.apply_move(mv2)
                played.append(san2)
            except Exception as exc:  # pragma: no cover
                logger.warning("chess_move engine reply failed: %s", exc)

        manager.persist()
        self._signal_streaming(message)
        cdn_url, local_path, png = await _chess_post_board(self.bot, message, game)
        manager.persist()
        await _chess_record(
            self.bot,
            message,
            "chess move(s): " + " ".join(played) + f" · fen {game.fen.split()[0]}",
        )

        result = _chess_game_result(game, posted=True, cdn_url=cdn_url, local_path=local_path, png=png)
        result += (
            "\n\n" + "Played move(s): " + " ".join(played)
            + ("\nThe game is over." if game.is_over else
               ("\nIt is now the player's move — tell them it is their turn."
                if not game.bot_turn else
                "\nIt is Maxwell's move — play it or pass the next call."))
        )
        return result


class ChessResignTool(Tool):
    """End the current chess game."""

    def get_description(self):
        return (
            "End the active chess game in this channel. Pass side=maxwell to "
            "have Maxwell resign, side=player to record the player resigning, or "
            "leave it default for a mutual end. Returns the final board and "
            "result. Only the player who started the game may call it."
        )

    async def execute(
        self,
        message: Message,
        side: str | None = None,
        **kwargs,
    ) -> str:
        if not __CHESS_IMPORTED__:
            return "Error: chess is not available (python-chess is missing)."
        channel_id = str(getattr(message.channel, "id", "") or "")
        author_id = str(getattr(message.author, "id", "") or "")
        manager = _chess_get_manager()
        try:
            game = manager.game_for(channel_id, author_id)
        except ValueError as exc:
            return f"Error: {exc}"
        except PermissionError as exc:
            return f"Error: {exc}"

        who = str(side or "player").strip().lower()
        if who in ("player", "user", "human"):
            winner = _chess_color_name(game.bot_color)
        elif who in ("maxwell", "bot", "max"):
            winner = _chess_color_name(game.player_color)
        else:
            winner = None
        manager.remove(channel_id)
        final = _chess_state_text(game)
        png = _chess_render_safe(game)
        if winner:
            final += f"\nGAME OVER: {winner} wins by resignation."
        else:
            final += "\nGAME OVER: the game was ended."
        final = _chess_append_image(final, png)
        await _chess_record(self.bot, message, f"chess game ended ({who} resigned).")
        return final + "\n\nGame ended. Use chess_start to begin a new one."


class UsageTool(Tool):
    """Query the usage/quota endpoint (z3ki.dev/v2/usage) with the API key in env."""

    def get_description(self):
        return (
            "Fetch current API usage and remaining quota from the provider "
            "(z3ki.dev/v2/usage) using the API key already configured in env. "
            "Returns usage percentages, reset times, and account counts so you "
            "can report how much budget is left."
        )

    def _url(self) -> str:
        return (os.environ.get("MAXWELL_USAGE_URL", "") or "").strip() or "https://z3ki.dev/v2/usage"

    def _api_key(self) -> str:
        return (
            os.environ.get("OLLAMA_API_KEY", "")
            or os.environ.get("OPENAI_COMPAT_API_KEY", "")
            or ""
        ).strip()

    async def execute(self, message: Message, **kwargs) -> str:
        url = self._url()
        key = self._api_key()
        if not key:
            return "Error: no API key configured (OLLAMA_API_KEY or OPENAI_COMPAT_API_KEY)."
        session = await _get_shared_session()
        headers = {
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
        }
        try:
            async with session.get(
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                body = await resp.text()
                if resp.status != 200:
                    return f"Error: usage endpoint returned HTTP {resp.status}: {body[:400]}"
        except asyncio.TimeoutError:
            return "Error: usage endpoint timed out."
        except Exception as exc:
            return f"Error: could not reach usage endpoint: {exc}"

        # Condense to a concise summary the model can read at a glance, with
        # the raw payload appended (truncated) only if the shape is unfamiliar.
        try:
            data = json.loads(body)
        except ValueError:
            return f"API usage from {url}:\n{body[:4000]}"

        lines: list[str] = [f"API usage from {url}:"]
        accounts = data.get("accounts")
        if accounts is not None:
            lines.append(f"Accounts: {accounts}")
        combined = data.get("combined") or {}
        if isinstance(combined, dict):
            for family, limits in combined.items():
                if not isinstance(limits, dict):
                    continue
                parts: list[str] = []
                for window in ("5h", "weekly"):
                    info = limits.get(window)
                    if not isinstance(info, dict):
                        continue
                    pct = info.get("remaining_pct")
                    reset = str(info.get("reset_time", ""))[:16]
                    name = info.get("display_name", window)
                    if pct is not None:
                        parts.append(f"{window}: {pct:.1f}% left (resets {reset})")
                    else:
                        parts.append(f"{window}: {name} (resets {reset})")
                if parts:
                    lines.append(f"- {family}: " + " · ".join(parts))
        # Include a trimmed raw copy so unusual fields are still available.
        rendered = json.dumps(data, indent=2, ensure_ascii=False)
        if len(rendered) > 4000:
            rendered = rendered[:4000] + "\n… [truncated]"
        lines.append("\nRaw payload:" + rendered)
        return "\n".join(lines)
