"""History remains scoped and durable after the live vector store is retired."""

import asyncio
import os
from pathlib import Path
import subprocess
import sys
import sqlite3
from datetime import datetime, timezone

from conversation_memory import ConversationMemoryManager
from maxwell_core.memory.scope import MemoryRequester


def test_opt_out_deletes_one_account_and_blocks_new_rows(tmp_path):
    async def scenario():
        memory = ConversationMemoryManager(str(tmp_path))
        memory.preferences.set_default("100", "tracking", "on")
        memory.preferences.set_default("200", "tracking", "on")
        try:
            await memory.add_to_channel_memory("c1", {
                "message_id": "keep", "guild_id": "g", "author_id": "200",
                "content": "other person", "timestamp": "2026-10-12T00:00:00+00:00",
            })
            await memory.add_to_channel_memory("c1", {
                "message_id": "drop", "guild_id": "g", "author_id": "100",
                "content": "private words", "timestamp": "2026-10-12T00:00:01+00:00",
            })
            memory.preferences.set_default("100", "tracking", "off")
            memory.tracks_message_content = lambda uid: uid != "100"
            memory._db.execute(
                "INSERT INTO vectors (id, kind, channel_id, guild_id, author, author_id, "
                "content, timestamp, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                ("fact", "entity", "", "", "", "100", "likes tea", "2026-10-12T00:00:02+00:00", 1),
            )
            memory._db.execute(
                "CREATE TABLE user_entities (user_id TEXT PRIMARY KEY, display_names TEXT)"
            )
            memory._db.execute(
                "INSERT INTO user_entities (user_id, display_names) VALUES (?, ?)",
                ("100", "[]"),
            )
            removed = await memory.delete_author_content("100")
            assert removed["messages"] == 1
            assert removed["facts"] == 1
            assert removed["profiles"] == 1
            requester = MemoryRequester("200", "c1", "g", is_admin=True)
            history = await memory.get_channel_memory("c1", requester=requester)
            assert [row["content"] for row in history] == ["other person"]
            assert memory._db.execute(
                "SELECT COUNT(*) FROM vectors WHERE author_id='100'"
            ).fetchone()[0] == 0
            await memory.add_to_channel_memory("c1", {
                "message_id": "bot-reply", "guild_id": "g", "author_id": "999",
                "author_is_bot": True, "reply_to_author_id": "100",
                "content": "echo of private words", "timestamp": "2026-10-12T00:00:03+00:00",
            })
            history = await memory.get_channel_memory("c1", requester=requester)
            assert [row["message_id"] for row in history] == ["keep"]
            recalled = await memory.get_relevant_channel_memory(
                "c1", "private words", requester=requester,
            )
            assert all("private" not in row["content"] for row in recalled)
        finally:
            await memory.flush()

    asyncio.run(scenario())


