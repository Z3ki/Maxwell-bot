"""Extra Discord-native tools for Maxwell.

This plugin intentionally keeps optional "power tools" out of normal prompt
assembly. The model calls them only when it actually needs cross-server recall,
reminders, rich Discord UI, or URL media inspection.
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
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse

import aiohttp
import discord

from response_guard import strip_internal_context
from tools import Tool
from maxwell_core.memory.scope import MemoryRequester

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
    """Atomic feature records; mutation locks are shared with privacy deletion."""
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = asyncio.Lock()

    def _read_sync(self):
        if not self.path.exists():
            return []
        value = json.loads(self.path.read_text("utf-8"))
        if not isinstance(value, list):
            raise TypeError("Unexpected reminder store")
        return [row for row in value if isinstance(row, dict)]

    def _write_sync(self, rows):
        from utils import _atomic_json_write_sync
        _atomic_json_write_sync(self.path, rows)

    def _add_sync(self, owner_id, channel_id, text, due_at, started_at):
        from utils import FileLock
        with FileLock(self.path, timeout=5):
            from privacy import owned_write_allowed
            if not owned_write_allowed(self.path.parent.parent.parent, owner_id, started_at):
                raise ValueError("Request predates a privacy deletion; submit a new reminder request")
            rows = self._read_sync()
            row = {"id": uuid.uuid4().hex[:12], "owner_id": str(owner_id),
                   "channel_id": str(channel_id), "text": str(text)[:1800],
                   "due_at": float(due_at), "created_at": time.time()}
            rows.append(row)
            rows.sort(key=lambda item: float(item.get("due_at") or 0))
            self._write_sync(rows)
            return dict(row)

    async def add(self, *, owner_id, channel_id, text, due_at):
        from privacy import request_started_at
        started_at = request_started_at()
        async with self._lock:
            return await asyncio.to_thread(self._add_sync, owner_id, channel_id, text, due_at, started_at)

    async def list_for(self, owner_id):
        async with self._lock:
            rows = await asyncio.to_thread(self._read_sync)
        return [dict(row) for row in rows if str(row.get("owner_id") or "") == str(owner_id)]

    def _remove_sync(self, reminder_id, owner_id):
        from utils import FileLock
        with FileLock(self.path, timeout=5):
            rows = self._read_sync()
            kept = [row for row in rows if not (str(row.get("id") or "") == str(reminder_id)
                    and (owner_id is None or str(row.get("owner_id") or "") == str(owner_id)))]
            if len(rows) != len(kept):
                self._write_sync(kept)
                return True
            return False

    async def remove(self, reminder_id, owner_id=None):
        async with self._lock:
            return await asyncio.to_thread(self._remove_sync, reminder_id, owner_id)

    async def due(self, now=None):
        cutoff = float(time.time() if now is None else now)
        async with self._lock:
            rows = await asyncio.to_thread(self._read_sync)
        return [dict(row) for row in rows if float(row.get("due_at") or 0) <= cutoff]


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


# A deleted channel used to be fetched on every 5s tick, which spammed the
# log and stalled the event loop. Back off that channel instead of retrying,
# and drop the reminder once the channel is gone or delivery keeps failing.
_UNAVAILABLE_CHANNEL_UNTIL: dict[str, float] = {}
_REMINDER_FAILURES: dict[str, int] = {}
_UNAVAILABLE_CHANNEL_BACKOFF_SECONDS = 900.0
_MAX_REMINDER_FAILURES = 8


async def _drop_reminder(store: ReminderStore, rid: str, channel_id: str, why: str) -> None:
    logger.warning("Dropping reminder %s for channel %s: %s", rid, channel_id, why)
    await store.remove(rid)
    _REMINDER_FAILURES.pop(rid, None)


async def deliver_due_reminders(bot: Any, store: ReminderStore) -> None:
    now = time.time()
    for item in await store.due():
        rid = str(item.get("id") or "")
        channel_id = str(item.get("channel_id") or "")
        owner_id = str(item.get("owner_id") or "")
        if not rid or not channel_id:
            continue
        if now < _UNAVAILABLE_CHANNEL_UNTIL.get(channel_id, 0.0):
            continue
        channel = None
        with contextlib.suppress(Exception):
            channel = bot.get_channel(int(channel_id))
        if channel is None:
            try:
                channel = await bot.fetch_channel(int(channel_id))
            except discord.NotFound:
                await _drop_reminder(store, rid, channel_id, "channel no longer exists")
                continue
            except discord.Forbidden:
                channel = None
            except Exception:
                logger.exception("Reminder %s channel lookup failed", rid)
                channel = None
        if channel is None:
            failures = _REMINDER_FAILURES.get(rid, 0) + 1
            _REMINDER_FAILURES[rid] = failures
            if failures >= _MAX_REMINDER_FAILURES:
                await _drop_reminder(
                    store, rid, channel_id, f"unavailable after {failures} tries"
                )
                continue
            _UNAVAILABLE_CHANNEL_UNTIL[channel_id] = (
                now + _UNAVAILABLE_CHANNEL_BACKOFF_SECONDS
            )
            logger.warning("Reminder %s channel %s is unavailable", rid, channel_id)
            continue
        _UNAVAILABLE_CHANNEL_UNTIL.pop(channel_id, None)
        text = strip_internal_context(str(item.get("text") or ""))
        if not text.strip():
            await store.remove(rid)
            continue
        text = text[:1800]
        try:
            await channel.send(
                f"<@{owner_id}> reminder: {text}",
                allowed_mentions=discord.AllowedMentions(
                    everyone=False, roles=False, users=True, replied_user=False
                ),
            )
        except discord.Forbidden:
            await _drop_reminder(store, rid, channel_id, "missing permission to send")
            continue
        except Exception:
            failures = _REMINDER_FAILURES.get(rid, 0) + 1
            _REMINDER_FAILURES[rid] = failures
            if failures >= _MAX_REMINDER_FAILURES:
                await _drop_reminder(
                    store, rid, channel_id, f"send failed {failures} times"
                )
                continue
            _UNAVAILABLE_CHANNEL_UNTIL[channel_id] = (
                now + _UNAVAILABLE_CHANNEL_BACKOFF_SECONDS
            )
            logger.exception("Failed delivering reminder %s", rid)
            continue
        _REMINDER_FAILURES.pop(rid, None)
        await store.remove(rid)


class SendRichMessageTool(Tool):
    """Send Discord embeds or Components V2 layouts."""

    # This is a reply, same as send_message. It used to be a "[returns
    # nothing]" side tool, so the model treated it as decoration, never called
    # it, and the dispatcher skipped it whenever it followed a send_message.
    returns_result = False
    ends_turn = True
    produces_visible_output = True

    def __init__(self, bot: Any):
        self.bot = bot

    def get_name(self) -> str:
        return "send_rich_message"

    def get_description(self) -> str:
        return (
            "Reply with a Discord embed card. Use it as the reply in place of send_message "
            "when the answer has structure: a comparison or specs (fields replace tables, "
            "inline fields sit side by side), steps or a ranked list, a status or results "
            "summary, a link you built or hosted (link buttons), or an image to show "
            "(image_url). Keep chat, jokes and short answers in send_message. It can share "
            "a batch with send_message, e.g. a short line plus the card. Title or description "
            "is required; description takes Discord markdown. Falls back to plain text when "
            "embeds are not allowed."
        )

    def get_parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Card title, max 256 chars"},
                "description": {
                    "type": "string",
                    "description": "Main body, Discord markdown, max 4096 chars",
                },
                "fields": {
                    "oneOf": [
                        {
                            "type": "array",
                            "maxItems": 25,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "value": {"type": "string"},
                                    "inline": {"type": "boolean"},
                                },
                                "required": ["name", "value"],
                            },
                        },
                        {"type": "string", "description": "JSON array of fields"},
                    ],
                    "description": "Labelled sections. inline=true puts up to 3 on one row",
                },
                "color": {
                    "type": "string",
                    "description": "Six-digit hex accent, e.g. 5865F2",
                },
                "footer": {"type": "string"},
                "image_url": {
                    "type": "string",
                    "description": "Large image, http(s) only (e.g. a generated image URL)",
                },
                "thumbnail_url": {"type": "string", "description": "Small corner image, http(s)"},
                "buttons": {
                    "oneOf": [
                        {
                            "type": "array",
                            "maxItems": 5,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "label": {"type": "string"},
                                    "url": {"type": "string"},
                                    "emoji": {"type": "string"},
                                },
                            },
                        },
                        {"type": "string", "description": "JSON array of buttons"},
                    ],
                    "description": "Link buttons: {label, url, emoji?}",
                },
                "mode": {
                    "type": "string",
                    "enum": ["embed", "components_v2", "auto"],
                    "default": "auto",
                    "description": "auto and embed send an embed. components_v2 sends a text "
                    "layout with no fields, footer or images",
                },
            },
        }

    @staticmethod
    def _transcript(title: str, description: str, fields: Any, footer: Any,
                    image_url: Any, buttons: Any) -> str:
        """What Maxwell remembers posting. Without it the next turn saw a bare
        embed title, or nothing at all for a layout, and repeated himself."""
        lines = ["[rich message]"]
        if title:
            lines.append(f"**{title}**")
        if description:
            lines.append(description)
        lines.extend(
            f"{item.get('name') or ''}: {item.get('value') or ''}"
            for item in _json_list(fields)[:25]
        )
        if footer:
            lines.append(str(footer))
        if image_url and str(image_url).startswith(("http://", "https://")):
            lines.append(f"image: {image_url}")
        for item in _json_list(buttons)[:5]:
            target = str(item.get("url") or "").strip()
            if target:
                lines.append(f"button {item.get('label') or 'Open'}: {target}")
        return "\n".join(lines)[:4000]

    async def _delivered(self, message: Any, sent: Any, text: str) -> None:
        """Same bookkeeping send_gif does: mark the request delivered and put
        the post in memory. Skipping it left the request journal convinced
        nothing was sent and the transcript missing the answer."""
        record = getattr(self.bot, "_record_delivery", None)
        if callable(record) and sent is not None:
            try:
                await record(message, sent)
            except Exception as exc:
                logger.warning("Could not record rich message delivery (%s)", type(exc).__name__)
        remember = getattr(self.bot, "_remember_sent_message", None)
        if callable(remember) and sent is not None:
            try:
                await remember(message, sent, text, message.channel)
            except Exception as exc:
                logger.warning("Could not remember rich message (%s)", type(exc).__name__)

    @staticmethod
    def _link_buttons(raw: Any) -> list[discord.ui.Button]:
        out: list[discord.ui.Button] = []
        for item in _json_list(raw)[:5]:
            label = strip_internal_context(str(item.get("label") or "Open"))[:80]
            if not label.strip():
                continue
            url = strip_internal_context(str(item.get("url") or "")).strip()
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
    ) -> tuple[bool, Any]:
        """(sent, message). An adapter may return None for a message that did
        go out, so success is its own flag."""
        layout_cls = getattr(discord.ui, "LayoutView", None)
        container_cls = getattr(discord.ui, "Container", None)
        text_cls = getattr(discord.ui, "TextDisplay", None)
        row_cls = getattr(discord.ui, "ActionRow", None)
        if not all((layout_cls, container_cls, text_cls, row_cls)):
            return False, None

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
        except (AttributeError, TypeError, ValueError):
            logger.warning("Components V2 layout unavailable; falling back to embed")
            return False, None

        try:
            sent = await message.channel.send(
                view=view, allowed_mentions=discord.AllowedMentions.none()
            )
        except discord.HTTPException as exc:
            # Only an explicit payload rejection is safe to retry as an embed.
            # Permission errors go straight to the plain-text fallback; an
            # uncertain network/server failure must not duplicate a delivery.
            if exc.status == 400 and exc.code == 50035:
                logger.warning("Components V2 payload rejected; falling back to embed")
                return False, None
            raise
        return True, sent

    def _channel_permissions(self, message: Any) -> Any:
        channel = message.channel
        guild = getattr(message, "guild", None) or getattr(channel, "guild", None)
        member = getattr(guild, "me", None)
        permissions_for = getattr(channel, "permissions_for", None)
        if member is not None and callable(permissions_for):
            return permissions_for(member)
        return None

    @staticmethod
    def _webhook_reply(message: Any) -> bool:
        """User-install replies post through the interaction webhook.

        The channel adapter still reports the real channel's permissions so
        server tools can see them. This send does not use those permissions.
        """
        from user_install import UserInstallChannelAdapter

        return isinstance(getattr(message, "channel", None), UserInstallChannelAdapter)

    async def _send_plain(
        self,
        message: Any,
        *,
        title: str,
        description: str,
        footer: str | None,
        image_url: str | None,
        thumbnail_url: str | None,
        fields: Any,
        buttons: Any,
    ) -> str:
        parts = [f"**{title}**" if title else "", description]
        for item in _json_list(fields):
            name = str(item.get("name") or "\u200b")
            value = str(item.get("value") or "\u200b")
            parts.append(f"**{name}**\n{value}")
        if footer:
            parts.append(str(footer))
        parts.extend(
            str(url)
            for url in (image_url, thumbnail_url)
            if url and str(url).startswith(("https://", "http://"))
        )
        text = strip_internal_context("\n\n".join(part for part in parts if part))
        if not text.strip():
            return "Error: content is empty after safety filtering."
        link_buttons = self._link_buttons(buttons)
        view = None
        if link_buttons:
            view = discord.ui.View(timeout=None)
            for button in link_buttons:
                view.add_item(button)
        # Split for Discord's content limit without truncating the fallback.
        sent = 0
        try:
            for start in range(0, len(text), 2000):
                chunk = text[start : start + 2000]
                posted = await message.channel.send(
                    content=chunk,
                    view=view if start + 2000 >= len(text) else None,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                sent += 1
                await self._delivered(message, posted, chunk)
        except (discord.Forbidden, discord.NotFound):
            if sent:
                return (
                    f"Sent {sent} plain-text fallback messages, but Discord denied "
                    "access to the remaining content. Do not resend the delivered content."
                )
            return (
                "Error: missing permissions or channel access to send this message. "
                "Ask a server admin to check Maxwell's View Channel and Send Messages "
                "permissions (Send Messages in Threads for a thread)."
            )
        return "Sent plain-text fallback message (rich messages were unavailable)."

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
        title_s = strip_internal_context(str(title or "")).strip()
        desc_s = strip_internal_context(str(description or "")).strip()
        footer = strip_internal_context(str(footer or ""))
        image_url = strip_internal_context(str(image_url or ""))
        thumbnail_url = strip_internal_context(str(thumbnail_url or ""))
        fields = [
            {**item,
             "name": strip_internal_context(str(item.get("name") or "")),
             "value": strip_internal_context(str(item.get("value") or ""))}
            for item in _json_list(fields)
        ]
        if not title_s and not desc_s:
            return "Error: title or description is required."

        webhook_reply = self._webhook_reply(message)
        permissions = None if webhook_reply else self._channel_permissions(message)
        is_thread = isinstance(message.channel, discord.Thread) or getattr(
            message.channel, "type", None
        ) in {
            discord.ChannelType.news_thread,
            discord.ChannelType.public_thread,
            discord.ChannelType.private_thread,
        }
        send_permission = "send_messages_in_threads" if is_thread else "send_messages"
        missing = [] if webhook_reply else [
            name
            for name in ("view_channel", send_permission)
            if getattr(permissions, name, None) is False
        ]
        if missing:
            return (
                f"Error: missing permissions to send in this channel: {', '.join(missing)}. "
                "Ask a server admin to update Maxwell's channel permissions."
            )

        plain_args = {
            "title": title_s,
            "description": desc_s,
            "footer": footer,
            "image_url": image_url,
            "thumbnail_url": thumbnail_url,
            "fields": fields,
            "buttons": buttons,
        }
        # The live progress message has to step aside before the card lands.
        self._signal_streaming(message)
        if not webhook_reply and getattr(permissions, "embed_links", None) is False:
            return await self._send_plain(message, **plain_args)

        transcript = self._transcript(title_s, desc_s, fields, footer, image_url, buttons)
        # auto means embed. The V2 layout only carries title, body and buttons,
        # so auto picking it quietly threw away every field, footer and image.
        mode_s = str(mode or "auto").strip().lower()
        if mode_s == "components_v2":
            try:
                sent, posted = await self._send_v2(
                    message,
                    title=title_s,
                    description=desc_s,
                    color=str(color or ""),
                    buttons=buttons,
                )
            except discord.Forbidden:
                return await self._send_plain(message, **plain_args)
            except discord.NotFound:
                return "Error: missing permissions or channel access; the channel is unavailable."
            if sent:
                await self._delivered(message, posted, transcript)
                return "Sent Components V2 rich message."

        embed = discord.Embed(
            title=title_s[:256] or None,
            description=desc_s[:4096] or None,
            colour=_parse_colour(color),
        )
        for item in _json_list(fields)[:25]:
            name = str(item.get("name") or "\u200b")[:256]
            value = str(item.get("value") or "\u200b")[:1024]
            inline = item.get("inline") is True or str(item.get("inline")).lower() == "true"
            embed.add_field(name=name, value=value, inline=inline)
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
        if len(embed) > 6000:
            return await self._send_plain(message, **plain_args)
        try:
            posted = await message.channel.send(
                embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none()
            )
        except discord.Forbidden:
            return await self._send_plain(message, **plain_args)
        except discord.NotFound:
            return "Error: missing permissions or channel access; the channel is unavailable."
        await self._delivered(message, posted, transcript)
        return "Sent rich embed message."


class RecallCrossServerMemoryTool(Tool):
    returns_result = True

    def __init__(self, bot: Any):
        self.bot = bot

    def get_name(self) -> str:
        return "recall_cross_server_memory"

    def get_description(self) -> str:
        return (
            "Search memories explicitly instead of adding more history to every response. "
            "The self scope uses only memories visible in this message's context. Maxwell "
            "admins may request global scope, which searches only operator-approved public "
            "facts and never exposes server conversations or private user memories."
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
                    kinds=["ltm", "shared_context"],
                    requester=MemoryRequester.from_message(
                        message, is_admin=is_admin
                    ),
                    top_k=top_k,
                    min_similarity=0.20,
                    apply_recency=False,
                    global_public_only=True,
                )
            else:
                rows = await memory.rag_search(
                    q,
                    kinds=["message"],
                    author_id=user_id,
                    requester=MemoryRequester.from_message(
                        message, is_admin=is_admin
                    ),
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
                            requester=MemoryRequester.from_message(
                                message, is_admin=is_admin
                            ),
                        )
                    )
            shared_getter = getattr(memory, "get_relevant_shared_context", None)
            if callable(shared_getter):
                guild = getattr(message, "guild", None)
                channel = getattr(message, "channel", None)
                with contextlib.suppress(Exception):
                    extras.extend(
                        await shared_getter(
                            requester=MemoryRequester.from_message(
                                message, is_admin=is_admin
                            ),
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
                _fetch_public_url,
                _is_safe_url,
            )
        except Exception as exc:
            return f"Error: media helpers unavailable: {exc}"

        if not _is_safe_url(url):
            return "Error: unsafe or unsupported URL."
        try:
            _final_url, _content_type, raw = await _fetch_public_url(
                url, max_bytes=3_200_000, timeout=20
            )
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
        authorize = getattr(self.bot, "_authorize_tool_execution", None)
        if not callable(authorize):
            return "refused: tool authorization is unavailable"
        try:
            denied = authorize(message, tool_name, tool, {"url": target})
        except Exception:
            return "refused: tool authorization could not be verified"
        if denied:
            return str(denied)
        try:
            return str(await tool.execute(message, url=target))
        except Exception as exc:
            return f"Error inspecting {kind}: {exc}"


