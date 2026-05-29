"""Memory management for Maxwell Bot"""

import json
import logging
import asyncio
import os
import tempfile
import re
import uuid
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

MAX_MEMORY_CHARS = 1000
MAX_LTM_LINES = 999
MAX_CHANNELS = 25
MAX_SHARED_CONTEXT = 1000
MAX_SHARED_CONTEXT_CHARS = 1200
DEFAULT_REM_EVENT_BUFFER_MAX = 500


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utcnow_iso() -> str:
    return _utcnow().isoformat()


def _atomic_json_write_sync(path: Path, data):
    """Atomic JSON write: temp file -> fsync -> rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _atomic_text_write_sync(path: Path, text: str):
    """Atomic text write: temp file -> fsync -> rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _normalize_ltm_line(content: str) -> str:
    """Keep one memory per physical line so the file stays easy to edit."""
    return " ".join(str(content).split())[:MAX_MEMORY_CHARS]


def _normalize_context_text(content: str) -> str:
    return " ".join(str(content or "").split())[:MAX_SHARED_CONTEXT_CHARS]


def _parse_iso(ts: str) -> Optional[datetime]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _strip_reasoning_text(content: str) -> str:
    text = str(content or "")
    text = re.sub(r"<think\b[^>]*>.*?</think>", "", text, flags=re.IGNORECASE | re.DOTALL)
    return " ".join(text.split())


class RemEventLog:
    """JSON-backed visible event ring for REM assimilation."""

    def __init__(self, data_dir: str, max_events: int = DEFAULT_REM_EVENT_BUFFER_MAX):
        self.data_dir = Path(data_dir)
        self.events_file = self.data_dir / "rem_events.json"
        self.max_events = max(1, int(max_events or DEFAULT_REM_EVENT_BUFFER_MAX))
        self.events = []
        self._lock = asyncio.Lock()
        self._dirty = False
        self._save_task = None

    def load_from_disk(self):
        try:
            if self.events_file.exists():
                data = json.loads(self.events_file.read_text(encoding="utf-8"))
                self.events = self._sanitize_events(data if isinstance(data, list) else [])
        except (json.JSONDecodeError, OSError) as e:
            logger.error(f"Failed to load REM events: {e}")
            self.events = []

    def _sanitize_events(self, events: list) -> list:
        clean = []
        for raw in events:
            if not isinstance(raw, dict):
                continue
            role = str(raw.get("role") or "")
            if role not in {"user", "assistant"}:
                continue
            clean.append({
                "ts": str(raw.get("ts") or _utcnow_iso()),
                "channel_id": str(raw.get("channel_id") or ""),
                "guild_id": str(raw.get("guild_id")) if raw.get("guild_id") is not None else None,
                "user_id": str(raw.get("user_id") or ""),
                "user_name": str(raw.get("user_name") or "")[:120],
                "role": role,
                "content": _strip_reasoning_text(raw.get("content", ""))[:4000],
                "auto_mode": bool(raw.get("auto_mode", False)),
            })
        return clean[-self.max_events:]

    async def _atomic_save(self, snapshot: list):
        try:
            await asyncio.to_thread(_atomic_json_write_sync, self.events_file, snapshot)
        except Exception as e:
            logger.error(f"Failed to save REM events: {e}")

    def _schedule_save(self):
        self._dirty = True
        if self._save_task is not None:
            self._save_task.cancel()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._save_task = loop.call_later(5, self._do_save)

    def _do_save(self):
        if self._dirty:
            self._dirty = False
            snapshot = json.loads(json.dumps(self.events, ensure_ascii=False))
            asyncio.ensure_future(self._atomic_save(snapshot))
        self._save_task = None

    async def record(self, event: dict):
        async with self._lock:
            clean = self._sanitize_events([{**(event or {}), "ts": (event or {}).get("ts") or _utcnow_iso()}])
            if not clean or not clean[0]["content"]:
                return
            self.events.append(clean[0])
            if len(self.events) > self.max_events:
                self.events = self.events[-self.max_events:]
            self._schedule_save()

    async def drain_slice(self, since_ts: str | None = None) -> list:
        since = _parse_iso(since_ts or "")
        async with self._lock:
            if since is None:
                return [dict(e) for e in self.events]
            out = []
            for event in self.events:
                ts = _parse_iso(event.get("ts", ""))
                if ts and ts > since:
                    out.append(dict(event))
            return out

    async def size(self) -> int:
        async with self._lock:
            return len(self.events)

    async def flush(self):
        if self._save_task is not None:
            self._save_task.cancel()
            self._save_task = None
        if self._dirty:
            self._dirty = False
            snapshot = json.loads(json.dumps(self.events, ensure_ascii=False))
            await self._atomic_save(snapshot)


