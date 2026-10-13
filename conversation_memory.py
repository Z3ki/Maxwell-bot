"""Scoped SQLite conversation history without embeddings or vector retrieval.

Keep the existing database filename/table for in-place upgrades. Legacy facts
and vectors remain on disk for export; this service reads only transcript rows
and never loads, generates, or searches embeddings.
"""

import asyncio
import hashlib
import json
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from maxwell_core.memory.scope import MemoryRequester, _decode_metadata, _has_requester
from media_payloads import sanitize_media_memory

def _is_tool_metadata(meta: dict) -> bool:
    return bool(meta.get("is_tool") or meta.get("tool_name"))


def _is_human_author(author_id: str, meta: dict) -> bool:
    return (
        str(author_id or "").isdigit()
        and not meta.get("author_is_bot")
        and not _is_tool_metadata(meta)
    )


def _opt_out_transcript_plan(rows, uid: str) -> tuple[list[str], list[tuple[str, str]]]:
    """Rows to delete, and other people's rows whose quoted text must be cleared.

    A tool row belongs to this account when it names them or when it sits in
    their turn: after their message and before the next person speaks.
    Maxwell's replies to them are removed. Someone else's own message stays,
    without the verbatim quote of this account.
    """
    grouped: dict[tuple[str, str], list[tuple]] = {}
    for row in rows:
        row_id, channel_id, guild_id, author_id, _kind, _stamp, _created, raw = row
        meta = _decode_metadata(raw)
        grouped.setdefault((str(channel_id or ""), str(guild_id or "")), []).append(
            (str(row_id), str(author_id or ""), meta)
        )
    delete_ids: list[str] = []
    strips: list[tuple[str, str]] = []
    for group in grouped.values():
        user_message_ids = {
            row_id for row_id, author_id, _meta in group if author_id == uid
        }
        involves = bool(user_message_ids) or any(
            str(meta.get("reply_to_author_id") or "") == uid
            or str(meta.get("requested_by") or "") == uid
            or str(meta.get("origin_request_id") or "") in user_message_ids
            for _row_id, _author_id, meta in group
        )
        if not involves:
            continue
        last_human = ""
        for row_id, author_id, meta in group:
            is_tool = _is_tool_metadata(meta)
            is_bot = bool(meta.get("author_is_bot"))
            replied = str(meta.get("reply_to_author_id") or "") == uid
            requested = str(meta.get("requested_by") or "") == uid
            from_user_turn = str(meta.get("origin_request_id") or "") in user_message_ids
            drop = (
                author_id == uid
                or requested
                or from_user_turn
                or (is_tool and (replied or last_human == uid))
                or (is_bot and replied)
            )
            if drop:
                delete_ids.append(row_id)
            elif replied and str(meta.get("reply_to_content") or "").strip():
                cleaned = dict(meta)
                cleaned.pop("reply_to_content", None)
                strips.append((
                    row_id,
                    json.dumps(cleaned, ensure_ascii=False, default=str),
                ))
            if _is_human_author(author_id, meta):
                last_human = author_id
    return list(dict.fromkeys(delete_ids)), strips


def _delete_author_rows(db, author_id: str) -> dict:
    """Remove one account's stored messages, replies, tool rows, and facts.

    Other people's own messages stay. A verbatim quote of this account stored
    on those messages is cleared. ``user_entities`` is removed only when that
    older table is present.
    """
    uid = str(author_id or "").strip()
    empty = {"messages": 0, "facts": 0, "profiles": 0}
    if not uid.isdigit():
        return dict(empty)
    db.execute("BEGIN IMMEDIATE")
    try:
        rows = db.execute(
            "SELECT id, channel_id, guild_id, author_id, kind, timestamp, created_at, metadata "
            "FROM vectors WHERE kind IN ('message', 'bot_output') "
            "ORDER BY channel_id, guild_id, timestamp, created_at, id"
        ).fetchall()
        delete_ids, strips = _opt_out_transcript_plan(rows, uid)
        messages = 0
        for start in range(0, len(delete_ids), 400):
            chunk = delete_ids[start:start + 400]
            marks = ",".join("?" for _ in chunk)
            removed = db.execute(
                f"DELETE FROM vectors WHERE id IN ({marks})",
                chunk,
            ).rowcount
            messages += max(0, int(removed or 0))
        for row_id, metadata in strips:
            db.execute(
                "UPDATE vectors SET metadata=? WHERE id=?",
                (metadata, row_id),
            )
        facts = db.execute(
            "DELETE FROM vectors WHERE author_id=? AND kind IN ('entity', 'ltm')",
            (uid,),
        ).rowcount
        profiles = 0
        if db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='user_entities'"
        ).fetchone():
            profiles = db.execute(
                "DELETE FROM user_entities WHERE user_id=?", (uid,)
            ).rowcount
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise
    return {
        "messages": int(messages or 0),
        "facts": int(facts or 0),
        "profiles": int(profiles or 0),
    }


