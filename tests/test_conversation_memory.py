"""History remains scoped and durable after the live vector store is retired."""

import asyncio
import os
from pathlib import Path
import subprocess
import sys

from conversation_memory import ConversationMemoryManager
from maxwell_core.memory.scope import MemoryRequester


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
    env_file.write_text(f"DISCORD_BOT_TOKEN=offline-test-token\nAI_MODEL=offline-chat-model\nDATA_DIR={tmp_path}/data\nENABLE_RAG=true\nEMBED_MODEL=unused-local-model\n")
    code = """
import sys
class BlockLegacy:
    def find_spec(self, fullname, *args):
        if fullname == 'rag_memory' or fullname == 'numpy' or fullname.startswith('numpy.'):
            raise AssertionError('retired dependency imported: ' + fullname)
sys.meta_path.insert(0, BlockLegacy())
from bot import MaxwellBot
from config import Config
from conversation_memory import ConversationMemoryManager
bot = MaxwellBot()
assert isinstance(bot.memory, ConversationMemoryManager)
assert Config.ENABLE_RAG is False
assert Config.RAG_WEB_STORE_ENABLED is False
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
        await legacy.flush()
        memory = ConversationMemoryManager(str(tmp_path))
        try:
            rows = await memory.get_channel_memory("channel", requester=MemoryRequester("user", "channel", "guild"))
            assert [row["content"] for row in rows] == ["existing conversation"]
            assert all("embedding" not in row for row in rows)
            assert memory.get_long_term_memory() == []
            assert memory._db.execute("SELECT embedding FROM vectors WHERE id='fact'").fetchone()[0] == b"archived-fact-vector"
        finally:
            await memory.flush()

    asyncio.run(scenario())
