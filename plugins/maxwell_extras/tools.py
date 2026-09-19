"""Extra Discord-native tools for Maxwell.

This plugin intentionally keeps optional "power tools" out of normal prompt
assembly. The model calls them only when it actually needs cross-server recall,
reminders, rich Discord UI, or URL media inspection.

It also patches the live image-generation tool instances so generated files are
captured and handed back to the LLM as tool media instead of being posted to
Discord automatically.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import re
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import MethodType, SimpleNamespace
from typing import Any
from urllib.parse import urlparse

import aiohttp
import discord

from tools import Tool

logger = logging.getLogger(__name__)

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".avif"}
_VIDEO_EXTS = {".mp4", ".webm", ".mov", ".mkv", ".m4v", ".avi"}
_AUDIO_EXTS = {".wav", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".flac", ".webm"}
_HEX_RE = re.compile(r"^[0-9a-fA-F]{6}$")


def _json_list(value: Any) -> list[dict]:
    """Accept a JSON string or a list and return only object entries."""
    if value is None or value == "":
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            return []
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _parse_colour(value: Any) -> discord.Colour:
    text = str(value or "").strip().lstrip("#")
    if not _HEX_RE.fullmatch(text):
        return discord.Colour.blurple()
    return discord.Colour(int(text, 16))


def _utc_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _parse_iso(value: str) -> float:
    text = str(value or "").strip()
    if not text:
        raise ValueError("at_iso is empty")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


class ReminderStore:
    """Small atomic JSON store used by the reminder polling job."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = asyncio.Lock()

    def _read_sync(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text("utf-8"))
        except Exception:
            logger.exception("Failed to read reminder store %s", self.path)
            return []
        if not isinstance(raw, list):
            return []
        return [item for item in raw if isinstance(item, dict)]

    def _write_sync(self, rows: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(rows, indent=2, ensure_ascii=False), "utf-8")
        tmp.replace(self.path)

    async def add(
        self,
        *,
        owner_id: str,
        channel_id: str,
        text: str,
        due_at: float,
    ) -> dict:
        async with self._lock:
            rows = self._read_sync()
            row = {
                "id": uuid.uuid4().hex[:12],
                "owner_id": str(owner_id),
                "channel_id": str(channel_id),
                "text": str(text)[:1800],
                "due_at": float(due_at),
                "created_at": time.time(),
            }
            rows.append(row)
            rows.sort(key=lambda item: float(item.get("due_at") or 0))
            self._write_sync(rows)
            return dict(row)

    async def list_for(self, owner_id: str) -> list[dict]:
        async with self._lock:
            rows = self._read_sync()
        return [
            dict(item)
            for item in rows
            if str(item.get("owner_id") or "") == str(owner_id)
        ]

    async def remove(self, reminder_id: str, owner_id: str | None = None) -> bool:
        async with self._lock:
            rows = self._read_sync()
            kept = []
            removed = False
            for item in rows:
                same_id = str(item.get("id") or "") == str(reminder_id)
                owner_ok = owner_id is None or str(item.get("owner_id") or "") == str(owner_id)
                if same_id and owner_ok:
                    removed = True
                    continue
                kept.append(item)
            if removed:
                self._write_sync(kept)
            return removed

    async def due(self, now: float | None = None) -> list[dict]:
        cutoff = float(time.time() if now is None else now)
        async with self._lock:
            rows = self._read_sync()
        return [
            dict(item)
            for item in rows
            if float(item.get("due_at") or 0) <= cutoff
        ]