async def forget_author(memory, user_id: str) -> dict:
    """Delete stored content for one account through the live history store."""
    deleter = getattr(memory, "delete_author_content", None)
    if not callable(deleter):
        raise TypeError(
            "Message storage is off, but stored messages could not be deleted. "
            "Open /config and choose Off again."
        )
    result = deleter(str(user_id))
    if asyncio.iscoroutine(result) or asyncio.isfuture(result):
        result = await result
    return result if isinstance(result, dict) else {}


def delete_author_content_file(path, author_id: str) -> dict:
    """Delete one account's stored content through a short-lived connection."""
    db_path = Path(path)
    if not db_path.is_file():
        return {"messages": 0, "facts": 0, "profiles": 0}
    db = sqlite3.connect(str(db_path), isolation_level=None, timeout=5)
    try:
        return _delete_author_rows(db, author_id)
    finally:
        db.close()


def _tracking_blocks(memory, message: dict) -> bool:
    """True when this row belongs to someone who opted out of storage.

    A bot row is blocked when it replies to that person. A tool row is
    blocked when it records their turn. Another person's own message is
    kept even if it quotes them.
    """
    checker = getattr(memory, "tracks_message_content", None)
    if not callable(checker):
        return False
    subjects = []
    author_id = str(message.get("author_id") or "")
    if author_id.isdigit() and not message.get("author_is_bot"):
        subjects.append(author_id)
    if message.get("author_is_bot"):
        reply_to = str(message.get("reply_to_author_id") or "")
        if reply_to.isdigit():
            subjects.append(reply_to)
    if message.get("is_tool") or message.get("tool_name"):
        requested = str(message.get("requested_by") or "")
        if requested.isdigit():
            subjects.append(requested)
    for uid in subjects:
        try:
            allowed = checker(uid)
        except Exception:
            return True
        if not allowed:
            return True
    return False


_RECALL_STOP_WORDS = frozenset(
    "a an and are as at be been but by can could did do does for from had has have "
    "how i if in is it its just me my of on or our please remember said say should "
    "so some that the their them then there these they this to us was we were what "
    "when where which who why will with would you your about earlier previous recall".split()
)