def test_opt_out_deletes_replies_quotes_and_tool_rows(tmp_path):
    async def scenario():
        memory = ConversationMemoryManager(str(tmp_path))
        memory.preferences.set_default("100", "tracking", "on")
        memory.preferences.set_default("200", "tracking", "on")
        try:
            await memory.add_to_channel_memory("c1", {
                "message_id": "u1", "guild_id": "g", "author_id": "100", "author": "Ada",
                "content": "my diagnosis is X, what should I do",
                "timestamp": "2026-10-12T00:00:00+00:00",
            })
            await memory.add_to_channel_memory("c1", {
                "message_id": "tool1", "guild_id": "g", "author": "Tool", "author_id": "",
                "is_tool": True, "requested_by": "100",
                "content": 'Called web_search with {"query": "treatment for diagnosis X"} -> ...',
                "timestamp": "2026-10-12T00:00:01+00:00",
            })
            await memory.add_to_channel_memory("c1", {
                "message_id": "b1", "guild_id": "g", "author_id": "999", "author": "Maxwell",
                "author_is_bot": True, "reply_to_author_id": "100",
                "reply_to_content": "my diagnosis is X, what should I do",
                "content": "Ada, for diagnosis X you should ...",
                "timestamp": "2026-10-12T00:00:02+00:00",
            })
            await memory.add_to_channel_memory("c1", {
                "message_id": "other", "guild_id": "g", "author_id": "200", "author": "Bea",
                "content": "unrelated hello",
                "reply_to_author_id": "100",
                "reply_to_content": "my diagnosis is X, what should I do",
                "timestamp": "2026-10-12T00:00:03+00:00",
            })
            await memory.add_to_channel_memory("c1", {
                "message_id": "tool-other", "guild_id": "g", "author": "Tool",
                "is_tool": True, "requested_by": "200", "content": "Called weather with {}",
                "timestamp": "2026-10-12T00:00:04+00:00",
            })
            await memory.add_to_channel_memory("c1", {
                "message_id": "tool-named", "guild_id": "g", "author": "Tool",
                "is_tool": True, "requested_by": "100",
                "content": "Called later with diagnosis X",
                "timestamp": "2026-10-12T00:00:05+00:00",
            })
            removed = await memory.delete_author_content("100")
            assert removed["messages"] == 4
            requester = MemoryRequester("200", "c1", "g", is_admin=True)
            history = await memory.get_channel_memory("c1", requester=requester)
            assert [row["message_id"] for row in history] == ["other", "tool-other"]
            assert all("diagnosis" not in row["content"] for row in history)
            assert all(not row.get("reply_to_content") for row in history)
            recalled = await memory.get_relevant_channel_memory(
                "c1", "diagnosis", requester=requester,
            )
            assert recalled == []
            memory.tracks_message_content = lambda uid: uid != "100"
            await memory.add_to_channel_memory("c1", {
                "message_id": "bot-again", "guild_id": "g", "author_id": "999",
                "author_is_bot": True, "reply_to_author_id": "100",
                "reply_to_content": "my diagnosis is X, what should I do",
                "content": "still quoting the diagnosis",
                "timestamp": "2026-10-12T00:00:06+00:00",
            })
            await memory.add_to_channel_memory("c1", {
                "message_id": "tool-again", "guild_id": "g", "author": "Tool",
                "is_tool": True, "requested_by": "100",
                "content": "Called again with diagnosis X",
                "timestamp": "2026-10-12T00:00:07+00:00",
            })
            history = await memory.get_channel_memory("c1", requester=requester)
            assert [row["message_id"] for row in history] == ["other", "tool-other"]
        finally:
            await memory.flush()

    asyncio.run(scenario())


def test_history_edits_and_retention_never_cross_guilds_or_channels(tmp_path):
    async def scenario():
        memory = ConversationMemoryManager(str(tmp_path), max_messages=2)
        try:
            for guild, channel, mids in [("g1", "c1", [1, 2, 3]), ("g2", "c1", [4]), ("g1", "c2", [5])]:
                for mid in mids:
                    await memory.add_to_channel_memory(channel, {
                        "message_id": str(mid), "guild_id": guild, "content": "same text",
                        "author_id": "u1", "timestamp": f"2026-10-06T12:00:0{mid}+00:00",
                    })
            requester = MemoryRequester("u1", "c1", "g1", is_admin=True)
            history = await memory.get_channel_memory("c1", requester=requester)
            assert [m["message_id"] for m in history] == ["2", "3"]
            await memory.add_to_channel_memory("c1", {
                "message_id": "2", "guild_id": "g1", "content": "edited",
                "timestamp": "2026-10-06T12:00:02+00:00",
            })
            assert [m["content"] for m in await memory.get_channel_memory("c1", requester=requester)] == ["edited", "same text"]
            assert await memory.get_channel_memory("c2", requester=requester) == []
            assert await memory.get_channel_memory("c1") == []
            assert not hasattr(memory, "rag_search")
            assert not hasattr(memory, "start_embedding_recovery_worker")
            assert not hasattr(memory, "_embed_session")
            assert memory._db.execute("SELECT COUNT(*) FROM vectors WHERE embedding IS NOT NULL").fetchone()[0] == 0
        finally:
            await memory.flush()
        restored = ConversationMemoryManager(str(tmp_path), max_messages=2)
        assert [m["content"] for m in await restored.get_channel_memory("c1", requester=requester)] == ["edited", "same text"]
        await restored.flush()

    asyncio.run(scenario())