class ReminderTool(Tool):
    returns_result = True

    def __init__(self, bot: Any, store: ReminderStore):
        self.bot = bot
        self.store = store

    def get_name(self) -> str:
        return "reminder"

    def get_description(self) -> str:
        return (
            "Create/list/cancel persistent reminders. Reminders survive restarts and "
            "are delivered back to the Discord channel. Params: action=create|list|cancel; "
            "text; delay_seconds or at_iso; reminder_id for cancel."
        )

    def get_parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["create", "list", "cancel"],
                    "default": "create",
                },
                "text": {"type": "string"},
                "delay_seconds": {
                    "type": "integer",
                    "description": "Seconds from now; max 90 days",
                },
                "at_iso": {
                    "type": "string",
                    "description": "Absolute ISO-8601 time, preferably with timezone",
                },
                "reminder_id": {"type": "string"},
            },
        }

    async def execute(
        self,
        message: Any,
        action: str = "create",
        text: str | None = None,
        delay_seconds: int | str | None = None,
        at_iso: str | None = None,
        reminder_id: str | None = None,
        **kwargs,
    ) -> str:
        owner_id = str(getattr(getattr(message, "author", None), "id", "") or "")
        channel_id = str(getattr(getattr(message, "channel", None), "id", "") or "")
        if not owner_id or not channel_id:
            return "Error: reminders require a Discord user and channel."

        act = str(action or "create").strip().lower()
        if act == "list":
            rows = await self.store.list_for(owner_id)
            if not rows:
                return "No reminders scheduled."
            rows.sort(key=lambda item: float(item.get("due_at") or 0))
            return "\n".join(
                f"{item['id']} — {_utc_iso(float(item['due_at']))} — {item.get('text', '')}"
                for item in rows[:25]
            )

        if act == "cancel":
            rid = str(reminder_id or "").strip()
            if not rid:
                return "Error: reminder_id is required to cancel."
            removed = await self.store.remove(rid, owner_id=owner_id)
            return (
                f"Cancelled reminder {rid}."
                if removed
                else "Error: reminder not found or it belongs to another user."
            )

        body = str(text or "").strip()
        if not body:
            return "Error: text is required."
        try:
            if at_iso:
                due_at = _parse_iso(at_iso)
            else:
                seconds = int(delay_seconds if delay_seconds is not None else 0)
                if seconds < 1:
                    return "Error: delay_seconds must be at least 1."
                if seconds > 90 * 86400:
                    return "Error: reminders are capped at 90 days."
                due_at = time.time() + seconds
        except (TypeError, ValueError) as exc:
            return f"Error: invalid reminder time: {exc}"

        if due_at <= time.time():
            return "Error: reminder time must be in the future."
        if due_at > time.time() + 90 * 86400:
            return "Error: reminders are capped at 90 days."

        row = await self.store.add(
            owner_id=owner_id,
            channel_id=channel_id,
            text=body,
            due_at=due_at,
        )
        return f"Reminder {row['id']} scheduled for {_utc_iso(due_at)}."


async def deliver_due_reminders(bot: Any, store: ReminderStore) -> None:
    for item in await store.due():
        rid = str(item.get("id") or "")
        channel_id = str(item.get("channel_id") or "")
        owner_id = str(item.get("owner_id") or "")
        if not rid or not channel_id:
            continue
        channel = None
        with contextlib.suppress(Exception):
            channel = bot.get_channel(int(channel_id))
        if channel is None:
            with contextlib.suppress(Exception):
                channel = await bot.fetch_channel(int(channel_id))
        if channel is None:
            logger.warning("Reminder %s channel %s is unavailable", rid, channel_id)
            continue
        text = str(item.get("text") or "")[:1800]
        try:
            await channel.send(
                f"<@{owner_id}> reminder: {text}",
                allowed_mentions=discord.AllowedMentions(
                    everyone=False, roles=False, users=True, replied_user=False
                ),
            )
        except Exception:
            logger.exception("Failed delivering reminder %s", rid)
            continue
        await store.remove(rid)


