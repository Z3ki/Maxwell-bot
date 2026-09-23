"""Shared helpers for Maxwell tools.

Tool classes live in ``plugins/*/impl.py``. This module keeps the
helpers, private bases, and constants those classes share.

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
import shlex
import shutil
import socket
from mail_transport import connect_imap, mail_ssl_context
import tempfile
import time
import traceback
import wave
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, ClassVar, cast
from urllib.parse import quote, unquote, urljoin, urlparse

import aiofiles
import aiohttp
import asyncio
import base64
import uuid
import discord
from discord import Activity, File, Message, Status
from tools import Tool
from control_defaults import parse_bool
from process_utils import communicate_process
from identity import identity_values, process_name
import site_backend
import site_server
import site_test
from utils import (  # single source of truth, fd-safe
    FileLock,
    _atomic_json_write_sync,
    docker_bind_path,
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
        annotate_legal_moves as _chess_annotate_legal_moves,
        choose_bot_move as _chess_choose_bot_move,
        board_ascii as _chess_board_ascii,
        get_manager as _chess_get_manager,
        position_notes as _chess_position_notes,
        render_board_png as _chess_render_board_png,
    )

    __CHESS_IMPORTED__ = True
except ModuleNotFoundError as _chess_err:  # pragma: no cover - missing optional dep
    if _chess_err.name != "chess":
        raise  # An unrelated broken import is a programming error, not a missing chess dependency.
    __CHESS_IMPORTED__ = False
    _ChessManager = None
    _chess_annotate_legal_moves = None
    _chess_choose_bot_move = None
    _chess_board_ascii = None
    _chess_get_manager = None
    _chess_position_notes = None
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
except Exception:  # noqa: S110 - logging is not configured this early
    # Import-time .env preload before logging is configured, so there is
    # nowhere to report to. config.py loads .env again with override=True and
    # doctor.py reports a genuinely missing/unreadable .env.
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
        # Written off-thread: this runs on the bot's event loop, and a blocking
        # write of a few hundred KB stalls every other chat.
        await asyncio.to_thread(Path(output_path).write_bytes, data)
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
    return bool(getattr(ip, "is_global", False))


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
        host = hostname.lower().rstrip(".")
        if host in {"localhost", "localhost.localdomain"} or host.endswith(
            (".localhost", ".local", ".internal", ".lan")
        ):
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


def _caller_is_admin(bot, message) -> bool:
    author_id = getattr(getattr(message, "author", None), "id", None)
    check = getattr(bot, "_is_admin", None)
    try:
        return bool(author_id is not None and callable(check) and check(author_id))
    except Exception:
        return False


def _is_private_chat(message) -> bool:
    """True for 1:1 DMs and group DMs. False when there is no message."""
    if message is None:
        return False
    channel = getattr(message, "channel", None)
    if isinstance(channel, (discord.DMChannel, discord.GroupChannel)):
        return True
    if getattr(message, "guild", None) is not None:
        return False
    if getattr(channel, "guild", None) is not None:
        return False
    return True


def _destination_is_current_chat(message, dest_id: int) -> bool:
    """True when dest_id is this channel, or the other person in this DM."""
    channel = getattr(message, "channel", None)
    origin = _parse_snowflake(getattr(channel, "id", None))
    if origin is not None and origin == dest_id:
        return True
    people = []
    recipient = getattr(channel, "recipient", None)
    if recipient is not None:
        people.append(recipient)
    people.extend(getattr(channel, "recipients", None) or [])
    for person in people:
        pid = _parse_snowflake(getattr(person, "id", None))
        if pid is not None and pid == dest_id:
            return True
    return False


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
    "delete_messages": "manage_messages",
}

# Extra flags shown when listing a user's role permissions. Not used to
# unlock mod tools — chatting does not make someone a moderator.
_BASIC_PERM_NAMES = (
    "view_channel",
    "send_messages",
    "send_messages_in_threads",
    "create_public_threads",
    "create_private_threads",
    "read_message_history",
    "embed_links",
    "attach_files",
    "add_reactions",
    "use_external_emojis",
    "use_external_stickers",
    "connect",
    "speak",
    "use_voice_activation",
    "stream",
    "change_nickname",
    "use_application_commands",
    "send_polls",
    "send_voice_messages",
)

# Which tools a detected perm actually unlocks. administrator is handled as all.
_CAP_TOOLS: dict[str, tuple[str, ...]] = {
    "manage_channels": (
        "create_category",
        "create_channel",
        "edit_category",
        "edit_channel",
        "move_channel",
        "clone_channel",
        "sync_channel",
        "delete_channel",
        "lock_channel",
        "lockdown",
        "set_channel_permissions",
        "list_permissions",
    ),
    "kick_members": ("kick_member",),
    "ban_members": ("ban_member", "unban_member", "list_bans", "softban_member"),
    "moderate_members": ("timeout_member", "list_timeouts"),
    "manage_roles": (
        "manage_role",
        "lock_channel",
        "lockdown",
        "set_channel_permissions",
        "list_permissions",
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
    "create_instant_invite": ("create_invite", "manage_invites"),
}

_ALL_MOD_TOOLS = tuple(
    dict.fromkeys(name for names in _CAP_TOOLS.values() for name in names)
)
# Guild/moderation tools plus anything that posts into another Discord room.
# Hidden and refused in DMs/group DMs so opening DMs does not hand strangers
# a remote mod console. Shell, sites, search, and current-chat send_message
# stay available. create_invite is kept: it can target another server the
# asker already has create_instant_invite in, and execute still checks that.
DM_BLOCKED_TOOLS = (
    frozenset(_ALL_MOD_TOOLS)
    | {
        "leave_server",
        "list_admin_servers",
        "list_servers",
        "list_channels",
        "list_roles",
        "list_members",
        "forward_message",
        "set_nickname",
        "update_server_prompt",
        "create_thread",
        "thread_control",
        "join_vc",
        "leave_vc",
    }
) - {"create_invite"}
_SNOWFLAKE_RE = re.compile(r"(\d{15,22})")
_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd])?\s*$", re.I)


def _canon_perm(name: str) -> str:
    return _PERM_ALIASES.get(name, name)


def _perms_to_caps(perms) -> tuple[set[str], str]:
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


def _admin_caps(guild, me=None) -> tuple[set[str], str]:
    me = me or _guild_me(guild)
    if not me:
        return set(), "bot member is not cached"
    return _perms_to_caps(getattr(me, "guild_permissions", None))


def _resolve_requester_member(guild, message):
    author = getattr(message, "author", None) if message is not None else None
    if author is None:
        return None
    if guild is None:
        return author
    author_guild = getattr(author, "guild", None)
    if author_guild is not None and getattr(author_guild, "id", None) == getattr(
        guild, "id", None
    ):
        return author
    uid = getattr(author, "id", None)
    getter = getattr(guild, "get_member", None)
    if uid is not None and callable(getter):
        with contextlib.suppress(Exception):
            member = getter(int(uid) if str(uid).isdigit() else uid)
            if member is not None:
                return member
    return author


def _member_channel_perms(member, channel=None):
    if member is None:
        return None
    if channel is not None:
        permissions_for = getattr(channel, "permissions_for", None)
        if callable(permissions_for):
            with contextlib.suppress(Exception):
                perms = permissions_for(member)
                if perms is not None:
                    return perms
    return getattr(member, "guild_permissions", None)


def _member_caps(member, channel=None) -> tuple[set[str], str]:
    if member is None:
        return set(), "member is not cached"
    return _perms_to_caps(_member_channel_perms(member, channel))


def _has_cap(caps: set[str], cap: str) -> bool:
    return "administrator" in caps or cap in caps or _canon_perm(cap) in caps


def _has_guild_cap(guild, cap: str) -> bool:
    caps, _reason = _admin_caps(guild)
    return _has_cap(caps, cap)


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


def _mod_tools_allowed(guild, message) -> set[str]:
    """Mod tools both the bot and the person asking can actually use."""
    if guild is None:
        return set()
    bot_caps, _ = _admin_caps(guild)
    user_caps, _ = _member_caps(_resolve_requester_member(guild, message))
    return set(_tools_for_caps(bot_caps)) & set(_tools_for_caps(user_caps))


def _needed_cap_label(cap: str, alt_caps: tuple[str, ...] = ()) -> str:
    needed = tuple(dict.fromkeys((cap,) + tuple(alt_caps)))
    return needed[0] if len(needed) == 1 else " or ".join(needed)


def _missing_cap(
    guild,
    cap: str,
    message=None,
    *,
    alt_caps: tuple[str, ...] = (),
    channel=None,
) -> str:
    """Refuse unless the bot AND the person asking have the Discord perm."""
    needed = tuple(dict.fromkeys((cap,) + tuple(alt_caps)))
    shown = _needed_cap_label(cap, alt_caps)
    name = getattr(guild, "name", "this server")
    bot_caps, _reason = _admin_caps(guild)
    if not any(_has_cap(bot_caps, c) for c in needed):
        return (
            f"Error: I do not have {shown}/admin in {name}. "
            "Run list_admin_servers to see roles, perms, and which tools I can use."
        )
    if message is None:
        return ""
    member = _resolve_requester_member(guild, message)
    user_caps, _ureason = _member_caps(member, channel)
    if any(_has_cap(user_caps, c) for c in needed):
        return ""
    who = (
        getattr(member, "display_name", None)
        or getattr(member, "name", None)
        or "you"
    )
    return (
        f"Error: {who} does not have {shown} in {name}. "
        "I only run that Discord tool if the person asking has the matching permission."
    )


def _mod_reason(message) -> str:
    name = process_name() or "Bot"
    return f"{name} admin tool requested by {getattr(message, 'author', '?')}"


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


def _member_role_perm_bits(member, guild, *, limit: int = 16) -> str:
    roles = _named_roles(member, guild) if member else []
    if not roles:
        return "@everyone"
    bits = []
    for role in roles[:limit]:
        granted = _role_elevated_perms(role)
        extra = f" [{', '.join(granted)}]" if granted else ""
        bits.append(f"{getattr(role, 'name', 'role')}{extra}")
    if len(roles) > limit:
        bits.append(f"+{len(roles) - limit} more")
    return ", ".join(bits)


def _user_access_line(guild, member) -> str:
    """Per-turn line: the asker's roles, role perms, and which mod tools they can authorize."""
    if guild is None or member is None:
        return ""
    name = str(getattr(guild, "name", None) or "this server").strip() or "this server"
    gid = getattr(guild, "id", "?")
    uname = (
        getattr(member, "display_name", None)
        or getattr(member, "name", None)
        or "user"
    )
    uid = getattr(member, "id", "?")
    role_txt = _member_role_perm_bits(member, guild)
    caps, reason = _member_caps(member)
    if reason and not caps:
        perm_txt = f"none ({reason})"
        tool_txt = "none"
    elif "administrator" in caps:
        perm_txt = "administrator"
        tool_txt = "all guild mod tools"
    else:
        perm_txt = ", ".join(sorted(caps)) if caps else "none"
        tools = _tools_for_caps(caps)
        tool_txt = ", ".join(tools) if tools else "none"
    return (
        f"Asker {uname} ({uid}) Discord access in {name} ({gid}): "
        f"roles={role_txt} | perms={perm_txt} | "
        f"tools they can authorize={tool_txt}"
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
        f"hierarchy pos {_member_top_position(me)}"
    )
    roles = _named_roles(me, guild)
    if roles:
        bits = []
        for role in roles[:12]:
            granted = _role_elevated_perms(role)
            extra = (
                f" grants {', '.join(granted)}"
                if granted
                else " (cosmetic / no extra mod perms)"
            )
            bits.append(f"{_role_label(role)}{extra}")
        lines.append("  roles: " + "; ".join(bits))
    else:
        lines.append("  roles: @everyone only")
    if reason and not caps:
        lines.append(f"  perms: none ({reason})")
    elif "administrator" in caps:
        lines.append("  perms: administrator (every guild mod tool)")
    else:
        lines.append("  perms: " + (", ".join(sorted(caps)) if caps else "none"))
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
    if (
        their_id is not None
        and their_id == my_id
        and action
        in {
            "kick",
            "ban",
            "timeout",
            "voice",
            "nick",
        }
    ):
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
    matches = [r for r in roles if str(getattr(r, "name", "")).lower() == wanted]
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
                return (
                    None,
                    f"Error: user {uid} is not in {getattr(guild, 'name', 'this server')}",
                )
            except discord.Forbidden:
                return (
                    None,
                    f"Error: cannot fetch members in {getattr(guild, 'name', 'this server')}",
                )
            except Exception as exc:
                return None, f"Error fetching member: {exc}"
        if member is None:
            return (
                None,
                f"Error: user {uid} is not in {getattr(guild, 'name', 'this server')}",
            )
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