def test_production_bot_starts_without_importing_numpy_or_legacy_rag(tmp_path):
    root = Path(__file__).resolve().parents[1]
    env_file = tmp_path / "settings.env"
    env_file.write_text(f"DISCORD_BOT_TOKEN=offline-test-token\nAI_MODEL=offline-chat-model\nDATA_DIR={tmp_path}/data\n")
    code = """
import sys
class BlockLegacy:
    def find_spec(self, fullname, *args):
        if fullname == 'numpy' or fullname.startswith('numpy.'):
            raise AssertionError('retired dependency imported: ' + fullname)
sys.meta_path.insert(0, BlockLegacy())
from bot import MaxwellBot
from config import Config
from conversation_memory import ConversationMemoryManager
bot = MaxwellBot()
assert isinstance(bot.memory, ConversationMemoryManager)
assert 'recall_cross_server_memory' not in bot.tools
print('History-only runtime ready')
"""
    result = subprocess.run([sys.executable, "-c", code], cwd=root, env={**os.environ, "MAXWELL_ENV_FILE": str(env_file)}, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert "History-only runtime ready" in result.stdout


def test_upgrade_preserves_transcripts_and_archived_vectors_without_retrieval(tmp_path):
    async def scenario():
        legacy = ConversationMemoryManager(str(tmp_path))
        await legacy.add_to_channel_memory("channel", {
            "message_id": "old-message", "guild_id": "guild", "content": "existing conversation",
        })
        legacy._db.execute("UPDATE vectors SET embedding=? WHERE id=?", (b"archived-vector", "old-message"))
        legacy._db.execute("INSERT INTO vectors(id,kind,channel_id,guild_id,content,timestamp,created_at,embedding) VALUES(?,?,?,?,?,?,?,?)",
                           ("fact", "ltm", "channel", "guild", "archived fact", "2026-10-06T12:00:00+00:00", 1, b"archived-fact-vector"))
        for trigger in ("conversation_search_insert", "conversation_search_delete", "conversation_search_update"):
            legacy._db.execute(f"DROP TRIGGER {trigger}")
        legacy._db.execute("DROP TABLE conversation_search")
        await legacy.flush()
        memory = ConversationMemoryManager(str(tmp_path))
        try:
            rows = await memory.get_channel_memory("channel", requester=MemoryRequester("user", "channel", "guild"))
            assert [row["content"] for row in rows] == ["existing conversation"]
            assert all("embedding" not in row for row in rows)
            assert memory.get_long_term_memory() == []
            assert memory._db.execute("SELECT embedding FROM vectors WHERE id='fact'").fetchone()[0] == b"archived-fact-vector"
            recalled = await memory.get_relevant_channel_memory(
                "channel", "existing conversation", requester=MemoryRequester("user", "channel", "guild"),
            )
            assert [row["message_id"] for row in recalled] == ["old-message"]
            assert "embedding" not in recalled[0]
        finally:
            await memory.flush()

    asyncio.run(scenario())


def test_cleanup_preserves_recent_invalid_and_archived_rows(tmp_path):
    async def scenario():
        memory = ConversationMemoryManager(str(tmp_path))
        cutoff = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)
        entries = [
            ("stale-user", "stale", "2026-10-10T10:00:00+00:00", "user"),
            ("stale-bot", "stale", "2026-10-11T14:00:00+00:00", "bot"),
            ("recent-user", "recent", "2026-10-11T12:00:00+00:00", "user"),
            ("invalid-user", "invalid", "not-a-date", "user"),
        ]
        try:
            for mid, channel, stamp, source in entries:
                await memory.add_to_channel_memory(channel, {
                    "message_id": mid, "guild_id": "guild", "content": mid,
                    "timestamp": stamp, "source": source,
                })
            with sqlite3.connect(memory.db_path) as db:
                db.execute(
                    "INSERT INTO vectors(id,kind,channel_id,content,timestamp,created_at) "
                    "VALUES ('archive','ltm','stale','archived fact','',0)"
                )
            assert await memory.cleanup_stale_channels(cutoff) == 1
            with sqlite3.connect(memory.db_path) as db:
                remaining = {r[0] for r in db.execute("SELECT id FROM vectors")}
            assert remaining == {"recent-user", "invalid-user", "archive"}
        finally:
            await memory.flush()

    asyncio.run(scenario())


