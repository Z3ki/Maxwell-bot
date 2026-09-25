"""Tests for RAGMemoryManager (replaces old MemoryManager tests)."""
import asyncio

from rag_memory import MemoryRequester, RAGMemoryManager


def _run(coro):
    """Run an async coroutine in a fresh event loop."""
    return asyncio.run(coro)


def _requester(channel="chan1", user="123", guild="", *, is_dm=True, is_admin=False, public=False):
    return MemoryRequester(
        user_id=user, channel_id=channel, guild_id=guild, is_dm=is_dm,
        is_admin=is_admin, channel_is_public=public,
    )


def _admin_requester():
    return _requester(
        channel="admin-channel", user="owner", guild="guild-1",
        is_dm=False, is_admin=True, public=True,
    )


def test_long_term_memory_add_and_get(tmp_path):
    async def run():
        mgr = RAGMemoryManager(str(tmp_path))
        mid = await mgr.add_long_term_memory("bot fact", requester=_requester())
        ltm = mgr.get_long_term_memory(_requester())
        assert len(ltm) == 1
        assert ltm[0]["content"] == "bot fact"
        assert ltm[0]["id"] == mid

    _run(run())


def test_long_term_memory_edit(tmp_path):
    async def run():
        mgr = RAGMemoryManager(str(tmp_path))
        mid = await mgr.add_long_term_memory("original fact", requester=_requester())
        ok = await mgr.edit_long_term_memory(mid, "edited fact")
        assert ok
        ltm = mgr.get_long_term_memory(_requester())
        assert ltm[0]["content"] == "edited fact"

    _run(run())


def test_long_term_memory_remove(tmp_path):
    async def run():
        mgr = RAGMemoryManager(str(tmp_path))
        mid = await mgr.add_long_term_memory("doomed fact", requester=_requester())
        ok = await mgr.remove_long_term_memory(mid)
        assert ok
        assert len(mgr.get_long_term_memory(_requester())) == 0

    _run(run())


def test_channel_memory_add_and_get(tmp_path):
    async def run():
        mgr = RAGMemoryManager(str(tmp_path))
        await mgr.add_to_channel_memory("chan1", {
            "message_id": "m1",
            "author": "Alice",
            "author_id": "123",
            "content": "hello world",
            "timestamp": "2026-01-01T00:00:00+00:00",
        })
        mem = await mgr.get_channel_memory("chan1", requester=_requester())
        assert len(mem) == 1
        assert mem[0]["author"] == "Alice"
        assert mem[0]["content"] == "hello world"
        assert await mgr.list_recent_channel_ids() == ["chan1"]

    _run(run())


def test_channel_memory_clear(tmp_path):
    async def run():
        mgr = RAGMemoryManager(str(tmp_path))
        await mgr.add_to_channel_memory("chan1", {
            "message_id": "m1",
            "author": "Alice",
            "author_id": "123",
            "content": "hello",
            "timestamp": "2026-01-01T00:00:00+00:00",
        })
        await mgr.clear_channel_memory("chan1")
        assert len(await mgr.get_channel_memory("chan1", requester=_requester())) == 0

        await mgr.add_to_channel_memory("chan1", {
            "message_id": "u1",
            "author": "Alice",
            "author_id": "123",
            "content": "user line",
            "timestamp": "2026-01-01T00:00:00+00:00",
        })
        await mgr.add_to_channel_memory("chan1", {
            "message_id": "b1",
            "author": "Maxwell",
            "author_id": "999",
            "author_is_bot": True,
            "content": "bot line that is long enough to store",
            "timestamp": "2026-01-01T00:00:01+00:00",
        })
        assert len(await mgr.get_channel_memory("chan1", requester=_requester())) == 2
        await mgr.clear_channel_memory("chan1")
        assert len(await mgr.get_channel_memory("chan1", requester=_requester())) == 0

    _run(run())


def test_server_prompt(tmp_path):
    mgr = RAGMemoryManager(str(tmp_path))
    assert mgr.get_server_prompt("123") is None
    mgr.set_server_prompt("123", "be casual")
    assert mgr.get_server_prompt("123") == "be casual"
    mgr.clear_server_prompt("123")
    assert mgr.get_server_prompt("123") is None


def test_shared_context(tmp_path):
    async def run():
        mgr = RAGMemoryManager(str(tmp_path))
        sid = await mgr.add_shared_context({
            "content": "test fact",
            "scope": "global",
            "public_approved": True,
            "source_kind": "operator_public",
            "importance": 5,
        }, requester=_admin_requester())
        sc = await mgr.list_shared_context(requester=_admin_requester())
        assert len(sc) == 1
        assert sc[0]["content"] == "test fact"
        ok = await mgr.remove_shared_context(sid, requester=_admin_requester())
        assert ok
        assert len(await mgr.list_shared_context(requester=_admin_requester())) == 0

    _run(run())


def test_ltm_batch(tmp_path):
    async def run():
        mgr = RAGMemoryManager(str(tmp_path))
        result = await mgr.apply_ltm_batch([
            {"kind": "add", "content": "fact 1"},
            {"kind": "add", "content": "fact 2"},
            {"kind": "add", "content": "fact 3"},
        ], requester=_requester())
        assert result["added"] == 3
        assert len(mgr.get_long_term_memory(_requester())) == 3

    _run(run())