class MemoryManager:
    """Memory manager with async I/O, debounced saves, and atomic writes."""

    def __init__(self, data_dir: str, max_messages: int = 100):
        self.data_dir = Path(data_dir)
        self.max_messages = min(max_messages, 500)
        self.memory_file = self.data_dir / "memory.json"
        self.ltm_file = self.data_dir / "long_term_memory.txt"
        self.shared_context_file = self.data_dir / "shared_context.json"
        self.memory = {}
        self.long_term_memory = []
        self.shared_context = []
        self._lock = asyncio.Lock()
        self._dirty = False
        self._save_task = None
        self._pending_save = None
        self._save_lock = asyncio.Lock()

    def load_from_disk(self):
        try:
            if self.memory_file.exists():
                with open(self.memory_file, "r", encoding="utf-8") as f:
                    self.memory = json.load(f)
                self._prune_short_term_memory()
                logger.info(f"Loaded memory for {len(self.memory)} channels")
        except (json.JSONDecodeError, OSError) as e:
            logger.error(f"Failed to load memory: {e}")
            self.memory = {}

        try:
            if self.ltm_file.exists():
                lines = self.ltm_file.read_text(encoding="utf-8").splitlines()
                self.long_term_memory = [
                    {"id": i + 1, "content": line.strip()}
                    for i, line in enumerate(lines[:MAX_LTM_LINES])
                    if line.strip()
                ]
                logger.info(f"Loaded {len(self.long_term_memory)} long-term memory lines")
        except OSError as e:
            logger.error(f"Failed to load long-term memory: {e}")
            self.long_term_memory = []

        try:
            if self.shared_context_file.exists():
                data = json.loads(self.shared_context_file.read_text(encoding="utf-8"))
                self.shared_context = self._sanitize_shared_context(data if isinstance(data, list) else [])
                logger.info(f"Loaded {len(self.shared_context)} shared context facts")
        except (json.JSONDecodeError, OSError) as e:
            logger.error(f"Failed to load shared context: {e}")
            self.shared_context = []

    async def _atomic_save(self, filepath: Path, data):
        """Thread-safe atomic JSON save."""
        try:
            # Serialize writes. Concurrent atomic renames are still a footgun: older
            # snapshots can win the race and resurrect stale memory. Do not "simplify".
            async with self._save_lock:
                await asyncio.to_thread(_atomic_json_write_sync, filepath, data)
        except Exception as e:
            logger.error(f"Failed to save {filepath}: {e}")

    def _prune_short_term_memory(self):
        """Keep short-term memory small before it reaches prompts or disk."""
        if not isinstance(self.memory, dict):
            self.memory = {}
            return
        pruned = {}
        for channel_id, messages in self.memory.items():
            if isinstance(messages, list):
                pruned[str(channel_id)] = messages[-self.max_messages:]

        def latest_ts(item):
            messages = item[1]
            if not messages:
                return ""
            return str(messages[-1].get("timestamp", ""))

        newest = sorted(pruned.items(), key=latest_ts, reverse=True)[:MAX_CHANNELS]
        self.memory = dict(newest)

    def _schedule_save(self):
        """Debounce: save within 5 seconds of last write"""
        self._dirty = True
        if self._save_task is not None:
            self._save_task.cancel()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._save_task = loop.call_later(5, self._do_save)

    def _do_save(self):
        """Actually save if dirty — snapshot data so we don't race mutations."""
        if self._dirty:
            self._dirty = False
            snapshot = json.loads(json.dumps(self.memory, ensure_ascii=False))
            # Track the save task so flush() can wait for it
            self._pending_save = asyncio.ensure_future(self._atomic_save(self.memory_file, snapshot))
        self._save_task = None

    async def flush(self):
        """Flush any pending memory save immediately. Call on shutdown."""
        if self._save_task is not None:
            self._save_task.cancel()
            self._save_task = None
        # Wait for any in-flight save
        if hasattr(self, '_pending_save') and self._pending_save is not None:
            try:
                await self._pending_save
            except Exception:
                pass
            self._pending_save = None
        self._save_lock = asyncio.Lock()
        if self._dirty:
            self._dirty = False
            snapshot = json.loads(json.dumps(self.memory, ensure_ascii=False))
            await self._atomic_save(self.memory_file, snapshot)
            logger.info("Memory flushed to disk")

    async def _save_ltm(self):
        """Save long-term memory as one editable text file."""
        lines = [_normalize_ltm_line(entry.get("content", "")) for entry in self.long_term_memory]
        lines = [line for line in lines if line][:MAX_LTM_LINES]
        text = "\n".join(lines)
        if text:
            text += "\n"
        await asyncio.to_thread(_atomic_text_write_sync, self.ltm_file, text)
        self.long_term_memory = [
            {"id": i + 1, "content": line} for i, line in enumerate(lines)
        ]

    def _sanitize_shared_context(self, entries: list) -> list:
        now = _utcnow()
        sanitized = []
        for raw in entries:
            if not isinstance(raw, dict):
                continue
            content = _normalize_context_text(raw.get("content", ""))
            if not content:
                continue
            expires_at = str(raw.get("expires_at") or "").strip()
            if expires_at:
                expiry = _parse_iso(expires_at)
                if expiry and expiry <= now:
                    continue
            scope = str(raw.get("scope") or "global").strip()[:80]
            visibility = str(raw.get("visibility") or "shared").strip()[:32]
            if visibility not in {"private", "shared", "admin_only", "public_hint"}:
                visibility = "shared"
            try:
                importance = int(raw.get("importance", 5))
            except (TypeError, ValueError):
                importance = 5
            tags = raw.get("tags", [])
            if isinstance(tags, str):
                tags = [tags]
            if not isinstance(tags, list):
                tags = []
            created_at = str(raw.get("created_at") or _utcnow_iso())
            last_seen_at = str(raw.get("last_seen_at") or created_at)
            sanitized.append({
                "id": str(raw.get("id") or uuid.uuid4().hex[:10])[:32],
                "scope": scope,
                "visibility": visibility,
                "importance": max(1, min(importance, 10)),
                "content": content,
                "source_user_id": str(raw.get("source_user_id") or "")[:64],
                "source_channel_id": str(raw.get("source_channel_id") or "")[:64],
                "source_guild_id": str(raw.get("source_guild_id") or "")[:64],
                "source_kind": str(raw.get("source_kind") or "unknown")[:32],
                "tags": [str(t).strip()[:32] for t in tags if str(t).strip()][:12],
                "created_at": created_at,
                "last_seen_at": last_seen_at,
                "expires_at": expires_at,
            })
        sanitized.sort(key=lambda e: (str(e.get("last_seen_at", "")), str(e.get("created_at", ""))), reverse=True)
        return sanitized[:MAX_SHARED_CONTEXT]

    async def _save_shared_context(self):
        self.shared_context = self._sanitize_shared_context(self.shared_context)
        await asyncio.to_thread(_atomic_json_write_sync, self.shared_context_file, self.shared_context)

    async def add_shared_context(self, entry: dict) -> str:
        if not isinstance(entry, dict):
            return ""
        async with self._lock:
            now = _utcnow_iso()
            clean = self._sanitize_shared_context([{**entry, "created_at": entry.get("created_at") or now, "last_seen_at": entry.get("last_seen_at") or now}])
            if not clean:
                return ""
            new_entry = clean[0]
            # Merge exact duplicate content/scope to avoid noisy repeated facts.
            for existing in self.shared_context:
                if existing.get("scope") == new_entry.get("scope") and existing.get("content", "").lower() == new_entry.get("content", "").lower():
                    existing["last_seen_at"] = now
                    existing["importance"] = max(int(existing.get("importance", 5)), int(new_entry.get("importance", 5)))
                    await self._save_shared_context()
                    return str(existing.get("id"))
            self.shared_context.insert(0, new_entry)
            await self._save_shared_context()
            logger.info(f"Added shared context #{new_entry['id']} scope={new_entry['scope']}")
            return str(new_entry["id"])

    async def remove_shared_context(self, context_id: str) -> bool:
        async with self._lock:
            before = len(self.shared_context)
            self.shared_context = [e for e in self.shared_context if str(e.get("id")) != str(context_id)]
            if len(self.shared_context) < before:
                await self._save_shared_context()
                logger.info(f"Removed shared context #{context_id}")
                return True
        return False

    async def update_shared_context(self, context_id: str, updates: dict) -> bool:
        if not isinstance(updates, dict):
            return False
        async with self._lock:
            for entry in self.shared_context:
                if str(entry.get("id")) == str(context_id):
                    allowed = {"scope", "visibility", "importance", "content", "tags", "expires_at"}
                    for key, value in updates.items():
                        if key in allowed:
                            entry[key] = value
                    entry["last_seen_at"] = _utcnow_iso()
                    self.shared_context = self._sanitize_shared_context(self.shared_context)
                    await self._save_shared_context()
                    logger.info(f"Updated shared context #{context_id}")
                    return True
        return False

    async def list_shared_context(self, limit: int = 200) -> list:
        async with self._lock:
            self.shared_context = self._sanitize_shared_context(self.shared_context)
            return [dict(e) for e in self.shared_context[:max(1, min(int(limit or 200), MAX_SHARED_CONTEXT))]]

    async def get_relevant_shared_context(
        self, user_id: str, guild_id: str = "", channel_id: str = "", is_dm: bool = False,
        is_admin: bool = False, max_items: int = 10, budget: int = 5000,
    ) -> list:
        user_id = str(user_id or "")
        guild_id = str(guild_id or "")
        channel_id = str(channel_id or "")
        scopes = {"global"}
        if user_id:
            scopes.add(f"user:{user_id}")
            if is_dm:
                scopes.add(f"dm:{user_id}")
        if guild_id:
            scopes.add(f"guild:{guild_id}")
        if channel_id:
            scopes.add(f"channel:{channel_id}")
        async with self._lock:
            self.shared_context = self._sanitize_shared_context(self.shared_context)
            candidates = []
            for entry in self.shared_context:
                visibility = entry.get("visibility", "shared")
                scope = entry.get("scope", "global")
                if visibility == "admin_only" and not is_admin:
                    continue
                if visibility == "private" and not (is_admin or scope in {f"user:{user_id}", f"dm:{user_id}", f"channel:{channel_id}"}):
                    continue
                if scope not in scopes:
                    continue
                candidates.append(dict(entry))

        def score(entry: dict):
            scope = entry.get("scope", "")
            exact = 0
            if scope == f"user:{user_id}":
                exact = 4
            elif scope == f"channel:{channel_id}":
                exact = 3
            elif scope == f"guild:{guild_id}":
                exact = 2
            elif scope == "global":
                exact = 1
            ts = _parse_iso(entry.get("last_seen_at", "")) or _parse_iso(entry.get("created_at", "")) or datetime.fromtimestamp(0, timezone.utc)
            return (exact, int(entry.get("importance", 5)), ts.timestamp())

        candidates.sort(key=score, reverse=True)
        selected = []
        used = 0
        for entry in candidates:
            line_len = len(entry.get("content", "")) + len(entry.get("scope", "")) + 20
            if selected and used + line_len > max(1000, min(int(budget or 5000), 20000)):
                break
            selected.append(entry)
            used += line_len
            if len(selected) >= max(1, min(int(max_items or 10), 50)):
                break
        return selected

    async def get_channel_memory(self, channel_id: str) -> list:
        async with self._lock:
            return list(self.memory.get(channel_id, []))

    async def add_to_channel_memory(self, channel_id: str, message: dict):
        async with self._lock:
            if channel_id not in self.memory:
                self.memory[channel_id] = []

            message["timestamp"] = datetime.now(timezone.utc).isoformat()
            self.memory[channel_id].append(message)

            if len(self.memory[channel_id]) > self.max_messages:
                self.memory[channel_id] = self.memory[channel_id][-self.max_messages:]

            # Evict oldest channels if over limit
            if len(self.memory) > MAX_CHANNELS:
                self._prune_short_term_memory()

            self._schedule_save()

    async def clear_channel_memory(self, channel_id: str):
        async with self._lock:
            if channel_id in self.memory:
                del self.memory[channel_id]
                self._schedule_save()
                logger.info(f"Cleared memory for channel {channel_id}")

    async def add_long_term_memory(self, content: str) -> str:
        content = _normalize_ltm_line(content)
        if not content:
            return "0"
        async with self._lock:
            self.long_term_memory.append({"id": len(self.long_term_memory) + 1, "content": content})
            if len(self.long_term_memory) > MAX_LTM_LINES:
                self.long_term_memory = self.long_term_memory[-MAX_LTM_LINES:]

            await self._save_ltm()
            next_id = len(self.long_term_memory)
            logger.info(f"Added long-term memory #{next_id}")
            return str(next_id)

    async def edit_long_term_memory(self, memory_id: str, content: str) -> bool:
        content = _normalize_ltm_line(content)
        async with self._lock:
            for entry in self.long_term_memory:
                if str(entry["id"]) == str(memory_id):
                    entry["content"] = content
                    await self._save_ltm()
                    logger.info(f"Edited long-term memory #{memory_id}")
                    return True
        return False

    async def remove_long_term_memory(self, memory_id: str) -> bool:
        async with self._lock:
            before = len(self.long_term_memory)
            self.long_term_memory = [
                m for m in self.long_term_memory if str(m["id"]) != str(memory_id)
            ]
            if len(self.long_term_memory) < before:
                await self._save_ltm()
                logger.info(f"Removed long-term memory #{memory_id}")
                return True
        return False

    def get_long_term_memory(self) -> list:
        return list(self.long_term_memory)

    def get_server_prompt(self, server_id: str) -> Optional[str]:
        prompts_file = self.data_dir / "prompts.json"
        try:
            if prompts_file.exists():
                with open(prompts_file, "r", encoding="utf-8") as f:
                    prompts = json.load(f)
                return prompts.get(server_id)
        except Exception as e:
            logger.error(f"Failed to load server prompts: {e}")
        return None

    def set_server_prompt(self, server_id: str, prompt: str):
        prompts_file = self.data_dir / "prompts.json"
        try:
            prompts_file.parent.mkdir(parents=True, exist_ok=True)
            prompts = {}
            if prompts_file.exists():
                with open(prompts_file, "r", encoding="utf-8") as f:
                    prompts = json.load(f)
            prompts[server_id] = prompt
            with open(prompts_file, "w", encoding="utf-8") as f:
                json.dump(prompts, f, indent=2, ensure_ascii=False)
            logger.info(f"Set prompt for server {server_id}")
        except Exception as e:
            logger.error(f"Failed to set server prompt: {e}")

    def clear_server_prompt(self, server_id: str):
        prompts_file = self.data_dir / "prompts.json"
        try:
            if prompts_file.exists():
                with open(prompts_file, "r", encoding="utf-8") as f:
                    prompts = json.load(f)
                if server_id in prompts:
                    del prompts[server_id]
                with open(prompts_file, "w", encoding="utf-8") as f:
                    json.dump(prompts, f, indent=2, ensure_ascii=False)
                logger.info(f"Cleared prompt for server {server_id}")
        except Exception as e:
            logger.error(f"Failed to clear server prompt: {e}")