def test_database_lock_does_not_prevent_loop_from_releasing_it(tmp_path):
    async def scenario():
        memory = ConversationMemoryManager(str(tmp_path))
        blocker = sqlite3.connect(memory.db_path)
        blocker.execute("BEGIN IMMEDIATE")
        try:
            write = asyncio.create_task(memory.add_to_channel_memory("channel", {
                "message_id": "new", "guild_id": "guild", "content": "persisted",
            }))
            # The loop must reach this release even while SQLite is waiting.
            await asyncio.sleep(0)
            blocker.rollback()
            await write
            history = await memory.get_channel_memory(
                "channel", requester=MemoryRequester("user", "channel", "guild"))
            assert [r["message_id"] for r in history] == ["new"]
        finally:
            blocker.close()
            await memory.flush()

    asyncio.run(scenario())


def test_cancelled_write_is_serialized_with_later_work_and_shutdown(tmp_path):
    async def scenario():
        memory = ConversationMemoryManager(str(tmp_path))
        blocker = sqlite3.connect(memory.db_path)
        blocker.execute("BEGIN IMMEDIATE")
        try:
            first = asyncio.create_task(memory.add_to_channel_memory("channel", {
                "message_id": "first", "guild_id": "guild", "content": "first",
            }))
            await asyncio.sleep(0)
            first.cancel()
            try:
                await first
            except asyncio.CancelledError:
                pass
            second = asyncio.create_task(memory.add_to_channel_memory("channel", {
                "message_id": "second", "guild_id": "guild", "content": "second",
            }))
            blocker.rollback()
            await second
            await memory.flush()
            with sqlite3.connect(memory.db_path) as db:
                assert {r[0] for r in db.execute("SELECT id FROM vectors")} == {"first", "second"}
        finally:
            blocker.close()
            await memory.flush()

    asyncio.run(scenario())


def test_indexed_recall_is_scoped_and_tracks_edits_retention_and_clear(tmp_path):
    async def scenario():
        memory = ConversationMemoryManager(str(tmp_path), max_messages=3)
        requester = MemoryRequester("u1", "c1", "g1", is_admin=True)
        try:
            for mid, guild, channel, text in (
                (1, "g1", "c1", "Birch deployment uses Sequoia"),
                (2, "g1", "c1", "unrelated lunch"),
                (3, "g1", "c1", "unrelated weather"),
                (4, "g2", "c1", "Birch private other guild"),
                (5, "g1", "c2", "Birch private other channel"),
                (6, "", "c1", "Birch private DM"),
            ):
                await memory.add_to_channel_memory(channel, {
                    "message_id": str(mid), "guild_id": guild, "content": text,
                    "author_id": "u1", "timestamp": f"2026-10-11T12:00:0{mid}+00:00",
                })
            recalled = await memory.get_relevant_channel_memory("c1", 'Birch" OR "scope_key', requester=requester)
            assert [row["message_id"] for row in recalled] == ["1"]
            assert await memory.get_relevant_channel_memory("c1", "Birch") == []
            assert await memory.get_relevant_channel_memory("c2", "Birch", requester=requester) == []
            assert await memory.get_relevant_channel_memory("c1", "Birch", requester=requester, exclude_ids=["1"]) == []
            dm_rows = await memory.get_relevant_channel_memory(
                "c1", "Birch", requester=MemoryRequester("u1", "c1", is_dm=True),
            )
            assert [row["message_id"] for row in dm_rows] == ["6"]
            await memory.add_to_channel_memory("c1", {
                "message_id": "1", "guild_id": "g1", "content": "Cedar deployment replaces Birch",
                "timestamp": "2026-10-11T12:00:01+00:00",
            })
            assert await memory.get_relevant_channel_memory("c1", "Sequoia", requester=requester) == []
            assert [row["message_id"] for row in await memory.get_relevant_channel_memory(
                "c1", "Cedar", requester=requester,
            )] == ["1"]
            await memory.add_to_channel_memory("c1", {
                "message_id": "7", "guild_id": "g1", "content": "fresh unrelated message",
                "timestamp": "2026-10-11T12:00:07+00:00",
            })
            assert await memory.get_relevant_channel_memory("c1", "Cedar", requester=requester) == []
            await memory.clear_channel_memory("c2")
            assert await memory.get_relevant_channel_memory(
                "c2", "Birch", requester=MemoryRequester("u1", "c2", "g1"),
            ) == []
        finally:
            await memory.flush()
        restored = ConversationMemoryManager(str(tmp_path))
        assert await restored.get_relevant_channel_memory("c1", "Cedar", requester=requester) == []
        await restored.flush()
    asyncio.run(scenario())