def test_message_dedup(tmp_path):
    """Adding a message with the same ID should replace, not duplicate."""
    async def run():
        mgr = RAGMemoryManager(str(tmp_path))
        await mgr.add_to_channel_memory("chan1", {
            "message_id": "m1",
            "author": "Alice",
            "author_id": "123",
            "content": "original",
            "timestamp": "2026-01-01T00:00:00+00:00",
        })
        await mgr.add_to_channel_memory("chan1", {
            "message_id": "m1",
            "author": "Alice",
            "author_id": "123",
            "content": "updated",
            "timestamp": "2026-01-01T00:00:01+00:00",
        })
        mem = await mgr.get_channel_memory("chan1", requester=_requester())
        assert len(mem) == 1
        assert mem[0]["content"] == "updated"

    _run(run())


def test_identical_message_text_keeps_discord_identity_and_order(tmp_path):
    async def run():
        mgr = RAGMemoryManager(str(tmp_path))
        for message_id, author_id, timestamp in (
            ("m1", "alice", "2026-01-01T00:00:00+00:00"),
            ("m2", "bob", "2026-01-01T00:00:01+00:00"),
            ("m3", "alice", "2026-01-01T00:00:02+00:00"),
        ):
            await mgr.add_to_channel_memory(
                "chan1",
                {
                    "message_id": message_id,
                    "author": author_id,
                    "author_id": author_id,
                    "content": "ok",
                    "timestamp": timestamp,
                },
            )
        rows = await mgr.get_channel_memory("chan1", requester=_requester())
        assert [row["message_id"] for row in rows] == ["m1", "m2", "m3"]
        assert [row["author_id"] for row in rows] == ["alice", "bob", "alice"]

    _run(run())


def test_active_channels_are_not_deleted_after_the_first_twenty_five(tmp_path):
    async def run():
        mgr = RAGMemoryManager(str(tmp_path))
        for index in range(40):
            await mgr.add_to_channel_memory(
                f"channel-{index}",
                {
                    "message_id": f"message-{index}",
                    "author_id": "user",
                    "author": "user",
                    "content": "hi",
                    "timestamp": f"2026-01-01T00:{index:02d}:00+00:00",
                },
            )
        count = mgr._db.execute(
            "SELECT COUNT(DISTINCT channel_id) FROM vectors WHERE kind='message'"
        ).fetchone()[0]
        assert count == 40

    _run(run())


def test_shared_context_accepts_budget_and_hides_expired_admin_facts(tmp_path):
    async def run():
        mgr = RAGMemoryManager(str(tmp_path))
        await mgr.add_shared_context({
            "content": "public fact",
            "scope": "global",
            "public_approved": True,
            "source_kind": "operator_public",
            "importance": 5,
            "visibility": "shared",
        }, requester=_admin_requester())
        await mgr.add_shared_context({
            "content": "secret admin fact",
            "scope": "global",
            "public_approved": True,
            "source_kind": "operator_public",
            "importance": 9,
            "visibility": "admin_only",
        }, requester=_admin_requester())
        await mgr.add_shared_context({
            "content": "expired fact",
            "scope": "global",
            "public_approved": True,
            "source_kind": "operator_public",
            "importance": 8,
            "visibility": "shared",
            "expires_at": "2000-01-01T00:00:00+00:00",
        }, requester=_admin_requester())
        visible = await mgr.get_relevant_shared_context(
            requester=_requester(channel="read-channel", user="1", guild="guild-1", is_dm=False, public=True),
            user_id="1", is_admin=False, max_items=20, budget=10000
        )
        contents = [e["content"] for e in visible]
        assert "public fact" in contents
        assert "secret admin fact" not in contents
        assert "expired fact" not in contents
        admin = await mgr.get_relevant_shared_context(
            requester=_admin_requester(),
            user_id="1", is_admin=True, max_items=20, budget=10000
        )
        admin_contents = [e["content"] for e in admin]
        assert "secret admin fact" in admin_contents

    _run(run())


def test_duplicate_content_hash_does_not_crash_init(tmp_path):
    import time

    mgr = RAGMemoryManager(str(tmp_path))
    db = mgr._db
    db.execute("DROP INDEX IF EXISTS idx_unique_content")
    now = time.time()
    for vid in ("dup-a", "dup-b"):
        db.execute(
            "INSERT INTO vectors (id, kind, channel_id, content, content_hash, "
            "timestamp, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                vid,
                "message",
                "chan",
                "same text",
                "abc123",
                "2026-01-01T00:00:00+00:00",
                now,
            ),
        )
    mgr._db.close()
    reopened = RAGMemoryManager(str(tmp_path))
    ids = [
        row["id"]
        for row in reopened._db.execute(
            "SELECT id FROM vectors WHERE channel_id='chan' ORDER BY id"
        ).fetchall()
    ]
    assert ids == ["dup-a", "dup-b"]
