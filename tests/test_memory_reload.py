"""Tests for RAGMemoryManager (replaces old MemoryManager tests)."""
import asyncio

from rag_memory import RAGMemoryManager


def _run(coro):
    """Run an async coroutine in a fresh event loop."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_long_term_memory_add_and_get(tmp_path):
    async def run():
        mgr = RAGMemoryManager(str(tmp_path))
        mid = await mgr.add_long_term_memory("bot fact")
        ltm = mgr.get_long_term_memory()
        assert len(ltm) == 1
        assert ltm[0]["content"] == "bot fact"
        assert ltm[0]["id"] == mid

    _run(run())


def test_long_term_memory_edit(tmp_path):
    async def run():
        mgr = RAGMemoryManager(str(tmp_path))
        mid = await mgr.add_long_term_memory("original fact")
        ok = await mgr.edit_long_term_memory(mid, "edited fact")
        assert ok
        ltm = mgr.get_long_term_memory()
        assert ltm[0]["content"] == "edited fact"

    _run(run())


def test_long_term_memory_remove(tmp_path):
    async def run():
        mgr = RAGMemoryManager(str(tmp_path))
        mid = await mgr.add_long_term_memory("doomed fact")
        ok = await mgr.remove_long_term_memory(mid)
        assert ok
        assert len(mgr.get_long_term_memory()) == 0

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
        mem = await mgr.get_channel_memory("chan1")
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
        assert len(await mgr.get_channel_memory("chan1")) == 0

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
        assert len(await mgr.get_channel_memory("chan1")) == 2
        await mgr.clear_channel_memory("chan1")
        assert len(await mgr.get_channel_memory("chan1")) == 0

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
            "importance": 5,
        })
        sc = await mgr.list_shared_context()
        assert len(sc) == 1
        assert sc[0]["content"] == "test fact"
        ok = await mgr.remove_shared_context(sid)
        assert ok
        assert len(await mgr.list_shared_context()) == 0

    _run(run())


def test_ltm_batch(tmp_path):
    async def run():
        mgr = RAGMemoryManager(str(tmp_path))
        result = await mgr.apply_ltm_batch([
            {"kind": "add", "content": "fact 1"},
            {"kind": "add", "content": "fact 2"},
            {"kind": "add", "content": "fact 3"},
        ])
        assert result["added"] == 3
        assert len(mgr.get_long_term_memory()) == 3

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
        mem = await mgr.get_channel_memory("chan1")
        assert len(mem) == 1
        assert mem[0]["content"] == "updated"

    _run(run())


def test_shared_context_accepts_budget_and_hides_expired_admin_facts(tmp_path):
    async def run():
        mgr = RAGMemoryManager(str(tmp_path))
        await mgr.add_shared_context({
            "content": "public fact",
            "scope": "global",
            "importance": 5,
            "visibility": "shared",
        })
        await mgr.add_shared_context({
            "content": "secret admin fact",
            "scope": "global",
            "importance": 9,
            "visibility": "admin_only",
        })
        await mgr.add_shared_context({
            "content": "expired fact",
            "scope": "global",
            "importance": 8,
            "visibility": "shared",
            "expires_at": "2000-01-01T00:00:00+00:00",
        })
        visible = await mgr.get_relevant_shared_context(
            user_id="1", is_admin=False, max_items=20, budget=10000
        )
        contents = [e["content"] for e in visible]
        assert "public fact" in contents
        assert "secret admin fact" not in contents
        assert "expired fact" not in contents
        admin = await mgr.get_relevant_shared_context(
            user_id="1", is_admin=True, max_items=20, budget=10000
        )
        admin_contents = [e["content"] for e in admin]
        assert "secret admin fact" in admin_contents

    _run(run())