class SendRichMessageTool(Tool):
    """Send Discord embeds or Components V2 layouts."""

    def __init__(self, bot: Any):
        self.bot = bot

    def get_name(self) -> str:
        return "send_rich_message"

    def get_description(self) -> str:
        return (
            "Send a polished Discord message in the current channel. Supports classic "
            "embeds, fields, image/thumbnail/footer, link buttons, and Components V2 "
            "(LayoutView/Container/TextDisplay) when the installed discord.py supports it. "
            "Use this only when rich presentation is useful; normal replies should stay plain."
        )

    def get_parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["embed", "components_v2", "auto"],
                    "default": "auto",
                },
                "title": {"type": "string"},
                "description": {"type": "string"},
                "color": {
                    "type": "string",
                    "description": "Six-digit hex color, e.g. 5865F2",
                },
                "footer": {"type": "string"},
                "image_url": {"type": "string"},
                "thumbnail_url": {"type": "string"},
                "fields": {
                    "type": "string",
                    "description": "JSON list of {name,value,inline} objects",
                },
                "buttons": {
                    "type": "string",
                    "description": "JSON list of link buttons: {label,url,emoji?}",
                },
            },
        }

    @staticmethod
    def _link_buttons(raw: Any) -> list[discord.ui.Button]:
        out: list[discord.ui.Button] = []
        for item in _json_list(raw)[:5]:
            label = str(item.get("label") or "Open")[:80]
            url = str(item.get("url") or "").strip()
            if not url.startswith(("https://", "http://")):
                continue
            kwargs: dict[str, Any] = {
                "label": label,
                "url": url,
                "style": discord.ButtonStyle.link,
            }
            emoji = str(item.get("emoji") or "").strip()
            if emoji:
                kwargs["emoji"] = emoji
            with contextlib.suppress(Exception):
                out.append(discord.ui.Button(**kwargs))
        return out

    async def _send_v2(
        self,
        message: Any,
        *,
        title: str,
        description: str,
        color: str,
        buttons: Any,
    ) -> bool:
        layout_cls = getattr(discord.ui, "LayoutView", None)
        container_cls = getattr(discord.ui, "Container", None)
        text_cls = getattr(discord.ui, "TextDisplay", None)
        row_cls = getattr(discord.ui, "ActionRow", None)
        if not all((layout_cls, container_cls, text_cls, row_cls)):
            return False

        text = ""
        if title:
            text += f"## {title[:256]}\n"
        text += description[:3900]
        if not text.strip():
            text = "\u200b"

        try:
            children: list[Any] = [text_cls(text)]
            link_buttons = self._link_buttons(buttons)
            if link_buttons:
                children.append(row_cls(*link_buttons))
            accent = None
            raw_color = str(color or "").strip().lstrip("#")
            if _HEX_RE.fullmatch(raw_color):
                accent = discord.Colour(int(raw_color, 16))
            container = (
                container_cls(*children, accent_colour=accent)
                if accent is not None
                else container_cls(*children)
            )
            view = layout_cls(timeout=None)
            view.add_item(container)
            await message.channel.send(view=view)
            return True
        except Exception:
            logger.exception("Components V2 send failed; falling back to embed")
            return False

    async def execute(
        self,
        message: Any,
        mode: str = "auto",
        title: str | None = None,
        description: str | None = None,
        color: str | None = None,
        footer: str | None = None,
        image_url: str | None = None,
        thumbnail_url: str | None = None,
        fields: Any = None,
        buttons: Any = None,
        **kwargs,
    ) -> str:
        title_s = str(title or "").strip()
        desc_s = str(description or "").strip()
        if not title_s and not desc_s:
            return "Error: title or description is required."

        mode_s = str(mode or "auto").strip().lower()
        if mode_s in {"components_v2", "auto"}:
            sent = await self._send_v2(
                message,
                title=title_s,
                description=desc_s,
                color=str(color or ""),
                buttons=buttons,
            )
            if sent:
                return "Sent Components V2 rich message."

        embed = discord.Embed(
            title=title_s[:256] or None,
            description=desc_s[:4096] or None,
            colour=_parse_colour(color),
        )
        for item in _json_list(fields)[:25]:
            name = str(item.get("name") or "\u200b")[:256]
            value = str(item.get("value") or "\u200b")[:1024]
            embed.add_field(name=name, value=value, inline=bool(item.get("inline", False)))
        if footer:
            embed.set_footer(text=str(footer)[:2048])
        if image_url and str(image_url).startswith(("http://", "https://")):
            embed.set_image(url=str(image_url))
        if thumbnail_url and str(thumbnail_url).startswith(("http://", "https://")):
            embed.set_thumbnail(url=str(thumbnail_url))

        link_buttons = self._link_buttons(buttons)
        view = None
        if link_buttons:
            view = discord.ui.View(timeout=None)
            for button in link_buttons:
                view.add_item(button)
        await message.channel.send(embed=embed, view=view)
        return "Sent rich embed message."