class ConversationMemoryManager:
    def __init__(self, data_dir: str, max_messages: int = 2000):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.max_messages = max(1, min(int(max_messages), 10000))
        self.db_path = self.data_dir / "maxwell_rag.db"
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="maxwell-history")
        self._closed = False
        self._db = sqlite3.connect(str(self.db_path), isolation_level=None, check_same_thread=False)
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
        self._db.execute("""CREATE INDEX IF NOT EXISTS idx_conversation_cleanup
            ON vectors(channel_id, timestamp) WHERE kind='message'""")
        self._initialize_search()

    def _initialize_search(self):
        # Contentless FTS stores only the index; transcript text stays in vectors.
        self._db.execute("BEGIN IMMEDIATE")
        try:
            exists = self._db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='conversation_search'"
            ).fetchone()
            self._db.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS conversation_search
                USING fts5(scope_key, content, content='', tokenize='unicode61')""")
            if not exists:
                self._db.execute("""INSERT INTO conversation_search(rowid,scope_key,content)
                    SELECT rowid,'g'||COALESCE(guild_id,'')||'c'||channel_id,content
                    FROM vectors WHERE kind IN ('message','bot_output')""")
            self._db.execute("""CREATE TRIGGER IF NOT EXISTS conversation_search_insert
                AFTER INSERT ON vectors WHEN new.kind IN ('message','bot_output')
                BEGIN
                    INSERT INTO conversation_search(rowid,scope_key,content)
                    VALUES(new.rowid,'g'||COALESCE(new.guild_id,'')||'c'||new.channel_id,new.content);
                END""")
            self._db.execute("""CREATE TRIGGER IF NOT EXISTS conversation_search_delete
                AFTER DELETE ON vectors WHEN old.kind IN ('message','bot_output')
                BEGIN
                    INSERT INTO conversation_search(conversation_search,rowid,scope_key,content)
                    VALUES('delete',old.rowid,'g'||COALESCE(old.guild_id,'')||'c'||old.channel_id,old.content);
                END""")
            self._db.execute("""CREATE TRIGGER IF NOT EXISTS conversation_search_update
                AFTER UPDATE OF kind,channel_id,guild_id,content ON vectors
                BEGIN
                    INSERT INTO conversation_search(conversation_search,rowid,scope_key,content)
                    SELECT 'delete',old.rowid,'g'||COALESCE(old.guild_id,'')||'c'||old.channel_id,old.content
                    WHERE old.kind IN ('message','bot_output');
                    INSERT INTO conversation_search(rowid,scope_key,content)
                    SELECT new.rowid,'g'||COALESCE(new.guild_id,'')||'c'||new.channel_id,new.content
                    WHERE new.kind IN ('message','bot_output');
                END""")
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise

    async def _run_db(self, callback, *args):
        if self._closed:
            raise RuntimeError("Conversation history is closed")
        future = asyncio.get_running_loop().run_in_executor(self._executor, partial(callback, *args))
        # Cancelling a caller cannot interrupt a running SQLite transaction.
        return await asyncio.shield(future)

    async def get_channel_memory(self, channel_id: str, *, requester: MemoryRequester | None = None) -> list[dict]:
        if not _has_requester(requester) or str(channel_id) != requester.channel_id:
            return []
        return await self._run_db(self._get_channel_memory, requester)

    def _get_channel_memory(self, requester):
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

    async def get_relevant_channel_memory(
        self, channel_id: str, query: str, *, requester: MemoryRequester | None = None,
        exclude_ids=(), limit: int = 4,
    ) -> list[dict]:
        if not _has_requester(requester) or str(channel_id) != requester.channel_id:
            return []
        words = list(dict.fromkeys(
            word for word in re.findall(r"[^\W_]{2,64}", str(query)[:2000].casefold())
            if word not in _RECALL_STOP_WORDS
        ))[:12]
        if not words:
            return []
        return await self._run_db(
            self._get_relevant_channel_memory, requester, words,
            tuple(str(mid) for mid in exclude_ids), max(1, min(int(limit), 6)),
        )

    def _get_relevant_channel_memory(self, requester, words, excluded, limit):
        guild = "" if requester.is_dm else requester.guild_id
        scope_key = f"g{guild}c{requester.channel_id}".replace('"', '""')
        match = f'scope_key:"{scope_key}" AND content:(' + " OR ".join(
            f'"{word}"' for word in words
        ) + ")"
        exclusion = " AND v.id NOT IN (" + ",".join("?" for _ in excluded) + ")" if excluded else ""
        rows = self._db.execute(
            "SELECT v.id,v.author,v.author_id,v.content,v.timestamp,v.metadata "
            "FROM conversation_search JOIN vectors v ON v.rowid=conversation_search.rowid "
            "WHERE conversation_search MATCH ? AND v.channel_id=? AND v.guild_id=? "
            "AND v.kind IN ('message','bot_output')" + exclusion +
            " ORDER BY bm25(conversation_search,0.0,1.0),v.timestamp DESC,v.created_at DESC LIMIT ?",
            (match, requester.channel_id, guild, *excluded, limit * 4),
        ).fetchall()
        result = []
        for row in rows:
            metadata = _decode_metadata(row["metadata"])
            if metadata.get("is_tool"):
                continue
            result.append(sanitize_media_memory(metadata | {
                "message_id": row["id"], "author": row["author"],
                "author_id": row["author_id"], "content": row["content"],
                "timestamp": row["timestamp"],
            }))
            if len(result) == limit:
                break
        return result

    async def add_to_channel_memory(self, channel_id: str, message: dict) -> None:
        await self._run_db(self._add_to_channel_memory, channel_id, message)

    async def delete_author_content(self, author_id: str) -> dict:
        """Delete transcript rows and facts stored for one Discord account."""
        return await self._run_db(self._delete_author_content, str(author_id))

    def _delete_author_content(self, author_id: str) -> dict:
        return _delete_author_rows(self._db, author_id)

    def _add_to_channel_memory(self, channel_id, message):
        entry = sanitize_media_memory(message)
        if _tracking_blocks(self, entry):
            return
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
        # All database operations use the same worker, including complete transactions.
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
        return await self._run_db(self._merge_message_metadata, message_id, patch)

    def _merge_message_metadata(self, message_id, patch):
        row = self._db.execute("SELECT metadata FROM vectors WHERE id=? AND kind IN ('message','bot_output')", (str(message_id),)).fetchone()
        if row is None:
            return False
        metadata = _decode_metadata(row["metadata"]) | sanitize_media_memory(patch)
        self._db.execute("UPDATE vectors SET metadata=? WHERE id=?", (json.dumps(metadata, ensure_ascii=False, default=str), str(message_id)))
        return True

    async def clear_channel_memory(self, channel_id: str) -> None:
        await self._run_db(self._clear_channel_memory, str(channel_id))

    def _clear_channel_memory(self, channel_id):
        self._db.execute("DELETE FROM vectors WHERE channel_id=? AND kind IN ('message','bot_output')", (channel_id,))

    async def list_recent_channel_ids(self, limit: int = 20) -> list[str]:
        return await self._run_db(self._list_recent_channel_ids, max(1, min(int(limit), 80)))

    def _list_recent_channel_ids(self, limit):
        rows = self._db.execute("SELECT channel_id FROM vectors WHERE kind IN ('message','bot_output') AND channel_id!='' GROUP BY channel_id ORDER BY MAX(created_at) DESC LIMIT ?", (limit,)).fetchall()
        return [row["channel_id"] for row in rows]

    async def cleanup_stale_channels(self, cutoff: datetime) -> int:
        return await self._run_db(self._cleanup_stale_channels, cutoff)

    def _cleanup_stale_channels(self, cutoff):
        # The covering partial index avoids reading transcript/embedding pages.
        # Keep the existing policy: newest stored user message, not bot output.
        self._db.execute("BEGIN IMMEDIATE")
        try:
            rows = self._db.execute(
                "SELECT channel_id, MAX(timestamp) AS latest FROM vectors "
                "WHERE kind='message' AND channel_id!='' GROUP BY channel_id"
            ).fetchall()
            cleared = 0
            for row in rows:
                try:
                    stale = bool(row["latest"]) and datetime.fromisoformat(row["latest"]) < cutoff
                except (TypeError, ValueError):
                    continue
                if stale:
                    self._clear_channel_memory(row["channel_id"])
                    cleared += 1
            self._db.execute("COMMIT")
            return cleared
        except BaseException:
            self._db.execute("ROLLBACK")
            raise

    def get_long_term_memory(self, *args, **kwargs) -> list[dict]:
        return []

    async def get_relevant_shared_context(self, *args, **kwargs) -> list[dict]:
        return []

    def load_from_disk(self) -> None:
        """History is queried directly; nothing is loaded into a global cache."""

    async def flush(self) -> None:
        if self._closed:
            return
        self._closed = True
        # FIFO executor ordering drains accepted work before closing the connection.
        future = asyncio.get_running_loop().run_in_executor(self._executor, self._db.close)
        try:
            await asyncio.shield(future)
        finally:
            self._executor.shutdown(wait=False)
