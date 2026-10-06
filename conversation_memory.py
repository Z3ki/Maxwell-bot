"""Scoped SQLite conversation history without embeddings or vector retrieval.

Keep the existing database filename/table for in-place upgrades. Legacy facts
and vectors remain on disk for export; this service reads only transcript rows
and never loads, generates, or searches embeddings.
"""

import asyncio
import hashlib
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from maxwell_core.memory.scope import MemoryRequester, _decode_metadata, _has_requester
from media_payloads import sanitize_media_memory


class ConversationMemoryManager:
    def __init__(self, data_dir: str, max_messages: int = 2000):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.max_messages = max(1, min(int(max_messages), 10000))
        self.db_path = self.data_dir / "maxwell_rag.db"
        self._lock = asyncio.Lock()
        self._db = sqlite3.connect(str(self.db_path), isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute("PRAGMA cache_size=-8000")
        self._db.execute("""CREATE TABLE IF NOT EXISTS vectors (
            id TEXT PRIMARY KEY, kind TEXT NOT NULL,
            channel_id TEXT DEFAULT '', guild_id TEXT DEFAULT '',
            author TEXT DEFAULT '', author_id TEXT DEFAULT '',
            source TEXT DEFAULT 'user', content TEXT NOT NULL,
            content_hash TEXT DEFAULT '', embedding BLOB,
            metadata TEXT DEFAULT '{}', scope TEXT DEFAULT '',
            importance INTEGER DEFAULT 0, parent_id TEXT DEFAULT '',
            chunk_index INTEGER DEFAULT 0, downvotes INTEGER DEFAULT 0,
            timestamp TEXT NOT NULL, created_at REAL NOT NULL,
            updated_at REAL DEFAULT 0)""")
        columns = {row["name"] for row in self._db.execute("PRAGMA table_info(vectors)")}
        for name, declaration in (
            ("guild_id", "TEXT DEFAULT ''"), ("source", "TEXT DEFAULT 'user'"),
            ("content_hash", "TEXT DEFAULT ''"), ("updated_at", "REAL DEFAULT 0"),
        ):
            if name not in columns:
                self._db.execute(f"ALTER TABLE vectors ADD COLUMN {name} {declaration}")
        self._db.execute("""CREATE INDEX IF NOT EXISTS idx_conversation_history
            ON vectors(channel_id, guild_id, timestamp DESC, created_at DESC)
            WHERE kind IN ('message', 'bot_output')""")

    async def get_channel_memory(self, channel_id: str, *, requester: MemoryRequester | None = None) -> list[dict]:
        if not _has_requester(requester) or str(channel_id) != requester.channel_id:
            return []
        rows = self._db.execute(
            "SELECT id, author, author_id, content, timestamp, metadata FROM vectors "
            "WHERE channel_id=? AND guild_id=? AND kind IN ('message','bot_output') "
            "ORDER BY timestamp DESC, created_at DESC LIMIT ?",
            (requester.channel_id, "" if requester.is_dm else requester.guild_id, self.max_messages),
        ).fetchall()
        return [sanitize_media_memory(_decode_metadata(row["metadata"]) | {
            "message_id": row["id"], "author": row["author"],
            "author_id": row["author_id"], "content": row["content"],
            "timestamp": row["timestamp"],
        }) for row in reversed(rows)]

    async def add_to_channel_memory(self, channel_id: str, message: dict) -> None:
        entry = sanitize_media_memory(message)
        if str(entry.get("message_type") or "default") not in {"default", "reply"}:
            return
        source = str(entry.get("source") or ("bot" if entry.get("author_is_bot") else "user"))
        if source == "system":
            return
        content = str(entry.get("content") or "")
        mid = str(entry.get("message_id") or uuid4().hex)
        guild = str(entry.get("guild_id") or "")
        channel = str(channel_id)
        kind = "bot_output" if source == "bot" else "message"
        stamp = str(entry.get("timestamp") or datetime.now(timezone.utc).isoformat())
        metadata = {key: value for key, value in entry.items() if key not in {
            "content", "author", "author_id", "message_id", "timestamp", "guild_id",
        }}
        now = time.time()
        async with self._lock:
            # One transaction preserves edit identity and per-scope retention.
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute("DELETE FROM vectors WHERE id=?", (mid,))
                self._db.execute(
                    "INSERT INTO vectors (id,kind,channel_id,guild_id,author,author_id,source,"
                    "content,content_hash,metadata,timestamp,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (mid, kind, channel, guild, str(entry.get("author") or ""),
                     str(entry.get("author_id") or ""), source, content[:8000],
                     hashlib.sha256(content.encode()).hexdigest(),
                     json.dumps(metadata, ensure_ascii=False, default=str), stamp, now, now),
                )
                self._db.execute(
                    "DELETE FROM vectors WHERE id IN (SELECT id FROM vectors "
                    "WHERE channel_id=? AND guild_id=? AND kind IN ('message','bot_output') "
                    "ORDER BY timestamp DESC,created_at DESC LIMIT -1 OFFSET ?)",
                    (channel, guild, self.max_messages),
                )
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    async def merge_message_metadata(self, message_id: str, patch: dict) -> bool:
        async with self._lock:
            row = self._db.execute("SELECT metadata FROM vectors WHERE id=? AND kind IN ('message','bot_output')", (str(message_id),)).fetchone()
            if row is None:
                return False
            metadata = _decode_metadata(row["metadata"]) | sanitize_media_memory(patch)
            self._db.execute("UPDATE vectors SET metadata=? WHERE id=?", (json.dumps(metadata, ensure_ascii=False, default=str), str(message_id)))
            return True

    async def clear_channel_memory(self, channel_id: str) -> None:
        async with self._lock:
            self._db.execute("DELETE FROM vectors WHERE channel_id=? AND kind IN ('message','bot_output')", (str(channel_id),))

    async def list_recent_channel_ids(self, limit: int = 20) -> list[str]:
        rows = self._db.execute("SELECT channel_id FROM vectors WHERE kind IN ('message','bot_output') AND channel_id!='' GROUP BY channel_id ORDER BY MAX(created_at) DESC LIMIT ?", (max(1, min(int(limit), 80)),)).fetchall()
        return [row["channel_id"] for row in rows]

    def get_long_term_memory(self, *args, **kwargs) -> list[dict]:
        return []

    async def get_relevant_shared_context(self, *args, **kwargs) -> list[dict]:
        return []

    def load_from_disk(self) -> None:
        """History is queried directly; nothing is loaded into a global cache."""

    async def flush(self) -> None:
        self._db.close()