class RecallCrossServerMemoryTool(Tool):
    returns_result = True

    def __init__(self, bot: Any):
        self.bot = bot

    def get_name(self) -> str:
        return "recall_cross_server_memory"

    def get_description(self) -> str:
        return (
            "Explicitly search Maxwell's stored memory across servers/channels instead of "
            "injecting extra cross-server history into every normal response. Non-admins can "
            "only search their own authored history plus memory already visible to them. "
            "Admins may request scope=global."
        )

    def get_parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 8},
                "scope": {
                    "type": "string",
                    "enum": ["self", "global"],
                    "default": "self",
                },
            },
            "required": ["query"],
        }

    async def execute(
        self,
        message: Any,
        query: str,
        limit: int | str = 8,
        scope: str = "self",
        **kwargs,
    ) -> str:
        memory = getattr(self.bot, "memory", None)
        if memory is None or not hasattr(memory, "rag_search"):
            return "Error: RAG memory is unavailable."

        q = str(query or "").strip()
        if not q:
            return "Error: query is required."
        try:
            top_k = max(1, min(20, int(limit)))
        except (TypeError, ValueError):
            top_k = 8

        author = getattr(message, "author", None)
        user_id = str(getattr(author, "id", "") or "")
        is_admin = False
        checker = getattr(self.bot, "_is_admin", None)
        if callable(checker):
            with contextlib.suppress(Exception):
                is_admin = bool(checker(getattr(author, "id", None)))

        requested_scope = str(scope or "self").strip().lower()
        if requested_scope == "global" and not is_admin:
            return "Error: global cross-server recall is restricted to Maxwell admins."

        rows: list[dict] = []
        try:
            if requested_scope == "global":
                rows = await memory.rag_search(
                    q,
                    kinds=["message", "bot_output", "ltm", "shared", "entity"],
                    top_k=top_k,
                    min_similarity=0.20,
                    apply_recency=False,
                )
            else:
                rows = await memory.rag_search(
                    q,
                    kinds=["message"],
                    author_id=user_id,
                    top_k=top_k,
                    min_similarity=0.20,
                    apply_recency=False,
                )
        except Exception as exc:
            logger.exception("Cross-server rag_search failed")
            return f"Error searching memory: {exc}"

        extras: list[dict] = []
        if requested_scope == "self":
            getter = getattr(memory, "get_entity_facts", None)
            if callable(getter):
                with contextlib.suppress(Exception):
                    extras.extend(
                        await getter(
                            user_id,
                            query=q,
                            top_k=min(6, top_k),
                            budget=6000,
                        )
                    )
            shared_getter = getattr(memory, "get_relevant_shared_context", None)
            if callable(shared_getter):
                guild = getattr(message, "guild", None)
                channel = getattr(message, "channel", None)
                with contextlib.suppress(Exception):
                    extras.extend(
                        await shared_getter(
                            user_id=user_id,
                            guild_id=str(getattr(guild, "id", "") or ""),
                            channel_id=str(getattr(channel, "id", "") or ""),
                            is_dm=guild is None,
                            is_admin=is_admin,
                            max_items=min(6, top_k),
                            budget=6000,
                        )
                    )

        combined: list[dict] = []
        seen: set[str] = set()
        for item in rows + extras:
            if not isinstance(item, dict):
                continue
            content = " ".join(str(item.get("content") or "").split())
            if not content:
                continue
            key = str(item.get("id") or "") + "\0" + content[:200]
            if key in seen:
                continue
            seen.add(key)
            row = dict(item)
            row["content"] = content
            combined.append(row)
            if len(combined) >= top_k:
                break

        if not combined:
            return "No matching cross-server memories found."

        lines = []
        for idx, item in enumerate(combined, start=1):
            meta = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            guild_id = str(item.get("guild_id") or meta.get("guild_id") or "")
            channel_id = str(item.get("channel_id") or meta.get("channel_id") or "")
            where = ""
            if guild_id or channel_id:
                where = f" [guild={guild_id or '?'} channel={channel_id or '?'}]"
            sim = item.get("similarity")
            score = ""
            if isinstance(sim, (int, float)):
                score = f" ({float(sim):.2f})"
            lines.append(
                f"{idx}.{score}{where} {str(item.get('content') or '')[:1800]}"
            )
        return "\n".join(lines)