_YOUTUBE_HOST_RE = re.compile(
    r"(^|\.)((?:music\.)?youtube\.com|youtu\.be|youtube-nocookie\.com)$",
    re.I,
)


def _is_youtube_url(url: str) -> bool:
    """True for youtube.com / youtu.be hosts. Used to skip ToS-breaking fetches."""
    try:
        host = (urlparse(str(url or "")).hostname or "").lower()
    except Exception:
        return False
    return bool(host and _YOUTUBE_HOST_RE.search(host))


def _find_category(guild, category_id: str | None = None, category_name: str | None = None):
    if category_id:
        raw = str(category_id).strip().lower()
        if raw in {"none", "null", "uncategorized", "0"}:
            return None, ""
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
        if wanted in {"none", "null", "uncategorized"}:
            return None, ""
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


async def _lock_target(channel, locked: bool, reason: str) -> str:
    target = getattr(getattr(channel, "guild", None), "default_role", None)
    if target is None:
        return f"Error: @everyone role is unavailable for {_channel_label(channel)}"
    try:
        if isinstance(channel, discord.VoiceChannel) or isinstance(
            channel, getattr(discord, "StageChannel", type(None))
        ):
            await channel.set_permissions(
                target,
                connect=False if locked else None,
                reason=reason,
            )
        else:
            await channel.set_permissions(
                target,
                send_messages=False if locked else None,
                send_messages_in_threads=False if locked else None,
                reason=reason,
            )
    except discord.Forbidden:
        return f"Error: Discord denied locking {_channel_label(channel)}"
    except Exception as e:
        return f"Error locking {_channel_label(channel)}: {e}"
    return ""


def _channel_label(channel) -> str:
    name = getattr(channel, "name", None) or str(getattr(channel, "id", "unknown"))
    return f"#{name} ({getattr(channel, 'id', '?')})"


def _channel_kind(channel) -> str:
    if channel is None:
        return "unknown"
    if isinstance(channel, discord.CategoryChannel):
        return "category"
    if isinstance(channel, discord.VoiceChannel):
        return "voice"
    stage = getattr(discord, "StageChannel", None)
    if stage is not None and isinstance(channel, stage):
        return "stage"
    forum = getattr(discord, "ForumChannel", None)
    if forum is not None and isinstance(channel, forum):
        return "forum"
    if isinstance(channel, discord.Thread):
        return "thread"
    type_val = getattr(channel, "type", None)
    type_name = str(getattr(type_val, "name", type_val) or "").lower().replace(" ", "_")
    mapping = {
        "text": "text",
        "0": "text",
        "voice": "voice",
        "2": "voice",
        "category": "category",
        "4": "category",
        "forum": "forum",
        "15": "forum",
        "stage_voice": "stage",
        "stage": "stage",
        "13": "stage",
        "news": "announcement",
        "5": "announcement",
        "public_thread": "thread",
        "private_thread": "thread",
        "news_thread": "thread",
    }
    if type_name in mapping:
        return mapping[type_name]
    hinted = str(getattr(channel, "kind", "") or "").strip().lower()
    if hinted:
        return hinted
    if getattr(channel, "bitrate", None) is not None:
        return "voice"
    if getattr(channel, "category_id", None) is None and getattr(
        channel, "channels", None
    ) is not None:
        return "category"
    return "text"


def _query_hit(query: str | None, *parts) -> bool:
    wanted = str(query or "").strip().lower()
    if not wanted:
        return True
    blob = " ".join(str(p or "") for p in parts).lower()
    return wanted in blob


def _dt_day(value) -> str:
    if value is None:
        return ""
    fmt = getattr(value, "strftime", None)
    if callable(fmt):
        with contextlib.suppress(Exception):
            return str(fmt("%Y-%m-%d"))
    return str(value)[:10]


def _role_color_hex(role) -> str:
    colour = getattr(role, "colour", None)
    if colour is None:
        colour = getattr(role, "color", None)
    val = getattr(colour, "value", colour)
    try:
        n = int(val)
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    return f"#{n:06X}"


def _granted_perm_names(perms, *, elevated_only: bool = False) -> list[str]:
    if perms is None:
        return []
    if getattr(perms, "administrator", False):
        return ["administrator"]
    names: list[str] = []
    seen: set[str] = set()

    def _add(name: str) -> None:
        key = _canon_perm(str(name))
        if key in seen:
            return
        seen.add(key)
        names.append(key)

    try:
        items = list(perms)
    except Exception:
        items = None
    if items and isinstance(items[0], tuple) and len(items[0]) == 2:
        for raw_name, value in items:
            if not value:
                continue
            key = _canon_perm(str(raw_name))
            if elevated_only and key not in set(_MOD_PERM_NAMES) and key not in _PERM_ALIASES:
                continue
            _add(key)
        return names
    scan = _MOD_PERM_NAMES if elevated_only else _MOD_PERM_NAMES + _BASIC_PERM_NAMES
    for name in scan:
        if name == "administrator":
            continue
        if getattr(perms, name, False):
            _add(name)
    return names


def _role_elevated_perms(role) -> list[str]:
    return _granted_perm_names(getattr(role, "permissions", None), elevated_only=True)


def _role_all_perms(role) -> list[str]:
    return _granted_perm_names(getattr(role, "permissions", None), elevated_only=False)


def _format_member_roles_detail(member, guild=None) -> list[str]:
    """Every role on a member plus that role's permissions and effective caps."""
    guild = guild or getattr(member, "guild", None)
    roles = list(getattr(member, "roles", None) or [])
    roles.sort(key=lambda role: int(getattr(role, "position", 0) or 0), reverse=True)
    lines: list[str] = []
    if not roles:
        lines.append("Roles: none cached")
    else:
        bits = []
        for role in roles:
            granted = _role_all_perms(role)
            extra = (
                f" grants {', '.join(granted)}"
                if granted
                else " (no extra perms)"
            )
            bits.append(f"{_role_label(role)}{extra}")
        lines.append("Roles: " + "; ".join(bits))
    caps, reason = _member_caps(member)
    if reason and not caps:
        lines.append(f"Effective perms: none ({reason})")
    elif "administrator" in caps:
        lines.append("Effective perms: administrator")
    else:
        lines.append(
            "Effective perms: " + (", ".join(sorted(caps)) if caps else "none")
        )
    tools = _tools_for_caps(caps)
    lines.append(
        "Mod tools they can authorize: " + (", ".join(tools) if tools else "none")
    )
    return lines


def _format_role_line(role) -> str:
    bits = [_role_label(role)]
    color = _role_color_hex(role)
    if color:
        bits.append(color)
    members = getattr(role, "members", None)
    if members is not None:
        with contextlib.suppress(Exception):
            bits.append(f"{len(members)} members")
    flags = []
    if getattr(role, "hoist", False):
        flags.append("hoist")
    if getattr(role, "mentionable", False):
        flags.append("mentionable")
    if getattr(role, "managed", False):
        flags.append("managed")
    if flags:
        bits.append(" ".join(flags))
    granted = _role_elevated_perms(role)
    if granted:
        bits.append("grants " + ", ".join(granted))
    return " | ".join(bits)


def _format_channel_line(channel, *, include_topic: bool = True) -> str:
    kind = _channel_kind(channel)
    name = getattr(channel, "name", None) or str(getattr(channel, "id", "unknown"))
    prefix = "#" if kind not in {"voice", "stage", "category"} else ""
    if kind == "category":
        prefix = ""
    bits = [f"{prefix}{name} ({getattr(channel, 'id', '?')}) {kind}"]
    topic = str(getattr(channel, "topic", None) or "").replace("\n", " ").strip()
    if include_topic and topic:
        bits.append("topic=" + topic[:80] + ("…" if len(topic) > 80 else ""))
    if getattr(channel, "nsfw", False):
        bits.append("nsfw")
    slow = getattr(channel, "slowmode_delay", None)
    if isinstance(slow, int) and slow > 0:
        bits.append(f"slowmode={slow}s")
    if kind in {"voice", "stage"}:
        present = list(getattr(channel, "members", None) or [])
        limit = getattr(channel, "user_limit", 0) or 0
        bits.append(f"users={len(present)}" + (f"/{limit}" if limit else ""))
        if present:
            names = [
                getattr(member, "display_name", None)
                or getattr(member, "name", None)
                or str(getattr(member, "id", "?"))
                for member in present[:6]
            ]
            extra = f" +{len(present) - 6}" if len(present) > 6 else ""
            bits.append("in=" + ", ".join(names) + extra)
        bitrate = getattr(channel, "bitrate", None)
        if bitrate:
            bits.append(f"{int(bitrate) // 1000}kbps")
    return " | ".join(bits)


def _format_member_line(member) -> str:
    uid = getattr(member, "id", "?")
    username = getattr(member, "name", None) or str(uid)
    display = getattr(member, "display_name", None) or username
    bits = [f"{display} ({uid}) @{username}"]
    nick = getattr(member, "nick", None)
    if nick and str(nick) != str(display):
        bits.append(f"nick={nick}")
    if getattr(member, "bot", False):
        bits.append("bot")
    if getattr(member, "pending", False):
        bits.append("pending")
    roles = [
        getattr(r, "name", "role")
        for r in (getattr(member, "roles", None) or [])
        if getattr(r, "name", "") not in {"", "@everyone"}
    ]
    if roles:
        shown = roles[:24]
        extra = f" +{len(roles) - 24}" if len(roles) > 24 else ""
        bits.append("roles=" + ", ".join(shown) + extra)
    caps, _reason = _member_caps(member)
    if "administrator" in caps:
        bits.append("perms=administrator")
    elif caps:
        shown_caps = sorted(caps)
        extra = ""
        if len(shown_caps) > 10:
            extra = f" +{len(shown_caps) - 10}"
            shown_caps = shown_caps[:10]
        bits.append("perms=" + ", ".join(shown_caps) + extra)
    status = getattr(member, "status", None)
    status_name = str(getattr(status, "name", status) or "").lower()
    if status_name and status_name not in {"none", "offline"}:
        bits.append(f"status={status_name}")
    elif status_name == "offline":
        bits.append("status=offline")
    joined = _dt_day(getattr(member, "joined_at", None))
    if joined:
        bits.append(f"joined={joined}")
    timeout = getattr(member, "timed_out_until", None) or getattr(
        member, "communication_disabled_until", None
    )
    if timeout:
        bits.append("timed_out_until=" + _dt_day(timeout))
    voice = getattr(getattr(member, "voice", None), "channel", None)
    if voice is not None:
        bits.append("voice=" + _channel_label(voice))
    return " | ".join(bits)


def _guild_channels(guild) -> list:
    seen: dict[int, object] = {}
    buckets = [
        getattr(guild, "channels", None),
        getattr(guild, "categories", None),
        getattr(guild, "text_channels", None),
        getattr(guild, "voice_channels", None),
        getattr(guild, "stage_channels", None),
        getattr(guild, "forums", None),
        getattr(guild, "forum_channels", None),
        getattr(guild, "threads", None),
    ]
    for bucket in buckets:
        for channel in bucket or []:
            cid = getattr(channel, "id", None)
            if cid is None:
                continue
            seen[int(cid)] = channel
    return list(seen.values())


def _channel_category_id(channel):
    cid = getattr(channel, "category_id", None)
    if cid is not None:
        return cid
    parent = getattr(channel, "category", None)
    return getattr(parent, "id", None)


