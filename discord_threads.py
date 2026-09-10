"""Discord thread control: spawn a thread and brief the Maxwell that talks there.

Guided-goal used to open a questionnaire thread and wait. That is gone.
``create_thread`` opens a real Discord thread and stores a brief so the same
bot process, on the next turn *in that thread*, is not starting from zero.
``thread_control`` is how main Maxwell (or anyone in the thread) updates that
brief, renames, archives, or lists threads.

Storage is ``DATA_DIR/discord_threads.json``. The live Discord thread is the
channel; this file is only the brief + parent snapshot.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import discord
from discord import Message

from tools import Tool
from utils import FileLock, _atomic_json_write_sync, _load_json_safe, _safe_int

logger = logging.getLogger(__name__)

STORE_NAME = "discord_threads.json"
MAX_THREADS = 200
CONTEXT_MAX = 4000
NOTE_MAX = 1000
NOTES_MAX = 8
SNAPSHOT_MAX = 2500
NAME_MAX = 100
AUTO_ARCHIVE_ALLOWED = (60, 1440, 4320, 10080)
DEFAULT_AUTO_ARCHIVE = 1440

# discord.ChannelType: news_thread=10, public_thread=11, private_thread=12
_THREAD_TYPE_VALUES = frozenset({10, 11, 12})
_THREAD_TYPE_NAMES = frozenset(
    {"news_thread", "public_thread", "private_thread", "ChannelType.public_thread",
     "ChannelType.private_thread", "ChannelType.news_thread"}
)
_FORUM_TYPE_VALUES = frozenset({15})  # ChannelType.forum


def is_discord_thread(channel: Any) -> bool:
    """True for a Discord thread/forum post, including test doubles."""
    if channel is None:
        return False
    typ = getattr(channel, "type", None)
    value = getattr(typ, "value", typ)
    try:
        if int(value) in _THREAD_TYPE_VALUES:
            return True
    except (TypeError, ValueError):
        pass
    name = str(getattr(typ, "name", "") or typ or "")
    if name in _THREAD_TYPE_NAMES or name.endswith("_thread"):
        return True
    cls_name = type(channel).__name__
    if cls_name in {"Thread", "ThreadChannel"}:
        return True
    if getattr(channel, "parent_id", None) and hasattr(channel, "archived"):
        return True
    return False


def is_forum_channel(channel: Any) -> bool:
    typ = getattr(channel, "type", None)
    value = getattr(typ, "value", typ)
    try:
        if int(value) in _FORUM_TYPE_VALUES:
            return True
    except (TypeError, ValueError):
        pass
    name = str(getattr(typ, "name", "") or "")
    return name in {"forum", "ChannelType.forum"}


def sanitize_thread_name(name: str | None) -> str:
    text = re.sub(r"[\r\n\t]+", " ", str(name or ""))
    text = re.sub(r"\s+", " ", text).strip()
    text = text[:NAME_MAX].strip()
    return text or "thread"


def clamp_auto_archive(value: Any) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return DEFAULT_AUTO_ARCHIVE
    return min(AUTO_ARCHIVE_ALLOWED, key=lambda allowed: abs(allowed - n))


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clip(text: Any, limit: int) -> str:
    raw = str(text or "").strip()
    if len(raw) <= limit:
        return raw
    return raw[: limit - 1].rstrip() + "…"


class ThreadStore:
    """Briefs for Discord threads this process opened or was told about."""

    def __init__(self, data_dir: str):
        self.path = Path(data_dir) / STORE_NAME
        self._lock = asyncio.Lock()
        self._cache: dict[str, dict[str, Any]] | None = None

    def load(self) -> dict[str, dict[str, Any]]:
        if self._cache is None:
            self._cache = self._load_sync()
        return self._cache

    def _load_sync(self) -> dict[str, dict[str, Any]]:
        data = _load_json_safe(self.path, dict)
        if not isinstance(data, dict):
            return {}
        threads = data.get("threads", data)
        if not isinstance(threads, dict):
            return {}
        out: dict[str, dict[str, Any]] = {}
        for key, rec in threads.items():
            if isinstance(rec, dict) and key:
                out[str(key)] = rec
        return out

    def _save_sync(self, threads: dict[str, dict[str, Any]]) -> None:
        pruned = self._prune(threads)
        with FileLock(self.path):
            _atomic_json_write_sync(self.path, {"threads": pruned})
        self._cache = pruned

    @staticmethod
    def _prune(threads: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        if len(threads) <= MAX_THREADS:
            return threads
        def sort_key(item: tuple[str, dict[str, Any]]) -> str:
            rec = item[1]
            return str(rec.get("updated_at") or rec.get("created_at") or "")
        keep = sorted(threads.items(), key=sort_key, reverse=True)[:MAX_THREADS]
        return dict(keep)

    def get(self, thread_id: str | None) -> dict[str, Any] | None:
        if not thread_id:
            return None
        rec = self.load().get(str(thread_id))
        return rec if isinstance(rec, dict) else None

    async def remember(
        self,
        thread: Any,
        *,
        parent_message: Any = None,
        context: str = "",
        parent_snapshot: str = "",
        source: str = "create_thread",
        opening: str = "",
    ) -> dict[str, Any]:
        thread_id = str(getattr(thread, "id", "") or "")
        if not thread_id:
            raise ValueError("thread has no id")
        parent = getattr(thread, "parent", None)
        parent_channel = getattr(parent_message, "channel", None) if parent_message is not None else None
        rec = {
            "id": thread_id,
            "name": str(getattr(thread, "name", "") or "")[:NAME_MAX],
            "parent_channel_id": str(
                getattr(thread, "parent_id", None)
                or getattr(parent, "id", None)
                or getattr(parent_channel, "id", "")
                or ""
            ),
            "parent_channel_name": str(
                getattr(parent, "name", None)
                or getattr(parent_channel, "name", "")
                or ""
            ),
            "parent_message_id": str(getattr(parent_message, "id", "") or ""),
            "guild_id": str(
                getattr(getattr(thread, "guild", None), "id", None)
                or getattr(getattr(parent_message, "guild", None), "id", "")
                or ""
            ),
            "created_at": _utcnow(),
            "updated_at": _utcnow(),
            "created_by": str(
                getattr(getattr(parent_message, "author", None), "id", "") or ""
            ),
            "context": _clip(context, CONTEXT_MAX),
            "parent_snapshot": _clip(parent_snapshot, SNAPSHOT_MAX),
            "notes": [],
            "status": "open",
            "source": str(source or "create_thread")[:40],
            "jump_url": str(getattr(thread, "jump_url", "") or ""),
            "opening": _clip(opening, 500),
        }
        async with self._lock:
            threads = dict(self.load())
            prev = threads.get(thread_id)
            if isinstance(prev, dict):
                rec["created_at"] = prev.get("created_at") or rec["created_at"]
                rec["notes"] = list(prev.get("notes") or [])
                if not rec["context"]:
                    rec["context"] = prev.get("context") or ""
                if not rec["parent_snapshot"]:
                    rec["parent_snapshot"] = prev.get("parent_snapshot") or ""
            threads[thread_id] = rec
            await asyncio.to_thread(self._save_sync, threads)
        return rec

    async def update(
        self,
        thread_id: str,
        *,
        context: str | None = None,
        mode: str = "append",
        name: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        tid = str(thread_id)
        async with self._lock:
            threads = dict(self.load())
            rec = dict(threads.get(tid) or {"id": tid, "notes": [], "context": ""})
            rec["id"] = tid
            rec["updated_at"] = _utcnow()
            if name:
                rec["name"] = sanitize_thread_name(name)
            if status:
                rec["status"] = str(status)
            if context is not None:
                text = _clip(context, CONTEXT_MAX if mode == "replace" else NOTE_MAX)
                if mode == "replace":
                    rec["context"] = _clip(context, CONTEXT_MAX)
                elif text:
                    notes = [str(n) for n in (rec.get("notes") or []) if str(n).strip()]
                    notes.append(text)
                    rec["notes"] = notes[-NOTES_MAX:]
            threads[tid] = rec
            await asyncio.to_thread(self._save_sync, threads)
        return rec

    def list_for(
        self,
        *,
        guild_id: str | None = None,
        parent_channel_id: str | None = None,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        rows = []
        for rec in self.load().values():
            if not isinstance(rec, dict):
                continue
            if not include_archived and rec.get("status") == "archived":
                continue
            if guild_id and str(rec.get("guild_id") or "") != str(guild_id):
                continue
            if parent_channel_id and str(rec.get("parent_channel_id") or "") != str(
                parent_channel_id
            ):
                continue
            rows.append(rec)
        rows.sort(key=lambda r: str(r.get("updated_at") or r.get("created_at") or ""), reverse=True)
        return rows

    def prompt_block(self, message: Any) -> str:
        """System-prompt text for a turn that is happening inside a thread."""
        channel = getattr(message, "channel", None)
        if not is_discord_thread(channel):
            return ""
        rec = self.get(str(getattr(channel, "id", "") or ""))
        name = str(getattr(channel, "name", "") or (rec or {}).get("name") or "thread")
        parent = getattr(channel, "parent", None)
        parent_name = str(
            getattr(parent, "name", None)
            or (rec or {}).get("parent_channel_name")
            or ""
        )
        parent_id = str(
            getattr(channel, "parent_id", None)
            or (rec or {}).get("parent_channel_id")
            or ""
        )
        header = f"You are in Discord thread #{name} (id {getattr(channel, 'id', '')}"
        if parent_name:
            header += f", child of #{parent_name}"
        elif parent_id:
            header += f", child of channel {parent_id}"
        header += ")."
        parts = [header]
        if rec and (rec.get("context") or rec.get("notes") or rec.get("parent_snapshot")):
            parts.append(
                "Brief from main Maxwell — this is why the thread exists. "
                "Stay on it. Do not restart from scratch."
            )
            if rec.get("context"):
                parts.append(str(rec["context"]))
            notes = [str(n).strip() for n in (rec.get("notes") or []) if str(n).strip()]
            if notes:
                parts.append("Later briefings:\n" + "\n".join(f"- {n}" for n in notes))
            if rec.get("parent_snapshot"):
                parts.append(
                    "Parent-channel snapshot when this thread was opened:\n"
                    + str(rec["parent_snapshot"])
                )
        else:
            parts.append(
                "No stored brief for this thread. Use the starter message and "
                "visible history; if that is not enough, ask what this thread is "
                "for rather than inventing it."
            )
        parts.append(
            "Stay in this thread. Discord cannot nest threads. Use thread_control "
            "to add context, rename, or archive."
        )
        return "\n".join(parts)


async def snapshot_parent_conversation(
    bot: Any, message: Any, *, limit: int = 12, char_budget: int = SNAPSHOT_MAX
) -> str:
    """Recent parent-channel lines plus the triggering message."""
    lines: list[str] = []
    memory = getattr(bot, "memory", None)
    channel = getattr(message, "channel", None)
    channel_id = str(getattr(channel, "id", "") or "")
    if memory is not None and hasattr(memory, "get_channel_memory") and channel_id:
        try:
            mem = await memory.get_channel_memory(channel_id)
        except Exception:
            mem = []
        for row in (mem or [])[-limit:]:
            if not isinstance(row, dict):
                continue
            who = str(row.get("author") or row.get("author_name") or "?").strip() or "?"
            text = str(row.get("content") or "").strip()
            if text:
                lines.append(f"{who}: {text[:400]}")
    content = str(getattr(message, "content", "") or "").strip()
    who = str(getattr(getattr(message, "author", None), "display_name", "") or "?").strip()
    current = f"{who}: {content}" if content else ""
    if current and (not lines or lines[-1] != current):
        lines.append(current)
    blob = "\n".join(lines)
    return _clip(blob, char_budget)


async def open_discord_thread(
    message: Any,
    *,
    name: str,
    auto_archive: int = DEFAULT_AUTO_ARCHIVE,
    opening: str = "",
) -> Any:
    """Create a public thread from a message, or a forum post."""
    channel = getattr(message, "channel", None)
    if channel is None:
        raise RuntimeError("no channel to thread from")
    if is_discord_thread(channel):
        raise RuntimeError("already inside a thread; Discord cannot nest threads")
    if getattr(message, "guild", None) is None:
        raise RuntimeError("DMs and group chats have no Discord threads")
    clean = sanitize_thread_name(name)
    archive = clamp_auto_archive(auto_archive)
    if is_forum_channel(channel):
        starter = (opening or clean)[:1900] or clean
        created = await channel.create_thread(
            name=clean,
            content=starter,
            auto_archive_duration=archive,
        )
        # discord.py forum create_thread may return (thread, message) or thread
        if isinstance(created, tuple):
            return created[0]
        return created
    if hasattr(message, "create_thread"):
        return await message.create_thread(
            name=clean, auto_archive_duration=archive
        )
    if hasattr(channel, "create_thread"):
        return await channel.create_thread(
            name=clean,
            auto_archive_duration=archive,
            type=discord.ChannelType.public_thread,
            message=message,
        )
    raise RuntimeError("this channel cannot have threads")


async def resolve_thread_channel(bot: Any, message: Any, thread_id: str | None) -> Any:
    """Current thread, or fetch by id."""
    if thread_id:
        tid = str(thread_id).strip()
        try:
            snowflake = int(tid)
        except (TypeError, ValueError):
            return None
        getter = getattr(bot, "get_channel", None)
        channel = getter(snowflake) if callable(getter) else None
        if channel is None:
            fetch = getattr(bot, "fetch_channel", None)
            if callable(fetch):
                channel = await fetch(snowflake)
        return channel
    channel = getattr(message, "channel", None)
    if is_discord_thread(channel):
        return channel
    return None


def can_access_thread(message: Any, channel: Any) -> bool:
    if not is_discord_thread(channel):
        return False
    guild_id = getattr(getattr(message, "guild", None), "id", None)
    target_guild_id = getattr(getattr(channel, "guild", None), "id", None)
    if guild_id is None or str(guild_id) != str(target_guild_id):
        return False
    author = getattr(message, "author", None)
    if author is None:
        return False
    permissions_for = getattr(channel, "permissions_for", None)
    permissions = None
    if callable(permissions_for):
        try:
            permissions = permissions_for(author)
        except Exception:
            return False
        if not getattr(permissions, "view_channel", False):
            return False
    is_private = getattr(channel, "is_private", None)
    if callable(is_private) and is_private():
        if not getattr(permissions, "manage_threads", False):
            uid = getattr(author, "id", None)
            member = getattr(channel, "get_member", None)
            if uid is None or not callable(member) or member(uid) is None:
                return False
    return True


class CreateThreadTool(Tool):
    """Open a Discord thread and hand thread-Maxwell a brief."""

    def get_description(self):
        return (
            "Open a Discord thread and brief the Maxwell that will talk there. "
            "context= is required: goal, decisions so far, constraints, what to do "
            "next — without it thread-you starts cold. Optional: name, message "
            "(opening post), auto_archive (60/1440/4320/10080 minutes). "
            "Cannot run inside an existing thread or in DMs."
        )

    async def execute(
        self,
        message: Message,
        name: str | None = None,
        context: str | None = None,
        opening: str | None = None,
        message_text: str | None = None,
        auto_archive: str | None = None,
        **kwargs,
    ) -> str:
        opening = str(
            opening
            or message_text
            or kwargs.get("text")
            or ""
        ).strip()
        brief = str(context or kwargs.get("brief") or kwargs.get("goal") or "").strip()
        if not brief:
            return (
                "Error: context is required. Tell thread-you what this is about, "
                "what was already decided, and what to do. Without that the thread "
                "starts cold."
            )
        store = getattr(self.bot, "thread_store", None)
        if store is None:
            return "Error: thread store is not available"
        if is_discord_thread(getattr(message, "channel", None)):
            return (
                "Error: already inside a thread. Use thread_control to add context, "
                "rename, or archive. Discord cannot nest threads."
            )
        if getattr(message, "guild", None) is None:
            return "Error: Discord threads only exist in servers, not DMs"
        title = sanitize_thread_name(
            name or kwargs.get("title") or brief[:50] or "thread"
        )
        snapshot = await snapshot_parent_conversation(self.bot, message)
        try:
            thread = await open_discord_thread(
                message,
                name=title,
                auto_archive=_safe_int(auto_archive, DEFAULT_AUTO_ARCHIVE),
                opening=opening,
            )
        except RuntimeError as exc:
            return f"Error: {exc}"
        except discord.Forbidden:
            return (
                "Error: Discord denied creating the thread (need create public "
                "threads, or this channel type cannot have them)"
            )
        except Exception as exc:
            return f"Error creating thread: {type(exc).__name__}: {exc}"[:400]
        rec = await store.remember(
            thread,
            parent_message=message,
            context=brief,
            parent_snapshot=snapshot,
            source="create_thread",
            opening=opening,
        )
        # Forum create_thread already posted `opening` as the starter.
        if opening and not is_forum_channel(getattr(message, "channel", None)):
            try:
                await thread.send(opening[:1900])
            except Exception as exc:
                logger.debug("thread opening post failed: %s", exc)
        url = (
            getattr(thread, "jump_url", None)
            or getattr(thread, "mention", None)
            or rec.get("jump_url")
            or title
        )
        tid = str(getattr(thread, "id", "") or rec.get("id") or "")
        return (
            f"Thread created: {url} (id {tid}). Brief stored — every turn in "
            f"that thread sees it. Use thread_control action=context thread_id={tid} "
            "to add more later."
        )


class ThreadControlTool(Tool):
    """Rename / archive / brief / list Discord threads this bot knows about."""

    def get_description(self):
        return (
            "Control a Discord thread. action: status (default in a thread), "
            "list (default in a channel), context (add/replace the brief), "
            "rename, archive, unarchive. Params: action, thread_id (optional; "
            "defaults to the current thread), context, mode=append|replace "
            "(for context), name (for rename)."
        )

    async def execute(
        self,
        message: Message,
        action: str | None = None,
        thread_id: str | None = None,
        context: str | None = None,
        mode: str | None = None,
        name: str | None = None,
        **kwargs,
    ) -> str:
        store = getattr(self.bot, "thread_store", None)
        if store is None:
            return "Error: thread store is not available"
        if getattr(message, "guild", None) is None:
            return "Error: thread control is only available in servers, not DMs"
        act = str(action or kwargs.get("op") or "").strip().lower()
        if not act:
            act = "status" if is_discord_thread(getattr(message, "channel", None)) else "list"
        tid = str(thread_id or kwargs.get("id") or "").strip() or None
        brief = str(context or kwargs.get("brief") or kwargs.get("text") or "").strip()
        write_mode = str(mode or "append").strip().lower()
        if write_mode not in {"append", "replace"}:
            write_mode = "append"
        new_name = str(name or kwargs.get("title") or "").strip()

        if act in {"list", "ls"}:
            return self._list(store, message)
        try:
            channel = await resolve_thread_channel(self.bot, message, tid)
        except Exception as exc:
            logger.debug("Thread lookup failed: %s", exc)
            return "Error: could not access that Discord thread"
        if not can_access_thread(message, channel):
            return "Error: choose a Discord thread you can access in this server"
        if act in {"status", "info", "show"}:
            return await self._status(store, message, tid)
        if act in {"context", "brief", "update", "add"}:
            if not brief:
                return "Error: context is required for action=context"
            return await self._context(store, message, tid, brief, write_mode)
        if act in {"rename", "name"}:
            if not new_name:
                return "Error: name is required for action=rename"
            return await self._rename(store, message, tid, new_name)
        if act in {"archive", "close"}:
            return await self._set_archived(store, message, tid, True)
        if act in {"unarchive", "open", "reopen"}:
            return await self._set_archived(store, message, tid, False)
        return (
            "Error: unknown action. Use status, list, context, rename, archive, "
            "or unarchive."
        )

    def _list(self, store: ThreadStore, message: Any) -> str:
        guild = getattr(message, "guild", None)
        guild_id = str(getattr(guild, "id", "") or "")
        parent_id = None
        channel = getattr(message, "channel", None)
        if is_discord_thread(channel):
            parent_id = str(getattr(channel, "parent_id", "") or "")
        else:
            parent_id = str(getattr(channel, "id", "") or "")
        if not guild_id:
            return "Error: thread control is only available in servers, not DMs"
        rows = store.list_for(guild_id=guild_id)
        getter = getattr(self.bot, "get_channel", None)
        rows = [
            rec for rec in rows
            if callable(getter)
            and can_access_thread(message, getter(_safe_int(rec.get("id"), 0)))
        ]
        if not rows:
            return "No Discord threads with a stored brief in this server."
        here = []
        elsewhere = []
        for rec in rows[:40]:
            line = self._one_line(rec)
            if parent_id and str(rec.get("parent_channel_id") or "") == parent_id:
                here.append(line)
            else:
                elsewhere.append(line)
        parts = []
        if here:
            parts.append("Threads on this channel:\n" + "\n".join(here))
        if elsewhere:
            parts.append("Other threads in this server:\n" + "\n".join(elsewhere[:20]))
        return "\n\n".join(parts) or "No Discord threads with a stored brief."

    @staticmethod
    def _one_line(rec: dict[str, Any]) -> str:
        name = rec.get("name") or "thread"
        tid = rec.get("id") or "?"
        parent = rec.get("parent_channel_name") or rec.get("parent_channel_id") or ""
        ctx = _clip(rec.get("context") or "", 80)
        status = rec.get("status") or "open"
        extra = f" — {ctx}" if ctx else ""
        where = f" (from #{parent})" if parent else ""
        mark = "" if status == "open" else f" [{status}]"
        return f"  • {name} `{tid}`{where}{mark}{extra}"

    async def _status(self, store: ThreadStore, message: Any, thread_id: str | None) -> str:
        channel = await resolve_thread_channel(self.bot, message, thread_id)
        tid = str(getattr(channel, "id", "") or thread_id or "")
        rec = store.get(tid)
        if rec is None and channel is None:
            return "Error: no thread id and this channel is not a thread"
        name = getattr(channel, "name", None) or (rec or {}).get("name") or tid
        archived = getattr(channel, "archived", None)
        lines = [f"Thread #{name} id {tid}"]
        if archived is not None:
            lines.append("Discord archived: yes" if archived else "Discord archived: no")
        if rec:
            if rec.get("parent_channel_name"):
                lines.append(f"Parent: #{rec['parent_channel_name']}")
            if rec.get("context"):
                lines.append("Brief:\n" + str(rec["context"]))
            notes = [str(n) for n in (rec.get("notes") or []) if str(n).strip()]
            if notes:
                lines.append("Later briefings:\n" + "\n".join(f"- {n}" for n in notes))
            if rec.get("parent_snapshot"):
                lines.append("Parent snapshot:\n" + str(rec["parent_snapshot"])[:1200])
        else:
            lines.append("No stored brief. Use action=context to give thread-you one.")
        return "\n".join(lines)

    async def _context(
        self,
        store: ThreadStore,
        message: Any,
        thread_id: str | None,
        brief: str,
        mode: str,
    ) -> str:
        channel = await resolve_thread_channel(self.bot, message, thread_id)
        tid = str(getattr(channel, "id", "") or thread_id or "")
        if not tid:
            return "Error: pass thread_id or run this inside the thread"
        rec = await store.update(tid, context=brief, mode=mode)
        verb = "replaced" if mode == "replace" else "appended"
        return (
            f"Brief {verb} on thread {tid} ({rec.get('name') or 'thread'}). "
            "The next turn in that thread will see it."
        )

    async def _rename(
        self, store: ThreadStore, message: Any, thread_id: str | None, new_name: str
    ) -> str:
        channel = await resolve_thread_channel(self.bot, message, thread_id)
        if channel is None or not is_discord_thread(channel):
            return "Error: pass thread_id of a live Discord thread, or run this inside it"
        clean = sanitize_thread_name(new_name)
        try:
            await channel.edit(name=clean)
        except discord.Forbidden:
            return "Error: Discord denied renaming the thread"
        except Exception as exc:
            return f"Error renaming thread: {exc}"[:300]
        tid = str(getattr(channel, "id", "") or "")
        await store.update(tid, name=clean)
        return f"Renamed thread {tid} to {clean}"

    async def _set_archived(
        self, store: ThreadStore, message: Any, thread_id: str | None, archived: bool
    ) -> str:
        channel = await resolve_thread_channel(self.bot, message, thread_id)
        if channel is None or not is_discord_thread(channel):
            return "Error: pass thread_id of a live Discord thread, or run this inside it"
        try:
            await channel.edit(archived=archived)
        except discord.Forbidden:
            return "Error: Discord denied changing archive state"
        except Exception as exc:
            return f"Error updating thread: {exc}"[:300]
        tid = str(getattr(channel, "id", "") or "")
        await store.update(tid, status="archived" if archived else "open")
        verb = "Archived" if archived else "Unarchived"
        return f"{verb} thread {tid}"