async def _normalize_audio_to_wav(data: bytes) -> bytes | None:
    """Return <=90s mono/16k WAV so the LLM's audio media contract is honest."""
    if not data:
        return None
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    proc = await asyncio.create_subprocess_exec(
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        "pipe:0",
        "-t",
        "90",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-f",
        "wav",
        "pipe:1",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _stderr = await asyncio.wait_for(proc.communicate(data), timeout=25)
    except asyncio.TimeoutError:
        proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        return None
    if proc.returncode != 0 or not stdout:
        return None
    return stdout[:3_600_000]


class InspectMediaUrlTool(Tool):
    returns_result = True

    def __init__(self, bot: Any):
        self.bot = bot

    def get_name(self) -> str:
        return "inspect_media_url"

    def get_description(self) -> str:
        return (
            "Inspect an image, video, GIF, or audio URL and return the media to the LLM. "
            "Images delegate to see_image, videos to see_video, and direct audio is safely "
            "downloaded and normalized to WAV. This does not post the media back to chat."
        )

    def get_parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "media_type": {
                    "type": "string",
                    "enum": ["auto", "image", "video", "audio"],
                    "default": "auto",
                },
            },
            "required": ["url"],
        }

    async def _audio(self, url: str) -> str:
        try:
            from bot_tools import (
                _get_shared_session,
                _is_safe_url,
                _read_response_limited,
            )
        except Exception as exc:
            return f"Error: media helpers unavailable: {exc}"

        if not _is_safe_url(url):
            return "Error: unsafe or unsupported URL."
        session = await _get_shared_session()
        timeout = aiohttp.ClientTimeout(total=20)
        try:
            async with session.get(
                url,
                allow_redirects=True,
                timeout=timeout,
                headers={"User-Agent": "Maxwell-bot/1.0"},
            ) as resp:
                if resp.status < 200 or resp.status >= 300:
                    return f"Error: audio fetch returned HTTP {resp.status}."
                final_url = str(resp.url)
                if not _is_safe_url(final_url):
                    return "Error: redirect target is unsafe."
                raw = await _read_response_limited(resp, 3_200_000)
        except Exception as exc:
            return f"Error fetching audio: {exc}"

        wav = await _normalize_audio_to_wav(raw)
        if not wav:
            return "Error: could not normalize audio (ffmpeg is required)."
        payload = base64.b64encode(wav).decode("ascii")
        return (
            "Audio loaded for model inspection (first 90 seconds).\n"
            f"__AUDIO_B64__{payload}__END_AUDIO_B64__"
        )

    async def execute(
        self,
        message: Any,
        url: str,
        media_type: str = "auto",
        **kwargs,
    ) -> str:
        target = str(url or "").strip()
        if not target.startswith(("http://", "https://")):
            return "Error: provide an http(s) URL."

        kind = str(media_type or "auto").strip().lower()
        path = urlparse(target).path.lower()
        ext = os.path.splitext(path)[1]
        if kind == "auto":
            if ext in _IMAGE_EXTS:
                kind = "image"
            elif ext in _VIDEO_EXTS:
                kind = "video"
            elif ext in _AUDIO_EXTS:
                kind = "audio"
            else:
                kind = "image"

        if kind == "audio":
            return await self._audio(target)

        tool_name = "see_video" if kind == "video" else "see_image"
        tool = (getattr(self.bot, "tools", None) or {}).get(tool_name)
        if tool is None:
            return f"Error: {tool_name} is not enabled."
        try:
            return str(await tool.execute(message, url=target))
        except Exception as exc:
            return f"Error inspecting {kind}: {exc}"