def _render_channel_map(
    guild,
    *,
    kind: str | None = None,
    query: str | None = None,
    category_id: str | None = None,
    limit: int = 80,
    include_topic: bool = True,
) -> str:
    wanted_kind = str(kind or "all").strip().lower()
    if wanted_kind in {"", "all", "*"}:
        wanted_kind = "all"
    cat_filter = _parse_snowflake(category_id)
    channels = _guild_channels(guild)
    categories = [ch for ch in channels if _channel_kind(ch) == "category"]
    categories.sort(key=lambda ch: int(getattr(ch, "position", 0) or 0))
    children: dict[object, list] = {}
    uncategorized: list = []
    for channel in channels:
        ch_kind = _channel_kind(channel)
        if ch_kind == "category":
            continue
        if wanted_kind != "all" and ch_kind != wanted_kind:
            continue
        if not _query_hit(
            query, getattr(channel, "name", ""), getattr(channel, "topic", "")
        ):
            continue
        parent = _channel_category_id(channel)
        if cat_filter is not None and parent != cat_filter:
            continue
        if parent is None:
            uncategorized.append(channel)
        else:
            children.setdefault(parent, []).append(channel)
    for group in children.values():
        group.sort(key=lambda ch: int(getattr(ch, "position", 0) or 0))
    uncategorized.sort(key=lambda ch: int(getattr(ch, "position", 0) or 0))

    lines: list[str] = []
    shown = 0
    total_match = sum(len(v) for v in children.values()) + len(uncategorized)
    if wanted_kind in {"all", "category"}:
        for cat in categories:
            if cat_filter is not None and getattr(cat, "id", None) != cat_filter:
                continue
            if not _query_hit(query, getattr(cat, "name", "")) and not children.get(
                getattr(cat, "id", None)
            ):
                continue
            if shown >= limit:
                break
            lines.append(_format_channel_line(cat, include_topic=False))
            shown += 1
            for child in children.get(getattr(cat, "id", None), []):
                if shown >= limit:
                    break
                lines.append("  " + _format_channel_line(child, include_topic=include_topic))
                shown += 1
    else:
        for cat in categories:
            kids = children.get(getattr(cat, "id", None), [])
            if not kids:
                continue
            lines.append(_format_channel_line(cat, include_topic=False))
            for child in kids:
                if shown >= limit:
                    break
                lines.append("  " + _format_channel_line(child, include_topic=include_topic))
                shown += 1
            if shown >= limit:
                break
    leftover = uncategorized
    if leftover and shown < limit and cat_filter is None:
        if wanted_kind in {"all", "category"} or leftover:
            lines.append("uncategorized")
        for child in leftover:
            if shown >= limit:
                break
            lines.append("  " + _format_channel_line(child, include_topic=include_topic))
            shown += 1
    if not lines:
        return f"No channels matched in {getattr(guild, 'name', 'this server')}."
    header = (
        f"Channels in {getattr(guild, 'name', 'server')} "
        f"({getattr(guild, 'id', '?')}): showing {shown}"
        + (f" of {total_match}" if total_match > shown else "")
    )
    if total_match > shown:
        header += ". Narrow with query= or kind=."
    return header + "\n" + "\n".join(lines)


def _render_role_list(guild, *, query: str | None = None, limit: int = 50) -> str:
    roles = sorted(
        getattr(guild, "roles", []) or [],
        key=lambda r: int(getattr(r, "position", 0) or 0),
        reverse=True,
    )
    matched = [
        role
        for role in roles
        if _query_hit(query, getattr(role, "name", ""), getattr(role, "id", ""))
    ]
    lines = [_format_role_line(role) for role in matched[:limit]]
    header = (
        f"Roles in {getattr(guild, 'name', 'server')} "
        f"({getattr(guild, 'id', '?')}): {len(matched)}/{len(roles)}"
    )
    if len(matched) > limit:
        header += f", showing {limit}. Narrow with query=."
    return header + "\n" + "\n".join(lines or ["none"])


def _member_status_name(member) -> str:
    status = getattr(member, "status", None)
    return str(getattr(status, "name", status) or "").lower()


def _render_member_list(
    guild,
    *,
    query: str | None = None,
    role_spec: str | None = None,
    status: str | None = None,
    limit: int = 40,
) -> str:
    members = list(getattr(guild, "members", []) or [])
    approx = getattr(guild, "member_count", None) or len(members)
    role = None
    if role_spec:
        role, error = _find_role(guild, role_spec)
        if error:
            return error
    wanted_status = str(status or "").strip().lower()
    if wanted_status in {"", "all", "*"}:
        wanted_status = ""
    matched = []
    for member in members:
        if role is not None:
            their_roles = getattr(member, "roles", None) or []
            role_id = getattr(role, "id", None)
            if all(getattr(r, "id", None) != role_id for r in their_roles):
                continue
        if wanted_status:
            if _member_status_name(member) != wanted_status:
                continue
        if not _query_hit(
            query,
            getattr(member, "name", ""),
            getattr(member, "display_name", ""),
            getattr(member, "global_name", ""),
            getattr(member, "nick", ""),
            getattr(member, "id", ""),
        ):
            continue
        matched.append(member)

    def _sort_key(member):
        st = _member_status_name(member)
        rank = {"online": 0, "idle": 1, "dnd": 2, "offline": 3}.get(st, 4)
        name = (
            getattr(member, "display_name", None) or getattr(member, "name", "") or ""
        ).lower()
        return (rank, name)

    matched.sort(key=_sort_key)
    lines = [_format_member_line(m) for m in matched[:limit]]
    header = (
        f"Members in {getattr(guild, 'name', 'server')} "
        f"({getattr(guild, 'id', '?')}): showing {min(len(matched), limit)} of "
        f"{len(matched)} matched, cache {len(members)}/~{approx}"
    )
    if len(members) < int(approx or 0) * 0.6:
        header += " (cache is partial — search by id or query)"
    if len(matched) > limit:
        header += ". Narrow with query= or role=."
    return header + "\n" + "\n".join(lines or ["none"])


def _guild_server_line(guild) -> str:
    name = getattr(guild, "name", "server")
    gid = getattr(guild, "id", "?")
    members = list(getattr(guild, "members", []) or [])
    approx = getattr(guild, "member_count", None) or len(members)
    me = _guild_me(guild)
    nick = ""
    if me is not None:
        nick = getattr(me, "nick", None) or getattr(me, "display_name", "") or ""
    channels = _guild_channels(guild)
    n_text = sum(1 for ch in channels if _channel_kind(ch) == "text")
    n_voice = sum(1 for ch in channels if _channel_kind(ch) in {"voice", "stage"})
    n_cat = sum(1 for ch in channels if _channel_kind(ch) == "category")
    owner = getattr(guild, "owner_id", None) or getattr(
        getattr(guild, "owner", None), "id", None
    )
    bits = [f"{name} (ID: {gid})", f"members~{approx}"]
    if owner:
        bits.append(f"owner={owner}")
    if nick:
        bits.append(f"my_nick={nick}")
    bits.append(f"channels={n_text}t/{n_voice}v/{n_cat}c")
    return " | ".join(bits)


def _guild_room_context(
    guild,
    channel=None,
    recent_users=None,
    *,
    max_chars: int = 2200,
) -> str:
    """Compact server map for the per-turn prompt. Empty outside a guild."""
    if guild is None:
        return ""
    parts: list[str] = []
    if channel is not None:
        cat = getattr(channel, "category", None)
        cat_bit = ""
        if cat is not None:
            cat_bit = (
                f" | category={getattr(cat, 'name', 'category')} "
                f"({getattr(cat, 'id', '?')})"
            )
        parts.append("This channel: " + _format_channel_line(channel) + cat_bit)
    channels = _guild_channels(guild)
    n_text = sum(1 for ch in channels if _channel_kind(ch) == "text")
    n_voice = sum(1 for ch in channels if _channel_kind(ch) in {"voice", "stage"})
    n_cat = sum(1 for ch in channels if _channel_kind(ch) == "category")
    n_forum = sum(1 for ch in channels if _channel_kind(ch) == "forum")
    parts.append(
        f"Server map {getattr(guild, 'name', 'server')} ({getattr(guild, 'id', '?')}): "
        f"{n_cat} categories, {n_text} text, {n_voice} voice"
        + (f", {n_forum} forum" if n_forum else "")
        + f", {len(getattr(guild, 'roles', []) or [])} roles, "
        f"~{getattr(guild, 'member_count', None) or len(getattr(guild, 'members', []) or [])} members. "
        "Use list_channels / list_roles / list_members for the full ids."
    )
    if channels:
        parts.append(
            _render_channel_map(
                guild, limit=24, include_topic=False
            )
        )
    roles = list(getattr(guild, "roles", []) or [])
    if roles:
        parts.append(_render_role_list(guild, limit=16))
    people: list[str] = []
    seen: set[str] = set()
    getter = getattr(guild, "get_member", None)
    for uid, name in list((recent_users or {}).items())[:12]:
        uid_s = str(uid)
        if uid_s in seen:
            continue
        seen.add(uid_s)
        member = None
        if callable(getter):
            with contextlib.suppress(Exception):
                member = getter(int(uid) if str(uid).isdigit() else uid)
        if member is not None:
            people.append(_format_member_line(member))
        else:
            people.append(f"{name} ({uid})")
    if _channel_kind(channel) in {"voice", "stage"}:
        for member in list(getattr(channel, "members", None) or [])[:8]:
            uid_s = str(getattr(member, "id", ""))
            if uid_s and uid_s not in seen:
                seen.add(uid_s)
                people.append(_format_member_line(member))
    if people:
        parts.append("People in this room:\n" + "\n".join(people[:12]))
    text = "\n".join(p for p in parts if p)
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 24].rstrip() + "\n… truncated; use list_* tools"


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


def _public_files_target(bot) -> tuple[str, str]:
    """Return (local_dir, public_base_url) for hosted files.

    Files land in <MAXWELL_SITE_DIR>/_files/<slug>/ and are served at
    <MAXWELL_PUBLIC_BASE_URL>/bot/_files/<slug>/... — same origin as
    create_site pages, so Discord can embed/unfurl the URL.
    """
    cfg = getattr(bot, "config", None)
    site_dir = str(getattr(cfg, "MAXWELL_SITE_DIR", "public/bot") or "public/bot")
    pub = str(
        getattr(cfg, "MAXWELL_PUBLIC_BASE_URL", "https://maxwell.example.com")
        or "https://maxwell.example.com"
    ).rstrip("/")
    return os.path.join(site_dir, "_files"), f"{pub}/bot/_files"


_HOST_MIME_EXT = {
    "text/html": ".html",
    "text/css": ".css",
    "text/javascript": ".js",
    "application/javascript": ".js",
    "application/json": ".json",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio/ogg": ".ogg",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "application/pdf": ".pdf",
    "application/zip": ".zip",
    "font/woff2": ".woff2",
    "font/woff": ".woff",
}


def _blob_looks_like_html(blob: bytes, content_type: str = "", url: str = "") -> bool:
    mime = (content_type or "").split(";", 1)[0].strip().lower()
    if mime.startswith("text/html") or mime == "application/xhtml+xml":
        return True
    path = unquote(urlparse(url or "").path or "").lower()
    if path.endswith((".html", ".htm", ".xhtml")):
        return True
    head = (blob or b"")[:256].lstrip().lower()
    return head.startswith((b"<!doctype html", b"<html"))


