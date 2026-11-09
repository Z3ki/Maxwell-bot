"""Reaction identities obey the same durable deletion fence as message text."""

import asyncio
import json
import sqlite3
import threading
import time

from conversation_memory import ConversationMemoryManager
from maxwell_core.memory.scope import MemoryRequester


def test_delete_reactor_preserves_other_authors_message_and_other_reactors(tmp_path):
    async def scenario():
        memory = ConversationMemoryManager(str(tmp_path))
        for uid in ("100", "200", "300"):
            memory.preferences.set_default(uid, "tracking", "on")
        try:
            await memory.add_to_channel_memory("20", {
                "message_id": "message", "guild_id": "30", "author_id": "200",
                "content": "keep this message", "reactions": [
                    {"user_id": "100", "user_name": "erase me", "emoji": "👍", "privacy_started_at": time.time()},
                    {"user_id": "300", "user_name": "keep me", "emoji": "❤️", "privacy_started_at": time.time()},
                ],
            })
            memory.preferences.set_default("100", "tracking", "off")
            result = await memory.delete_author_content("100")
            assert result["messages"] == 0
            raw = memory._db.execute("SELECT metadata FROM vectors WHERE id='message'").fetchone()[0]
            assert "erase me" not in raw and '"100"' not in raw
            requester = MemoryRequester("200", "20", "30")
            history = await memory.get_channel_memory("20", requester=requester)
            assert history[0]["content"] == "keep this message"
            assert [row["user_id"] for row in history[0]["reactions"]] == ["300"]
        finally:
            await memory.flush()

    asyncio.run(scenario())


def test_opt_out_hides_stored_reaction_before_background_deletion(tmp_path):
    async def scenario():
        memory = ConversationMemoryManager(str(tmp_path))
        for uid in ("100", "200"):
            memory.preferences.set_default(uid, "tracking", "on")
        try:
            await memory.add_to_channel_memory("20", {
                "message_id": "message", "guild_id": "30", "author_id": "200",
                "content": "keep this message", "reactions": [
                    {"user_id": "100", "user_name": "erase me", "emoji": "👍", "privacy_started_at": time.time()},
                ],
            })
            memory.preferences.set_default("100", "tracking", "off")
            requester = MemoryRequester("200", "20", "30")
            history = await memory.get_channel_memory("20", requester=requester)
            recalled = await memory.get_relevant_channel_memory("20", "keep", requester=requester)
            assert history[0]["reactions"] == []
            assert recalled[0]["reactions"] == []
        finally:
            await memory.flush()

    asyncio.run(scenario())


def test_late_reaction_patch_cannot_restore_identity_after_delete_and_reenable(tmp_path):
    async def scenario():
        memory = ConversationMemoryManager(str(tmp_path))
        for uid in ("100", "200"):
            memory.preferences.set_default(uid, "tracking", "on")
        blocker = sqlite3.connect(memory.db_path, isolation_level=None)
        entered = threading.Event()
        merge = memory._merge_message_metadata

        def blocked_merge(message_id, patch):
            entered.set()
            return merge(message_id, patch)

        try:
            await memory.add_to_channel_memory("20", {
                "message_id": "message", "guild_id": "30", "author_id": "200",
                "content": "keep this message",
            })
            patch = {"reactions": [{
                "user_id": "100", "user_name": "erase me", "emoji": "👍",
                "privacy_started_at": time.time(),
            }]}
            blocker.execute("BEGIN IMMEDIATE")
            memory._merge_message_metadata = blocked_merge
            writer = asyncio.create_task(memory.merge_message_metadata("message", patch))
            assert await asyncio.to_thread(entered.wait, 5)
            memory.preferences.delete_personal("100")
            memory.preferences.set_default("100", "tracking", "on")
            blocker.execute("ROLLBACK")
            assert await asyncio.wait_for(writer, 5)
            raw = memory._db.execute("SELECT metadata FROM vectors WHERE id='message'").fetchone()[0]
            assert json.loads(raw)["reactions"] == []
        finally:
            blocker.close()
            await memory.flush()

    asyncio.run(scenario())


def test_periodic_cleanup_removes_reactor_without_authored_messages(tmp_path):
    from privacy_data import cleanup_data

    async def scenario():
        memory = ConversationMemoryManager(str(tmp_path))
        for uid in ("100", "200"):
            memory.preferences.set_default(uid, "tracking", "on")
        try:
            await memory.add_to_channel_memory("20", {
                "message_id": "message", "guild_id": "30", "author_id": "200",
                "content": "keep this message", "reactions": [
                    {"user_id": "100", "user_name": "erase me", "emoji": "👍", "privacy_started_at": time.time()},
                ],
            })
            memory.preferences.set_default("100", "tracking", "off")
            await asyncio.to_thread(cleanup_data, tmp_path, memory.preferences)
            raw = memory._db.execute("SELECT metadata FROM vectors WHERE id='message'").fetchone()[0]
            assert json.loads(raw)["reactions"] == []
        finally:
            await memory.flush()

    asyncio.run(scenario())


def test_processing_notice_matches_opt_in_history_default(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from plugins.maxwell_extras.user_preferences import UserPreferenceStore
    from privacy import notice_text

    monkeypatch.setenv("MAXWELL_PUBLIC_BASE_URL", "https://operator.test")
    preferences = UserPreferenceStore(tmp_path / "preferences.json")
    bot = SimpleNamespace(_user_preferences=preferences)
    text = notice_text(bot, "100")
    assert text is not None
    assert "saved only when you enable" in text
    assert preferences.get("100")["defaults"]["tracking"] == "off"