def _read_discord_file(file_obj: Any) -> tuple[str, bytes] | None:
    filename = str(getattr(file_obj, "filename", "") or "generated.png")
    fp = getattr(file_obj, "fp", None)
    if fp is None or not hasattr(fp, "read"):
        return None
    try:
        pos = fp.tell() if hasattr(fp, "tell") else None
    except Exception:
        pos = None
    try:
        if hasattr(fp, "seek"):
            fp.seek(0)
        data = fp.read()
        if not isinstance(data, (bytes, bytearray)):
            return None
        return filename, bytes(data)
    except Exception:
        return None
    finally:
        if pos is not None and hasattr(fp, "seek"):
            with contextlib.suppress(Exception):
                fp.seek(pos)


class _CaptureChannel:
    """Delegate a channel except generated image file sends, which are captured."""

    def __init__(self, real: Any):
        self._real = real
        self.images: list[tuple[str, bytes]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    async def send(self, *args, **kwargs):
        files: list[Any] = []
        if kwargs.get("file") is not None:
            files.append(kwargs["file"])
        if kwargs.get("files"):
            files.extend(list(kwargs["files"]))
        captured = []
        for item in files:
            parsed = _read_discord_file(item)
            if parsed is None:
                continue
            filename, data = parsed
            ext = os.path.splitext(filename.lower())[1]
            if ext in _IMAGE_EXTS or data.startswith(
                (b"\x89PNG", b"\xff\xd8\xff", b"RIFF", b"GIF8")
            ):
                captured.append((filename, data))
        if captured:
            self.images.extend(captured)
            return SimpleNamespace(id=0, attachments=[], channel=self._real)
        return await self._real.send(*args, **kwargs)


class _MessageProxy:
    def __init__(self, original: Any, channel: _CaptureChannel):
        self._original = original
        self.channel = channel

    def __getattr__(self, name: str) -> Any:
        return getattr(self._original, name)


def patch_image_generators(bot: Any, ctx: Any = None) -> int:
    """Capture generated image sends and feed bytes back through the tool result."""
    patched = 0
    registry = getattr(bot, "tools", None) or {}

    def _wrap(original, tool_name: str):
        async def wrapped_execute(_self, message: Any, *args, **kwargs):
            capture = _CaptureChannel(getattr(message, "channel", None))
            proxy = _MessageProxy(message, capture)
            result = await original(proxy, *args, **kwargs)
            if not capture.images:
                return result
            parts = [
                str(result or f"{tool_name} generated image for model inspection.")
            ]
            for filename, data in capture.images[:4]:
                payload = base64.b64encode(data).decode("ascii")
                if len(payload) >= 4_900_000:
                    parts.append(
                        f"Generated {filename}, but it is too large for inline model inspection."
                    )
                    continue
                parts.append(
                    f"Generated {filename} for internal inspection only.\n"
                    f"__IMAGE_B64__{payload}__END_IMAGE_B64__"
                )
            return "\n".join(parts)

        return wrapped_execute

    for name in ("image_generator", "hd_image"):
        tool = registry.get(name)
        if tool is None or getattr(tool, "_maxwell_llm_image_capture", False):
            continue
        original = getattr(tool, "execute", None)
        if not callable(original):
            continue
        if ctx is not None and hasattr(ctx, "wrap_tool"):
            ctx.wrap_tool(name, lambda orig, _name=name: _wrap(orig, _name))
        else:
            tool.execute = MethodType(_wrap(original, name), tool)
        tool._maxwell_llm_image_capture = True
        patched += 1
    return patched