def _title_from_html(body: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", body or "", re.I | re.S)
    if not match:
        return ""
    return re.sub(r"\s+", " ", html.unescape(match.group(1))).strip()[:80]


def _filename_from_url(url: str, content_type: str = "", default: str = "file") -> str:
    path = unquote(urlparse(url or "").path or "")
    name = Path(path).name if path else ""
    name = _safe_attachment_filename(name, default=default)
    if name and "." in name and name not in {default, "file"}:
        return name
    mime = (content_type or "").split(";", 1)[0].strip().lower()
    ext = _HOST_MIME_EXT.get(mime, "")
    if ext and not name.endswith(ext):
        stem = name if name not in {"", ".", "..", default, "file"} else default
        if "." in stem:
            stem = Path(stem).stem
        return _safe_attachment_filename(stem + ext, default=default + ext)
    return name or default


def _host_file_slug(raw: Any, fallback: str) -> str:
    slug = re.sub(r"[^a-z0-9-]", "-", str(raw or "").lower().strip())[:30].strip("-")
    if not slug or len(slug) < 2:
        slug = fallback
    if slug in {"images", "files"} or slug.startswith("-"):
        slug = (f"f-{slug.lstrip('-')}" if slug else fallback)[:30]
    return slug or fallback


# class ImageGeneratorTool(Tool):  — moved to a plugin


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


# class HDImageGeneratorTool(Tool):  — moved to a plugin


# class ReactTool(Tool):  — moved to a plugin


# class EditMessageTool(Tool):  — moved to a plugin


# class DeleteMessageTool(Tool):  — moved to a plugin


# class ChangePresenceTool(Tool):  — moved to a plugin


# class SetActivityTool(Tool):  — moved to a plugin


# class SleepTool(Tool):  — moved to a plugin


# class ClearSleepTool(Tool):  — moved to a plugin


# class WaitTool(Tool):  — moved to a plugin


# class CreatePollTool(Tool):  — moved to a plugin


# class CreateInviteTool(Tool):  — moved to a plugin


def _find_guild(guilds: list, target: str) -> tuple[Any, str]:
    """Find one guild by ID, exact name, then unique partial name.

    Returns (guild, "") on a hit and (None, error_text) otherwise.
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


# class LeaveServerTool(Tool):  — moved to a plugin


# class LookupUserTool(Tool):  — moved to a plugin


# class SearchMessagesTool(Tool):  — moved to a plugin


# class SetNicknameTool(Tool):  — moved to a plugin


# class ForwardMessageTool(Tool):  — moved to a plugin


# class TypingTool(Tool):  — moved to a plugin


# class ListServersTool(Tool):  — moved to a plugin


# class ListAdminServersTool(Tool):  — moved to a plugin


# class ListChannelsTool(Tool):  — moved to a plugin


# class ListRolesTool(Tool):  — moved to a plugin


# class ListMembersTool(Tool):  — moved to a plugin


# class CreateCategoryTool(Tool):  — moved to a plugin


# class CreateChannelTool(Tool):  — moved to a plugin


# class EditChannelTool(Tool):  — moved to a plugin


# class DeleteChannelTool(Tool):  — moved to a plugin



# class EditCategoryTool(Tool):  — moved to a plugin


# class MoveChannelTool(Tool):  — moved to a plugin


# class CloneChannelTool(Tool):  — moved to a plugin


# class SyncChannelTool(Tool):  — moved to a plugin


# class LockdownTool(Tool):  — moved to a plugin


# class ListPermissionsTool(Tool):  — moved to a plugin


# class ManageInvitesTool(Tool):  — moved to a plugin


# class SoftbanMemberTool(Tool):  — moved to a plugin


# class ListTimeoutsTool(Tool):  — moved to a plugin



def _role_blocked(me, role) -> str:
    if int(getattr(role, "position", 0) or 0) >= _member_top_position(me):
        return (
            f"Error: role {getattr(role, 'name', role)} is equal/higher than "
            "my top role (hierarchy)"
        )
    return ""


def _role_is_everyone(role, guild) -> bool:
    if role is None:
        return False
    checker = getattr(role, "is_default", None)
    if callable(checker):
        with contextlib.suppress(Exception):
            if bool(checker()):
                return True
    rid = getattr(role, "id", None)
    gid = getattr(guild, "id", None) if guild is not None else None
    return rid is not None and gid is not None and rid == gid


def _same_role(left, right) -> bool:
    if left is None or right is None:
        return False
    if left is right:
        return True
    lid, rid = getattr(left, "id", None), getattr(right, "id", None)
    return lid is not None and lid == rid


def _role_ref(role) -> str:
    name = getattr(role, "name", None) or "role"
    return f"{name} ({getattr(role, 'id', '?')})"


def _parse_role_placement(position=None, above=None, below=None):
    """Return (kind, value, error). kind is position|above|below or None."""
    pos_text = None if position is None else str(position).strip()
    if pos_text == "":
        pos_text = None
    above_text = str(above or "").strip() or None
    below_text = str(below or "").strip() or None
    specified = [
        name
        for name, present in (
            ("position", pos_text is not None),
            ("above", above_text is not None),
            ("below", below_text is not None),
        )
        if present
    ]
    if len(specified) > 1:
        return None, None, "Error: use only one of position, above, or below"
    if not specified:
        return None, None, ""
    if pos_text is not None:
        if isinstance(position, bool) or not re.fullmatch(r"-?\d+", pos_text):
            return None, None, "Error: position must be a whole number"
        return "position", int(pos_text), ""
    if above_text is not None:
        return "above", above_text, ""
    return "below", below_text, ""


def _plan_role_move(
    guild,
    me,
    moving,
    *,
    position=None,
    above=None,
    below=None,
):
    """Compute new positions for roles below the bot's top role.

    Returns (changes, summary, error). ``changes`` is a list of
    ``(role, new_position)`` covering every role in the movable band so the
    Discord payload stays collision-free. Higher position = higher in
    Server Settings > Roles.
    """
    kind, value, err = _parse_role_placement(position, above, below)
    if err:
        return [], "", err
    if kind is None:
        return [], "", (
            "Error: provide position, above, or below to reorder a role"
        )
    if _role_is_everyone(moving, guild):
        return [], "", "Error: the @everyone role cannot be moved"
    ceiling = _member_top_position(me)
    moving_pos = int(getattr(moving, "position", 0) or 0)
    if moving_pos >= ceiling:
        blocked = _role_blocked(me, moving)
        return [], "", blocked or (
            "Error: role is equal/higher than my top role (hierarchy)"
        )
    ordered = sorted(
        getattr(guild, "roles", None) or [],
        key=lambda role: (
            int(getattr(role, "position", 0) or 0),
            int(getattr(role, "id", 0) or 0),
        ),
    )
    movable = [
        role
        for role in ordered
        if not _role_is_everyone(role, guild)
        and int(getattr(role, "position", 0) or 0) < ceiling
    ]
    if not any(_same_role(role, moving) for role in movable):
        return [], "", (
            f"Error: {_role_ref(moving)} is outside the range I can reorder "
            "(must sit below my top role)"
        )
    rest = [role for role in movable if not _same_role(role, moving)]
    insert_at = 0
    relation = ""
    if kind == "position":
        pos = int(value)
        if pos < 1:
            return [], "", (
                "Error: position must be at least 1 (@everyone is always 0)"
            )
        if pos >= ceiling:
            return [], "", (
                f"Error: position {pos} is equal/higher than my top role "
                f"(pos {ceiling}); I can only move roles to 1-{max(1, ceiling - 1)}"
            )
        max_pos = len(rest) + 1
        if pos > max_pos:
            return [], "", (
                f"Error: position {pos} is above the highest I can assign "
                f"({max_pos})"
            )
        insert_at = pos - 1
        relation = f"to position {pos}"
    else:
        target, find_err = _find_role(guild, value)
        if find_err:
            return [], "", find_err
        if _same_role(target, moving):
            return [], "", (
                "Error: a role cannot be placed above or below itself"
            )
        target_pos = int(getattr(target, "position", 0) or 0)
        if kind == "above":
            if _role_is_everyone(target, guild):
                insert_at = 0
                relation = "immediately above @everyone"
            elif target_pos >= ceiling:
                return [], "", (
                    f"Error: {_role_ref(target)} is equal/higher than my top "
                    "role; I cannot place a role above it"
                )
            else:
                idx = next(
                    (
                        i
                        for i, item in enumerate(rest)
                        if _same_role(item, target)
                    ),
                    None,
                )
                if idx is None:
                    return [], "", (
                        f"Error: {_role_ref(target)} is not in the range I "
                        "can reorder around"
                    )
                insert_at = idx + 1
                relation = f"immediately above {_role_ref(target)}"
        elif _role_is_everyone(target, guild):
            return [], "", "Error: nothing can sit below @everyone"
        elif target_pos >= ceiling:
            if target_pos == ceiling:
                insert_at = len(rest)
                relation = f"immediately below {_role_ref(target)}"
            else:
                return [], "", (
                    f"Error: {_role_ref(target)} is above my top role; "
                    "I cannot place a role immediately below it"
                )
        else:
            idx = next(
                (
                    i
                    for i, item in enumerate(rest)
                    if _same_role(item, target)
                ),
                None,
            )
            if idx is None:
                return [], "", (
                    f"Error: {_role_ref(target)} is not in the range I "
                    "can reorder around"
                )
            insert_at = idx
            relation = f"immediately below {_role_ref(target)}"
    new_order = rest[:insert_at] + [moving] + rest[insert_at:]
    changes = [(item, pos) for pos, item in enumerate(new_order, start=1)]
    changed = [
        (role, pos)
        for role, pos in changes
        if int(getattr(role, "position", 0) or 0) != pos
    ]
    new_pos = insert_at + 1
    guild_name = getattr(guild, "name", None) or "this server"
    if not changed:
        summary = (
            f"{_role_ref(moving)} is already {relation} in {guild_name}"
        )
        return [], summary, ""
    summary = (
        f"Moved {_role_ref(moving)} {relation} in {guild_name} "
        f"(now pos {new_pos}; higher number is higher in "
        "Server Settings > Roles)"
    )
    return changes, summary, ""


async def _move_role_hierarchy(
    guild,
    me,
    role,
    *,
    position=None,
    above=None,
    below=None,
    reason: str = "",
) -> str:
    changes, summary, err = _plan_role_move(
        guild,
        me,
        role,
        position=position,
        above=above,
        below=below,
    )
    if err:
        return err
    if not changes:
        return summary
    mover = getattr(guild, "edit_role_positions", None)
    if not callable(mover):
        return "Error: this Discord library cannot reorder role positions"
    payload = {
        discord.Object(id=int(item.id)): int(pos)
        for item, pos in changes
    }
    try:
        await mover(payload, reason=reason)
    except discord.Forbidden:
        return (
            f"Error: Discord denied reordering {_role_ref(role)} "
            "(manage_roles or hierarchy)"
        )
    except Exception as e:
        return f"Error reordering role: {e}"
    return summary


# class KickMemberTool(Tool):  — moved to a plugin


# class BanMemberTool(Tool):  — moved to a plugin


# class UnbanMemberTool(Tool):  — moved to a plugin


# class ListBansTool(Tool):  — moved to a plugin


# class TimeoutMemberTool(Tool):  — moved to a plugin


# class ManageRoleTool(Tool):  — moved to a plugin


# class PurgeMessagesTool(Tool):  — moved to a plugin


# class PinMessageTool(Tool):  — moved to a plugin


# class SetMemberNicknameTool(Tool):  — moved to a plugin


# class VoiceModTool(Tool):  — moved to a plugin


# class LockChannelTool(Tool):  — moved to a plugin


# class SetChannelPermissionsTool(Tool):  — moved to a plugin


# class EditServerTool(Tool):  — moved to a plugin


# class AuditLogTool(Tool):  — moved to a plugin


# class ManageEmojiTool(Tool):  — moved to a plugin


# class ChangeAvatarTool(Tool):  — moved to a plugin


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
# History used to replace huge create_site/edit_site payloads with this
# marker. If that string is ever sent back as the page, refuse to write it.
_ELIDED_SITE_PAYLOAD_RE = re.compile(
    r"^\[large \w+ omitted, \d+ chars\]$",
    re.IGNORECASE,
)
# Extensions a static host will serve as-is. Anything executable server-side
# (.php, .cgi) is pointless here and only invites confusion about what runs.
SITE_BLOCKED_SUFFIXES = {
    ".php",
    ".php5",
    ".phtml",
    ".cgi",
    ".pl",
    ".jsp",
    ".asp",
    ".aspx",
}


# Site `action=read` used to dump the whole file into the tool result. A 30–40k
# index.html is larger than the tool-loop tail budget, so the previous round
# (the other file) is dropped, the model re-reads that one, and we ping-pong
# until max_tool_iterations. Window large files and refuse duplicate reads.
SITE_READ_FULL_CHARS = 8_000
SITE_READ_WINDOW_CHARS = 6_000
SITE_IDLE_READ_LIMIT = 6
SITE_TEST_REPEAT_LIMIT = 2
SITE_READ_LOOP_MARKER = "__SITE_READ_LOOP__"
SITE_FILE_READ_ACTIONS = frozenset({"read", "cat", "get"})
SITE_MUTATING_ACTIONS = frozenset(
    {
        "write",
        "put",
        "set",
        "update",
        "patch_file",
        "code",
        "replace",
        "patch",
        "sub",
        "delete",
        "rm",
        "remove",
        "unlink",
        "deploy",
        "create",
        "snapshot",
        "start",
        "restart",
        "reload",
    }
)

# discord.py Message uses __slots__, so setattr(_site_idle_reads) raises
# AttributeError and every site tool blows up. Keep counters here, keyed by
# id(message) for the life of one tool loop.
_SITE_TURN_STATE: dict[int, dict[str, Any]] = {}
_SITE_TURN_STATE_MAX = 128


def _site_turn_state(message: Any) -> dict[str, Any] | None:
    """Idle-read / site_test counters for one tool-loop turn."""
    if message is None:
        return None
    key = id(message)
    state = _SITE_TURN_STATE.get(key)
    # CPython recycles id() after GC. Stale counters from a dead message
    # must not attach to a new object that happens to get the same id.
    if state is not None and state.get("_obj") is not message:
        _SITE_TURN_STATE.pop(key, None)
        state = None
    if state is None:
        while len(_SITE_TURN_STATE) >= _SITE_TURN_STATE_MAX:
            oldest = next(iter(_SITE_TURN_STATE), None)
            if oldest is None:
                break
            _SITE_TURN_STATE.pop(oldest, None)
        state = {
            "idle": 0,
            "test_counts": {},
            "read_cache": set(),
            "_obj": message,
        }
        _SITE_TURN_STATE[key] = state
    return state


def _site_start_line(raw: Any) -> int:
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return 1
    return max(1, min(n, 1_000_000))


def format_site_file_read(rel: str, text: str, *, start_line: int = 1) -> str:
    """Return a file without blowing the tool-loop tail budget.

    Small files come back whole. Larger ones get a numbered window; pass
    ``start_line`` to page. A minified one-liner is paged by character so it
    cannot dump 40k into the tool loop (that is what hung Maxwell).
    """
    start_line = _site_start_line(start_line)
    raw = text or ""
    n = len(raw)
    if n <= SITE_READ_FULL_CHARS and start_line <= 1:
        return f"{rel} ({n} chars):\n{raw}"
    lines = raw.splitlines(keepends=True)
    total = len(lines)
    if total == 0:
        return f"{rel} (0 chars):\n"
    # One giant line (minified HTML/JS): page by start_line as a char window.
    if total == 1 and n > SITE_READ_FULL_CHARS:
        offset = (start_line - 1) * SITE_READ_WINDOW_CHARS
        if offset >= n:
            offset = max(0, n - SITE_READ_WINDOW_CHARS)
        chunk = raw[offset : offset + SITE_READ_WINDOW_CHARS]
        more = ""
        if offset + len(chunk) < n:
            more = (
                f" Chars {offset + len(chunk) + 1}–{n} omitted — "
                f"pass start_line={start_line + 1} to continue."
            )
        return (
            f"{rel} ({n} chars, 1 line) showing chars "
            f"{offset + 1}–{offset + len(chunk)}.{more}\n"
            "Do not re-read this file unless you need another slice. "
            "Patch with action=replace (exact text from this window) or "
            "action=write.\n" + chunk
        )
    start = min(start_line, total)
    out: list[str] = []
    used = 0
    last = start - 1
    for i in range(start - 1, total):
        raw_line = lines[i]
        numbered = f"{i + 1}|{raw_line if raw_line.endswith(chr(10)) else raw_line + chr(10)}"
        if not out and len(numbered) > SITE_READ_WINDOW_CHARS:
            numbered = numbered[:SITE_READ_WINDOW_CHARS] + "\n"
            out.append(numbered)
            last = i + 1
            break
        if out and used + len(numbered) > SITE_READ_WINDOW_CHARS:
            break
        out.append(numbered)
        used += len(numbered)
        last = i + 1
    more = ""
    if last < total:
        more = f" Lines {last + 1}–{total} omitted — pass start_line={last + 1} to continue."
    return (
        f"{rel} ({n} chars, {total} lines) showing {start}–{last}.{more}\n"
        "Do not re-read this file unless you need another slice. "
        "Patch with action=replace (exact text from this window) or action=write.\n"
        + "".join(out)
    )


def site_read_loop_guard(
    message: Any, *, key: str, label: str, action: str
) -> str | None:
    """Refuse duplicate/idle site reads that hang the turn. None = proceed."""
    act = str(action or "").strip().lower()
    state = _site_turn_state(message)
    if act in SITE_MUTATING_ACTIONS:
        if state is not None:
            state["idle"] = 0
            state["test_counts"] = {}
            state["read_cache"] = set()
        return None
    if act not in SITE_FILE_READ_ACTIONS or state is None:
        return None
    idle = int(state.get("idle", 0) or 0) + 1
    state["idle"] = idle
    cache = state.setdefault("read_cache", set())
    if idle >= SITE_IDLE_READ_LIMIT:
        cache.add(key)
        return (
            "STOP. You have re-read site files repeatedly this turn without "
            "changing anything. The source is already in this turn. Call "
            "action=write or action=replace with a real change, then "
            f"send_message with the URL. {SITE_READ_LOOP_MARKER}"
        )
    if key in cache:
        return (
            f"Already returned {label} this turn — it is in an earlier tool "
            "result. Use action=replace or action=write to change it, or "
            "start_line= to window a different slice. Re-reading the same "
            "file will not print it again."
        )
    cache.add(key)
    return None


def site_test_repeat_guard(message: Any, fingerprint: str) -> str | None:
    """Refuse a third site_test of the same URL with no edit in between."""
    state = _site_turn_state(message)
    if state is None:
        return None
    state["idle"] = 0
    counts = state.setdefault("test_counts", {})
    n = int(counts.get(fingerprint, 0) or 0) + 1
    counts[fingerprint] = n
    if n > SITE_TEST_REPEAT_LIMIT:
        return (
            f"Already ran site_test on this URL this turn ({n - 1} times). "
            "Fix with edit_site or site_server (write/replace), then test "
            f"once, or send_message with the URL. {SITE_READ_LOOP_MARKER}"
        )
    return None


def _elided_site_payload_error(text: Any) -> str:
    """Error if ``text`` is a context-elision marker, not real site source."""
    if not isinstance(text, str):
        return ""
    stripped = text.strip()
    if len(stripped) > 240:
        return ""
    if _ELIDED_SITE_PAYLOAD_RE.match(stripped) or stripped in {
        "[large body elided]",
        "[large HTML/asset body elided to protect context budget; site creation succeeded from the original full body]",
    }:
        return (
            "Error: that text is a truncated-history placeholder, not the page. "
            "The real files are already on disk — use edit_site action=replace "
            "with a short find/replace, or send the actual HTML again."
        )
    return ""


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


# Text that means the page was never finished. These are matched against the
# HTML the model just wrote, because a 200 response and a screenshot of an
# unmounted page both look fine — the shortfall is only visible in the source.
_PLACEHOLDER_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"lorem\s+ipsum", "lorem ipsum filler text"),
    (r"\bTODO\b", "a TODO left in the page"),
    (r"\bFIXME\b", "a FIXME left in the page"),
    (r"coming\s+soon", "a 'coming soon' placeholder"),
    (r"\[insert[^\]]*\]", "an '[insert …]' placeholder"),
    (r"your\s+(?:text|content|title)\s+here", "a 'your text here' placeholder"),
    (r"\bplaceholder\s+(?:text|content|image)\b", "placeholder content"),
    (r"//\s*(?:implement|fill\s+in|add)\s+(?:this|later|me)", "an unimplemented stub"),
    (r"#\s*(?:implement|fill\s+in)\s+(?:this|later|me)", "an unimplemented stub"),
    (r"\bnot\s+implemented\b", "a 'not implemented' branch"),
    (r"\bTBD\b", "a TBD left in the page"),
)
# A nav or button that goes nowhere. Counted rather than named, because one
# href="#" is a legitimate JS hook and a dozen is an unwired navigation bar.
_DEAD_LINK_RE = re.compile(r"""<a\b[^>]*href\s*=\s*["']#["'][^>]*>""", re.IGNORECASE)
_DEAD_LINK_LIMIT = 4


def _site_placeholder_warnings(body: str | None, extra_files: list[dict]) -> list[str]:
    """Unfinished-content shortfalls in what was just written.

    Reported back through the tool result rather than blocking the write: a
    refusal would lose the whole page, and a page with one TODO in a comment is
    still worth publishing and then fixing.
    """
    sources: list[tuple[str, str]] = []
    if body:
        sources.append(("index.html", str(body)))
    for entry in extra_files or []:
        blob = entry.get("bytes") or b""
        if not blob or len(blob) > 2_000_000:
            continue
        with contextlib.suppress(UnicodeDecodeError):
            sources.append((str(entry.get("path") or "file"), blob.decode("utf-8")))

    found: list[str] = []
    for label, text in sources:
        for pattern, description in _PLACEHOLDER_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                item = f"{label}: {description}"
                if item not in found:
                    found.append(item)
        dead = len(_DEAD_LINK_RE.findall(text))
        if dead >= _DEAD_LINK_LIMIT:
            found.append(f"{label}: {dead} links point at href='#' and go nowhere")
    return found[:12]


_FETCH_ABS_API_RE = re.compile(
    r"""(?:fetch|axios\.\w+|EventSource)\s*\(\s*['"`]\s*/api/"""
)
_WS_HARDCODED_RE = re.compile(
    r"""new\s+WebSocket\s*\(\s*['"`](?:wss?://|/api/)"""
)
_BOT_API_SLUG_RE = re.compile(r"""['"`]/bot/([A-Za-z0-9._ \-]{1,80})/api/""")


def _site_api_path_warnings(slug: str, body: str | None, extra_files: list[dict]) -> list[str]:
    """Frontend calls that can never reach this site's backend.

    The page is served under /bot/<slug>/, so fetch('/api/...') resolves
    to the domain root and 404s — the frontend must call its backend with
    RELATIVE paths ('api/...'). A hardcoded /bot/<other>/api/... is the
    same bug with a different slug. Reported, not blocking: same rationale
    as _site_placeholder_warnings.
    """
    sources: list[tuple[str, str]] = []
    if body:
        sources.append(("index.html", str(body)))
    for entry in extra_files or []:
        blob = entry.get("bytes") or b""
        if not blob or len(blob) > 2_000_000:
            continue
        with contextlib.suppress(UnicodeDecodeError):
            sources.append((str(entry.get("path") or "file"), blob.decode("utf-8")))

    found: list[str] = []
    for label, text in sources:
        if not label.lower().endswith((".html", ".htm", ".js")):
            continue
        if _FETCH_ABS_API_RE.search(text):
            found.append(
                f"{label}: absolute fetch('/api/...') 404s under /bot/{slug}/ — "
                "call the backend with relative paths ('api/...')"
            )
        if _WS_HARDCODED_RE.search(text):
            found.append(
                f"{label}: hardcoded WebSocket URL — build it from "
                f"location.origin.replace('http', 'ws') + '/bot/{slug}/api/ws'"
            )
        found.extend(
            f"{label}: calls /bot/{match.group(1)}/api/... but this site "
            f"is {slug} — use relative 'api/...' instead"
            for match in _BOT_API_SLUG_RE.finditer(text)
            if match.group(1) != slug
        )
    return found[:12]


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


def _site_graph_note(bot, slug: str, *, refresh: bool = True) -> str:
    """One-line route graph for tool results. Never raises into the tool."""
    try:
        from knowledge_graph import graph_from_bot, refresh_site

        if refresh:
            note = refresh_site(bot, slug)
        else:
            graph = graph_from_bot(bot)
            note = graph.summarize_site(slug) if graph is not None else ""
        return f"\nGraph: {note}" if note else ""
    except Exception as e:
        logger.debug("site graph skipped: %s", e)
        return ""


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
    if re.search(
        r"http-equiv\s*=\s*[\"']?Content-Security-Policy", body, re.IGNORECASE
    ):
        return body
    if re.search(r"<head[^>]*>", body, re.IGNORECASE):
        return re.sub(
            r"(<head[^>]*>)",
            r"\1\n" + SITE_CSP_META,
            body,
            count=1,
            flags=re.IGNORECASE,
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


# class CreateSiteTool(Tool):  — moved to a plugin


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
        slug = re.sub(r"[^a-z0-9-]", "-", str(name or "").lower().strip())[:30].strip(
            "-"
        )
        if not slug:
            return None, None, None, "Error: name is required (the site slug)."
        if hasattr(self.bot, "_load_sites"):
            self.bot._load_sites(quiet=True)
        entry = (self.bot._sites or {}).get(slug)
        if not isinstance(entry, dict):
            return (
                None,
                None,
                None,
                (
                    f"Error: no site named '{slug}'. Call list_sites to see the slugs you own."
                ),
            )
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


# class EditSiteTool(_SiteOwnedTool):  — moved to a plugin


# class SiteServerTool(_SiteOwnedTool):  — moved to a plugin


class SiteTestTool(_SiteOwnedTool):
    """Load a published site in a real browser and report what broke."""

    def get_description(self):
        return (
            "Load one of your published sites the way a visitor's browser would: "
            "JS console errors, uncaught exceptions, failed network requests, "
            "broken CSS/JS/images, HTTP status, and a screenshot (vision). "
            "Call this once after create_site / edit_site / site_server before "
            "telling the user it works. fetch_url only sees HTML — this sees "
            "runtime. Params: name (slug), path (optional subpage or this site's "
            "full URL), wait (seconds for JS, default 2), screenshot (default true). "
            "Fix what it finds with write/replace, then test once more. Do not "
            "re-test the same URL without changing a file."
        )

    async def execute(
        self,
        message: Message,
        name: str | None = None,
        path: str | None = None,
        url: str | None = None,
        wait: Any = None,
        screenshot: Any = None,
        **kwargs,
    ) -> str:
        slug, entry, site_dir, err = self._resolve(message, name)
        if err:
            return err
        try:
            target = site_test.page_url(self.base_url, slug, path or url)
        except ValueError as e:
            return f"Error: {e}. path must be a page on this site."
        blocked = site_test_repeat_guard(message, f"{slug}:{target}")
        if blocked:
            return blocked
        try:
            wait_s = (
                float(wait) if wait is not None and str(wait).strip() != "" else 2.0
            )
        except (TypeError, ValueError):
            wait_s = 2.0
        wait_s = max(0.2, min(wait_s, 15.0))
        want_shot = parse_bool(screenshot, True)

        html = ""
        local = site_test.html_path_for_url(site_dir, slug, target)
        if local.is_file():
            with contextlib.suppress(OSError, UnicodeDecodeError):
                html = local.read_text(encoding="utf-8")

        asset_errors = [
            f"missing on disk: {rel}"
            for rel in site_test.missing_local_assets(html, site_dir, slug=slug)
        ]

        status, body, http_err = await site_test.http_get(target)
        if body and not html:
            with contextlib.suppress(UnicodeDecodeError):
                html = body.decode("utf-8")
        if html:
            linked = site_test.extract_assets(html, target)
            for item in await site_test.check_assets(linked):
                if item not in asset_errors:
                    asset_errors.append(item)

        backend_bits: list[str] = []
        if entry.get("server"):
            api_url = f"{self.base_url}/{slug}/api/"
            api_status, _, api_err = await site_test.http_get(api_url)
            if api_err:
                backend_bits.append(f"Python API {api_url} unreachable: {api_err}")
            else:
                backend_bits.append(f"Python API {api_url} HTTP {api_status}")
            try:
                log_text = await site_server.logs(
                    self.bot.config.DATA_DIR, slug, lines=20
                )
                clipped = (log_text or "").strip()[:2000]
                if clipped:
                    backend_bits.append("Recent logs:\n" + clipped)
            except Exception as e:
                backend_bits.append(f"logs: {e}")
        if entry.get("backend"):
            public = getattr(self.bot.config, "MAXWELL_PUBLIC_BASE_URL", "").rstrip("/")
            kv_url = f"{public}/api/site/{slug}/kv"
            kv_status, _, kv_err = await site_test.http_get(kv_url)
            if kv_err:
                backend_bits.append(f"KV store {kv_url} unreachable: {kv_err}")
            else:
                backend_bits.append(f"KV store {kv_url} HTTP {kv_status}")

        browser = await site_test.probe_browser(
            target, wait=wait_s, screenshot=want_shot
        )

        probe: dict[str, Any] = {
            "url": target,
            "http_status": (
                browser["http_status"]
                if browser.get("http_status") is not None
                else status
            ),
            "http_error": http_err,
            "title": browser.get("title") or "",
            "console_errors": list(browser.get("console_errors") or []),
            "console_warnings": list(browser.get("console_warnings") or []),
            "page_errors": list(browser.get("page_errors") or []),
            "failed_requests": list(browser.get("failed_requests") or []),
            "asset_errors": asset_errors,
            "backend": "\n".join(backend_bits),
            "screenshot_png": browser.get("screenshot_png"),
            # What actually rendered. format_report uses these to catch a page
            # that returns 200 with a clean console and is still just a
            # "Loading…" shell — the failure mode behind "the site is listed
            # but it doesn't work".
            "has_canvas_or_media": browser.get("has_canvas_or_media"),
        }
        if browser.get("visible_text") is not None:
            probe["visible_text"] = browser["visible_text"]
        if browser.get("rendered_nodes") is not None:
            probe["rendered_nodes"] = browser["rendered_nodes"]
        if browser.get("browser"):
            probe["browser"] = browser["browser"]
        if browser.get("browser_error"):
            probe["browser_error"] = browser["browser_error"]
        if not probe["title"] and html:
            title_match = re.search(
                r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL
            )
            if title_match:
                probe["title"] = re.sub(r"\s+", " ", title_match.group(1)).strip()[:120]
        report = site_test.format_report(probe)
        # Record whether this site currently renders, so list_sites can say so
        # without loading a browser for every entry.
        with contextlib.suppress(Exception):
            await self._record_health(slug, entry, probe)
        return report

    async def _record_health(self, slug: str, entry: dict, probe: dict) -> None:
        """Persist the last site_test verdict on the site entry."""
        stub = site_test.describe_stub(probe)
        broken = bool(
            stub
            or probe.get("page_errors")
            or probe.get("asset_errors")
            or probe.get("http_error")
        )
        updated = dict(entry or {})
        health = {
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "ok": not broken,
        }
        if stub:
            health["stub"] = stub[:200]
        if updated.get("health") == health:
            return
        updated["health"] = health
        await asyncio.to_thread(self._save_entry, slug, updated)


# class DeleteSiteTool(_SiteOwnedTool):  — moved to a plugin


# class ListSitesTool(Tool):  — moved to a plugin


_WEB_REPLY_CTX_RE = re.compile(r"\[Latest message replies to[^\]]*\]", re.IGNORECASE)


_WEB_SNIPPET_CHARS = 400

# ddgs `auto` currently fans out through Brave/Google, which 429/captcha in
# this environment. Prefer engines that still return hits, then fall through.
_WEB_SEARCH_BACKENDS = (
    "duckduckgo",
    "bing",
    "startpage",
    "mojeek",
    "yahoo",
    "wikipedia",
)
_WEB_SEARCH_BUDGET_SEC = 28.0


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


def _web_search_backends(engine: str | None) -> list[str]:
    """Resolve ddgs backends. Explicit engine is tried first, then fallbacks."""
    requested = str(engine or "").strip()
    if not requested or requested.lower() in {"auto", "default"}:
        return list(_WEB_SEARCH_BACKENDS)
    if not re.fullmatch(r"[a-z0-9_.,-]+", requested, flags=re.I):
        return list(_WEB_SEARCH_BACKENDS)
    backends = [b.strip().lower() for b in requested.split(",") if b.strip()]
    for fallback in _WEB_SEARCH_BACKENDS:
        if fallback not in backends:
            backends.append(fallback)
    return backends or list(_WEB_SEARCH_BACKENDS)


async def _web_search_collect(
    ddgs_cls: Any, query: str, limit: int, backends: list[str]
) -> tuple[list[dict[str, str]], list[str]]:
    """Query backends in order until we have `limit` unique hits or time runs out."""
    loop = asyncio.get_running_loop()
    seen: set[str] = set()
    hits: list[dict[str, str]] = []
    errors: list[str] = []
    deadline = time.monotonic() + _WEB_SEARCH_BUDGET_SEC
    for backend in backends:
        if len(hits) >= limit:
            break
        remaining = deadline - time.monotonic()
        if remaining < 3:
            break
        want = max(1, limit - len(hits))
        timeout = min(18.0, remaining)

        def _run(b=backend, n=want, wait=timeout):
            return list(
                ddgs_cls(timeout=min(20, max(5, int(wait)))).text(
                    query, max_results=n, backend=b
                )
            )

        try:
            raw = await asyncio.wait_for(
                loop.run_in_executor(None, _run),
                timeout=timeout,
            )
        except Exception as exc:
            err = str(exc).strip() or type(exc).__name__
            if re.search(r"no results", err, re.I):
                continue
            errors.append(f"{backend}: {err}")
            logger.info("web_search backend %s failed: %s", backend, err)
            continue
        for row in raw or []:
            hit = _normalize_web_hit(row)
            if not (hit["href"] or hit["body"]):
                continue
            key = hit["href"] or hit["title"]
            if key in seen:
                continue
            seen.add(key)
            hits.append(hit)
            if len(hits) >= limit:
                break
    return hits, errors


# class WebSearchTool(Tool):  — moved to a plugin


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


async def _iter_recent_channel_messages(message, bot=None, limit: int = 20):
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
    except TimeoutError:
        # Partial history is still useful, but a silent timeout looked exactly
        # like an empty channel to every caller.
        logger.warning(
            "channel history timed out after %ss with %d message(s)",
            _CHANNEL_HISTORY_TIMEOUT,
            len(collected),
        )
    except Exception as e:
        logger.warning(
            "channel history read failed after %d msg(s): %s", len(collected), e
        )
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

    recent: list[Any] = [
        msg async for msg in _iter_recent_channel_messages(message, bot=bot)
    ]
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


# class SendMessageTool(Tool):  — moved to a plugin


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


# class NoResponseTool(Tool):  — moved to a plugin


# class MoreToolsTool(Tool):  — moved to a plugin


# class SendFileTool(Tool):  — moved to a plugin


# class HostFileTool(Tool):  — moved to a plugin


# Patterns blocked in shell commands (defense-in-depth even in full-access mode).
# These mainly prevent accidental or malicious attempts to run nested privileged containers,
# mount host paths from inside commands, or access the Docker socket.
# The sandbox process is root with full capabilities. Isolation is the bind
# mount (only shelldocker/ unless MAXWELL_SHELL_FULL_HOST) plus taint tracking
# and this blocklist — not dropped capabilities.
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


async def _run_docker_cmd(
    *args: str, timeout: int = 30, output_limit: int | None = None
):
    """Run one `docker` command. Returns ``((stdout, stderr), returncode)``."""
    proc = await asyncio.create_subprocess_exec(
        "docker",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        if output_limit is None:
            output = proc.communicate()
        else:
            limit = max(0, int(output_limit))

            async def _read_limited(stream):
                data = bytearray()
                while True:
                    chunk = await stream.read(4096)
                    if not chunk:
                        break
                    if len(data) < limit:
                        data.extend(chunk[: limit - len(data)])
                return bytes(data)

            output = asyncio.gather(
                _read_limited(proc.stdout),
                _read_limited(proc.stderr),
                proc.wait(),
            )
            result = await asyncio.wait_for(output, timeout=timeout)
            return (result[0], result[1]), result[2]
        return await asyncio.wait_for(output, timeout=timeout), proc.returncode
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


# Image used by the shell sandbox. Built from docker/Dockerfile on first use.
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


# class ShellTool(Tool):  — moved to a plugin


def _shorten(text, n: int) -> str:
    """Collapse to one line and truncate. Shared by the live-message renderers."""
    t = " ".join(str(text or "").split())
    return t[:n] + ("…" if len(t) > n else "")


_FETCH_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_MAX_FETCH_REDIRECTS = 5
_FETCH_HEADERS = {
    "User-Agent": _IMAGE_FETCH_UA,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "application/json,text/plain;q=0.8,*/*;q=0.5"
    ),
}
_JINA_READER_PREFIX = "https://r.jina.ai/"


def _is_too_large_error(exc: BaseException) -> bool:
    return "response too large" in str(exc).lower()


def _jina_reader_url(url: str) -> str:
    raw = str(url or "").strip()
    if raw.startswith(_JINA_READER_PREFIX):
        return raw
    return _JINA_READER_PREFIX + raw


async def _fetch_via_jina_reader(
    url: str,
    *,
    max_bytes: int,
    timeout: float = 30.0,
) -> tuple[str, str, bytes]:
    """Retry a public URL through Jina Reader (extracted markdown).

    SSRF is enforced on the *original* URL, not on whatever Jina fetches
    server-side. Optional ``JINA_API_KEY`` is sent as Bearer; anonymous
    otherwise.
    """
    if not _is_safe_url(url):
        raise ValueError("Cannot fetch from private/internal URLs")
    extra = {
        "Accept": "text/markdown, text/plain;q=0.9, */*;q=0.5",
    }
    api_key = (os.getenv("JINA_API_KEY") or "").strip()
    if api_key:
        extra["Authorization"] = f"Bearer {api_key}"
    _final, content_type, raw = await _fetch_public_url(
        _jina_reader_url(url),
        max_bytes=max_bytes,
        timeout=timeout,
        extra_headers=extra,
    )
    return url, content_type or "text/markdown", raw


async def _fetch_public_url(
    url: str,
    *,
    max_bytes: int,
    timeout: float = 30.0,
    extra_headers: dict | None = None,
) -> tuple[str, str, bytes]:
    """GET a public URL, following a few SSRF-checked redirects.

    Each hop is re-checked with `_is_safe_url`. The shared session's
    `_SafeResolver` also refuses DNS that lands on private/link-local IPs.
    Returns `(final_url, content_type, body)`. Raises ValueError with a
    user-facing message on refusal, HTTP errors, or timeout.
    """
    current = url
    headers = dict(_FETCH_HEADERS)
    if extra_headers:
        headers.update(extra_headers)
    try:
        session = await _get_shared_session()
        for _hop in range(_MAX_FETCH_REDIRECTS + 1):
            if not _is_safe_url(current):
                raise ValueError("Cannot fetch from private/internal URLs")
            async with session.get(
                current,
                timeout=aiohttp.ClientTimeout(total=timeout),
                allow_redirects=False,
                headers=headers,
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


# class FetchUrlTool(Tool):  — moved to a plugin


# class SeeImageTool(Tool):  — moved to a plugin


# class SeeVideoTool(Tool):  — moved to a plugin


# class SendMemeTool(Tool):  — moved to a plugin


# class SendMediaTool(Tool):  — moved to a plugin


# KiloTool removed — it was a host-level RCE escape hatch that bypassed
# the Docker sandbox. One prompt injection and the LLM owns your box.


# class TtsTool(Tool):  — moved to a plugin


def _is_voice_channel(ch) -> bool:
    if ch is None:
        return False
    try:
        if isinstance(ch, discord.VoiceChannel):
            return True
        stage = getattr(discord, "StageChannel", None)
        if stage is not None and isinstance(ch, stage):
            return True
    except Exception as e:
        # isinstance can fail against stubbed/mocked discord classes; the
        # type-name check below is the intended fallback.
        logger.debug("voice channel isinstance check failed: %s", e)
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


def _resolve_voice_channel(
    bot, message, channel_id=None, channel_name=None, user_id=None
):
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


# class InboxListTool(Tool):  — moved to a plugin


# class InboxActTool(Tool):  — moved to a plugin


# class JoinVcTool(Tool):  — moved to a plugin


# class VcStatusTool(Tool):  — moved to a plugin


# class VcWhereTool(Tool):  — moved to a plugin


# class LeaveVcTool(Tool):  — moved to a plugin


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
    from_name = (
        str(getattr(cfg, "MAXWELL_EMAIL_FROM_NAME", "") or "").strip()
        or process_name(bot)
        or str(getattr(cfg, "BOT_NAME", "") or "").strip()
        or identity_values(cfg).get("bot_name", "")
        or ""
    )
    return {
        "host": getattr(cfg, "MAXWELL_SMTP_HOST", "127.0.0.1"),
        "smtp_port": int(getattr(cfg, "MAXWELL_SMTP_PORT", "25")),
        "imap_host": getattr(cfg, "MAXWELL_IMAP_HOST", "127.0.0.1"),
        "imap_port": int(getattr(cfg, "MAXWELL_IMAP_PORT", "993")),
        "user": getattr(cfg, "MAXWELL_EMAIL_USER", "") or "",
        "password": getattr(cfg, "MAXWELL_EMAIL_PASSWORD", ""),
        "from_addr": getattr(cfg, "MAXWELL_EMAIL_FROM", "") or "",
        "from_name": from_name,
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
        s.starttls(context=mail_ssl_context(host))
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
    """Open IMAPS; callers must log out when finished."""
    return connect_imap(host, port, user, password)


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
        selected, _ = M.select("INBOX", readonly=True)
        if selected != "OK":
            return "Error: could not select INBOX"
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
        selected, _ = M.select("INBOX", readonly=True)
        if selected != "OK":
            return "Error: could not select INBOX"
        typ, data = M.uid("FETCH", seq, "(BODY.PEEK[])")
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
        selected, _ = M.select("INBOX", readonly=True)
        if selected != "OK":
            return "Error: could not select INBOX"
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


# class EmailSendTool(Tool):  — moved to a plugin


# class EmailReadInboxTool(Tool):  — moved to a plugin


# class EmailGetMessageTool(Tool):  — moved to a plugin


# class EmailSearchTool(Tool):  — moved to a plugin


# ---------------------------------------------------------------------------
# Self-modification tools. These let Maxwell rewrite its own base
# personality + per-server prompts at runtime. The runtime load is hot —
# _load_control() reads mtime, so a write to bot_control.json is picked up
# on the next prompt assembly without a restart. server prompts are read
# on every prompt build, also hot.
# ---------------------------------------------------------------------------


# class UpdateBasePersonalityTool(Tool):  — moved to a plugin


# class UpdateServerPromptTool(Tool):  — moved to a plugin


# --------------------------------------------------------------------------- #
# Chess
#
# The chess_* tools let this bot play a real game against a chosen player in
# a channel. Exactly one active game per channel; only that opponent may move.
# The bot "sees" the board two ways: the tool result carries the board as
# ASCII + FEN + legal moves, and the posted PNG is returned as base64
# (__IMAGE_B64__) so the vision path attaches it to the next model turn. The
# same PNG is posted to the channel so the player sees it too.
# --------------------------------------------------------------------------- #

_CHESS_MENTION_RE = re.compile(r"<@!?(\d+)>")


def _chess_bot_name(bot=None) -> str:
    """Live people-facing name for this process (Maxwell, a nick, …)."""
    user = getattr(bot, "user", None) if bot is not None else None
    name = getattr(bot, "bot_name", None) if bot is not None else None
    name = str(
        name
        or getattr(user, "display_name", None)
        or getattr(user, "name", None)
        or process_name(bot)
        or identity_values().get("bot_name", "")
        or ""
    ).strip()
    return name or process_name(bot) or "Bot"


def _chess_user_label(user) -> str:
    return (
        str(
            getattr(user, "display_name", None)
            or getattr(user, "global_name", None)
            or getattr(user, "name", None)
            or getattr(user, "id", "")
            or "player"
        ).strip()
        or "player"
    )


def _chess_user_names(user) -> list[str]:
    names = []
    for attr in ("display_name", "global_name", "name", "nick"):
        val = getattr(user, attr, None)
        if val:
            names.append(str(val))
    uid = getattr(user, "id", None)
    if uid is not None:
        names.append(str(uid))
    return names


def _chess_is_self(bot, user) -> bool:
    if user is None:
        return False
    bot_user = getattr(bot, "user", None) if bot is not None else None
    uid = str(getattr(user, "id", "") or "")
    bot_id = str(getattr(bot_user, "id", "") or "")
    if uid and bot_id and uid == bot_id:
        return True
    needle = _chess_user_label(user).strip().lower()
    return bool(needle) and needle == _chess_bot_name(bot).lower()


def _chess_lookup_id(bot, message, uid: str):
    """Sync cache lookup only — never fetch over the network from a tool helper."""
    try:
        uid_int = int(uid)
    except (TypeError, ValueError):
        return None
    channel = getattr(message, "channel", None)
    guild = getattr(message, "guild", None) or getattr(channel, "guild", None)
    for obj in (guild, bot):
        if obj is None:
            continue
        for meth in ("get_member", "get_user"):
            fn = getattr(obj, meth, None)
            if not callable(fn):
                continue
            with contextlib.suppress(Exception):
                found = fn(uid_int)
                if found is not None:
                    return found
    return None


def _chess_resolve_player(message, opponent, bot) -> tuple[str, str]:
    """Who this bot is playing: ``(user_id, display_name)``.

    ``opponent`` is a mention, snowflake, or display name. Blank means the
    asker, unless the message @mentioned exactly one other human — then that
    person is the opponent.
    """
    author = getattr(message, "author", None)
    mentions = [u for u in (getattr(message, "mentions", None) or []) if u is not None]
    raw = str(opponent or "").strip()

    def _from_user(user):
        if user is None:
            raise ValueError("no opponent")
        if _chess_is_self(bot, user):
            raise ValueError(
                f"cannot play against {_chess_bot_name(bot)} — pick a human opponent"
            )
        uid = str(getattr(user, "id", "") or "")
        if not uid:
            raise ValueError("opponent has no user id")
        return uid, _chess_user_label(user)

    if not raw:
        author_id = str(getattr(author, "id", "") or "")
        others = [
            u
            for u in mentions
            if not _chess_is_self(bot, u)
            and str(getattr(u, "id", "") or "") != author_id
        ]
        if len(others) == 1:
            return _from_user(others[0])
        return _from_user(author)

    mention = _CHESS_MENTION_RE.fullmatch(raw)
    if mention:
        uid = mention.group(1)
        for user in mentions:
            if str(getattr(user, "id", "") or "") == uid:
                return _from_user(user)
        found = _chess_lookup_id(bot, message, uid)
        if found is not None:
            return _from_user(found)
        return uid, raw

    if raw.isdigit() and len(raw) >= 15:
        for user in mentions:
            if str(getattr(user, "id", "") or "") == raw:
                return _from_user(user)
        found = _chess_lookup_id(bot, message, raw)
        if found is not None:
            return _from_user(found)
        return raw, raw

    needle = raw.lstrip("@").strip().lower()
    pool: list = list(mentions)
    if author is not None:
        pool.append(author)
    channel = getattr(message, "channel", None)
    guild = getattr(message, "guild", None) or getattr(channel, "guild", None)
    getter = getattr(guild, "get_member_named", None) if guild is not None else None
    if callable(getter):
        with contextlib.suppress(Exception):
            named = getter(raw.lstrip("@"))
            if named is not None:
                pool.append(named)
    members = list(getattr(guild, "members", None) or []) if guild is not None else []
    if members and len(members) <= 500:
        pool.extend(members)
    pool.extend(list(getattr(channel, "recipients", None) or []))

    matches = []
    seen: set[str] = set()
    for user in pool:
        uid = str(getattr(user, "id", "") or "") or str(id(user))
        if uid in seen:
            continue
        names = [n.lower() for n in _chess_user_names(user)]
        if needle in names or any(
            n.startswith(needle) and len(needle) >= 3 for n in names
        ):
            seen.add(uid)
            matches.append(user)

    if len(matches) == 1:
        return _from_user(matches[0])
    if len(matches) > 1:
        labels = ", ".join(_chess_user_label(u) for u in matches[:8])
        raise ValueError(
            f"opponent '{raw}' is ambiguous ({labels}). Use a mention or user id."
        )
    raise ValueError(
        f"could not find opponent '{raw}'. Mention them, pass their user id, "
        "or omit opponent to play the person who asked."
    )


def _chess_is_bot_resign(who: str, bot=None) -> bool:
    token = str(who or "").strip().lower().lstrip("@")
    if token in {"bot", "engine", "ai", "me"}:
        return True
    name = _chess_bot_name(bot).lower()
    aliases = {name}
    if name == "maxwell":
        aliases.add("max")
    return token in aliases


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


def _chess_state_text(game, bot_name: str | None = None) -> str:
    """The board + metadata the model needs to play, as plain text.

    When it is Maxwell's turn this is his entire view of the position, because
    he now picks the move himself instead of delegating to the search. A bare
    SAN list is not enough for that: the annotations say what each move
    captures, whether it checks or mates, and whether the piece lands on a
    square where it is simply taken.
    """
    name = str(bot_name or "").strip() or process_name() or "Bot"
    lines: list[str] = []
    lines.append("CHESS BOARD (text — see attached image for the real board):")
    lines.append(_chess_board_ascii(game.board))
    lines.append("")
    lines.append(f"FEN: {game.fen}")
    move_hist = " ".join(game.history_san) or "none"
    lines.append(f"Move history (SAN): {move_hist}")
    lines.append(
        f"{name}={_chess_color_name(game.bot_color)} · "
        f"{game.player_name}={_chess_color_name(game.player_color)}"
    )
    result = game.result
    if result:
        lines.append(f"GAME OVER: {result}")
        return "\n".join(lines)

    lines.append(f"To move: {game.turn_label}")
    who = name if game.bot_turn else game.player_name
    lines.append(f"It is {who}'s move.")

    if game.bot_turn:
        # Maxwell's own turn: give him the annotated position, all of it. The
        # legal list is not truncated here — a move he cannot see is a move he
        # cannot play, and in a sharp position the cut-off 49th move is
        # sometimes the only one that does not lose.
        notes: list[str] = []
        annotated: list[str] = []
        if _chess_position_notes is not None:
            with contextlib.suppress(Exception):
                notes = list(_chess_position_notes(game.board))
        if _chess_annotate_legal_moves is not None:
            with contextlib.suppress(Exception):
                annotated = list(_chess_annotate_legal_moves(game.board))
        if notes:
            lines.append("")
            lines.append("POSITION:")
            lines.extend(f"  • {note}" for note in notes)
        moves = annotated or game.legal_san
        lines.append("")
        lines.append(f"YOUR LEGAL MOVES ({len(moves)}) — play exactly one of these:")
        lines.extend(f"  {item}" for item in moves)
        lines.append("")
        lines.append(
            "Pick the move yourself and pass it as chess_move(move='<SAN>'). "
            "Read the tags before choosing: take free material, answer threats "
            "to your own pieces, and do not play a move tagged LOSES THE PIECE "
            "unless you can show it wins more back. Play to win."
        )
    else:
        legal = game.legal_san
        shown = ", ".join(legal[:48])
        if len(legal) > 48:
            shown += f" … (+{len(legal) - 48} more)"
        lines.append(f"Legal moves for them ({len(legal)}): {shown}")
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
        logger.warning(
            "Cannot post chess board in %s — missing permissions",
            getattr(message.channel, "id", "?"),
        )
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
    except Exception as e:  # pragma: no cover
        # Memory write is best-effort; never fail the tool over it.
        logger.debug("Failed to record tool output in channel memory: %s", e)


# game_id -> consecutive illegal/absent moves on Maxwell's own turn. Maxwell
# picks his own moves now, so the failure mode to protect against is a game
# wedged forever because he keeps naming a move that is not legal. After
# _CHESS_MAX_MISSES tries the local search plays one move so the game advances;
# the counter resets on every successful move.
_CHESS_MISSES: dict[str, int] = {}
_CHESS_MAX_MISSES = 3


def _chess_note_miss(game_id: str) -> int:
    key = str(game_id or "")
    if not key:
        return 0
    count = _CHESS_MISSES.get(key, 0) + 1
    _CHESS_MISSES[key] = count
    if len(_CHESS_MISSES) > 256:
        for stale in list(_CHESS_MISSES)[:128]:
            _CHESS_MISSES.pop(stale, None)
    return count


def _chess_clear_misses(game_id: str) -> None:
    _CHESS_MISSES.pop(str(game_id or ""), None)


def _chess_miss_count(game_id: str) -> int:
    return _CHESS_MISSES.get(str(game_id or ""), 0)


def _chess_game_result(
    game,
    *,
    posted: bool,
    cdn_url: str = "",
    local_path: str = "",
    png: bytes | None = None,
    bot_name: str | None = None,
) -> str:
    text = _chess_state_text(game, bot_name=bot_name)
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


# class ChessStartTool(Tool):  — moved to a plugin


# class ChessStateTool(Tool):  — moved to a plugin


# class ChessMoveTool(Tool):  — moved to a plugin


# class ChessResignTool(Tool):  — moved to a plugin


# class UsageTool(Tool):  — moved to a plugin


def collect_debug_stats(bot, channel_id: str | None = None) -> str:
    """TTFT / TPS / token dump from the live provider + daily counter."""
    from providers import format_timing_debug

    provider = getattr(bot, "ai_provider", None)
    history = list(getattr(provider, "_timing_history", None) or [])
    if not history:
        last = getattr(provider, "_last_timing", None)
        if isinstance(last, dict) and last:
            history = [last]
    daily = None
    tracker = getattr(bot, "_token_tracker", None)
    if tracker is not None and hasattr(tracker, "summary"):
        with contextlib.suppress(Exception):
            daily = tracker.summary()
    extra: list[str] = []
    queue = getattr(bot, "_reply_queue", None)
    if queue is not None and channel_id and hasattr(queue, "depth"):
        with contextlib.suppress(Exception):
            extra.append(f"queue depth {queue.depth(str(channel_id))}")
    active = getattr(bot, "_active_requests", None)
    if isinstance(active, dict):
        extra.append(f"in-flight turns {sum(1 for t in active.values() if t and not t.done())}")
    return format_timing_debug(history, daily=daily, extra=extra or None)


_OWNER_NOTIFY_MIN_INTERVAL = 45.0
_OWNER_NOTIFY_FINGERPRINT_TTL = 600.0
_OWNER_NOTIFY_HOUR_CAP = 12


def _report_context_lines(message) -> list[str]:
    if message is None:
        return []
    lines: list[str] = []
    author = getattr(message, "author", None)
    if author is not None:
        who = (
            getattr(author, "display_name", None)
            or getattr(author, "name", None)
            or "?"
        )
        lines.append(f"who: {who} ({getattr(author, 'id', '?')})")
    channel = getattr(message, "channel", None)
    guild = getattr(message, "guild", None)
    if channel is not None:
        ch_name = getattr(channel, "name", None)
        if not ch_name:
            ch_name = "DM" if guild is None else str(getattr(channel, "id", "?"))
        bit = f"where: #{ch_name} ({getattr(channel, 'id', '?')})"
        if guild is not None:
            bit += (
                f" in {getattr(guild, 'name', 'server')} "
                f"({getattr(guild, 'id', '?')})"
            )
        lines.append(bit)
    mid = getattr(message, "id", None)
    if mid is not None:
        lines.append(f"message_id: {mid}")
    content = str(getattr(message, "content", "") or "").replace("\n", " ").strip()
    if content:
        lines.append("said: " + content[:400])
    return lines


def _redact_report_text(text: str) -> str:
    try:
        from autofix import redact_diagnostics

        return redact_diagnostics(str(text or ""))
    except Exception:
        return str(text or "")


async def notify_owner(
    bot,
    *,
    kind: str = "report",
    title: str,
    details: str = "",
    message=None,
    exc: BaseException | None = None,
) -> str:
    """DM the configured owner (CREATOR_ID / first owner) with a report."""
    if getattr(bot, "_owner_notify_sending", False):
        return "Error: already sending an owner report"
    kind = str(kind or "report").strip().lower() or "report"
    if kind not in {"report", "error", "info"}:
        kind = "report"
    title = _redact_report_text(str(title or "").strip())[:240]
    if not title:
        return "Error: what/title is required"
    values = identity_values(getattr(bot, "config", None))
    uid = str(values.get("creator_id") or "").strip()
    owner_name = str(values.get("creator_name") or "owner").strip() or "owner"
    if not uid.isdigit():
        return "Error: no owner Discord id configured (CREATOR_ID / MAXWELL_OWNER_IDS)"
    self_id = getattr(getattr(bot, "user", None), "id", None)
    if self_id is not None and str(self_id) == uid:
        return "Error: owner id is this bot"
    now = time.monotonic()
    times: list[float] = getattr(bot, "_owner_notify_times", None) or []
    stamps: dict[str, float] = getattr(bot, "_owner_notify_fingerprints", None) or {}
    bot._owner_notify_times = times
    bot._owner_notify_fingerprints = stamps
    times[:] = [t for t in times if now - t < 3600]
    expired = [k for k, t in stamps.items() if now - t > _OWNER_NOTIFY_FINGERPRINT_TTL]
    for key in expired:
        stamps.pop(key, None)
    fingerprint = "|".join(
        (
            kind,
            title[:80],
            type(exc).__name__ if exc is not None else "",
        )
    )
    last_same = stamps.get(fingerprint)
    if last_same is not None and now - last_same < _OWNER_NOTIFY_FINGERPRINT_TTL:
        return "Owner already got this report recently; not sending a duplicate."
    if times and now - times[-1] < _OWNER_NOTIFY_MIN_INTERVAL:
        return "Owner report rate-limited; try again in a minute."
    if len(times) >= _OWNER_NOTIFY_HOUR_CAP:
        return "Owner report hourly cap reached; not sending."
    when = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [
        f"**Maxwell {kind}**",
        title,
        f"when: {when}",
    ]
    lines.extend(_report_context_lines(message))
    extra = _redact_report_text(str(details or "").strip())
    if extra:
        lines.append("details:")
        lines.append(extra[:1500])
    if exc is not None:
        lines.append(f"error: {type(exc).__name__}: {_redact_report_text(str(exc))[:400]}")
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        tb = _redact_report_text(tb).strip()
        if tb:
            if len(tb) > 1200:
                tb = "…" + tb[-1200:]
            lines.append("```")
            lines.append(tb)
            lines.append("```")
    body = "\n".join(lines).strip()
    bot._owner_notify_sending = True
    try:
        getter = getattr(bot, "get_user", None)
        user = getter(int(uid)) if callable(getter) else None
        if user is None:
            fetch = getattr(bot, "fetch_user", None)
            if not callable(fetch):
                return f"Error: cannot resolve owner {uid}"
            user = await fetch(int(uid))
        if user is None:
            return f"Error: owner {uid} not found"
        dm = getattr(user, "dm_channel", None)
        if dm is None:
            create_dm = getattr(user, "create_dm", None)
            if not callable(create_dm):
                return "Error: cannot open owner DM"
            dm = await create_dm()
        sender = getattr(dm, "send", None)
        if not callable(sender):
            return "Error: owner DM cannot send"
        from plugins.discord_messages.impl import SendMessageTool

        chunks = SendMessageTool._chunks(body)
        for chunk in chunks:
            await sender(chunk)
        times.append(now)
        stamps[fingerprint] = now
        return f"Reported to {owner_name} ({uid})."
    except discord.Forbidden:
        return (
            f"Error: cannot DM owner {uid} — they have DMs closed or blocked this bot"
        )
    except Exception as send_exc:
        return f"Error sending owner DM: {send_exc}"
    finally:
        bot._owner_notify_sending = False


# class ReportTool(Tool):  — moved to a plugin


# class DebugTool(Tool):  — moved to a plugin


# class ManagePluginTool(Tool):  — moved to a plugin
