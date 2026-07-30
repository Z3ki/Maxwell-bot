"""Tests for RAGMemoryManager.apply_ltm_batch."""
import asyncio

from rag_memory import RAGMemoryManager


def _run(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_apply_ltm_batch_adds(tmp_path):
    async def run():
        mgr = RAGMemoryManager(str(tmp_path))
        result = await mgr.apply_ltm_batch([
            {"kind": "add", "content": "fact 1"},
            {"kind": "add", "content": "fact 2"},
            {"kind": "add", "content": "fact 3"},
        ])
        assert result["added"] == 3
        assert result["errors"] == 0
        ltm = mgr.get_long_term_memory()
        assert len(ltm) == 3
        assert [e["content"] for e in ltm] == ["fact 1", "fact 2", "fact 3"]

    _run(run())


def test_apply_ltm_batch_deletes(tmp_path):
    async def run():
        mgr = RAGMemoryManager(str(tmp_path))
        # Add 5 facts
        ids = []
        for i in range(5):
            mid = await mgr.add_long_term_memory(f"fact {i + 1}")
            ids.append(mid)
        # Delete ids 2 and 3 (0-indexed: positions 2, 3)
        result = await mgr.apply_ltm_batch([
            {"kind": "delete", "id": ids[2]},
            {"kind": "delete", "id": ids[3]},
        ])
        assert result["deleted"] == 2
        ltm = mgr.get_long_term_memory()
        assert len(ltm) == 3
        contents = [e["content"] for e in ltm]
        assert "fact 3" not in contents
        assert "fact 4" not in contents

    _run(run())


def test_apply_ltm_batch_edits(tmp_path):
    async def run():
        mgr = RAGMemoryManager(str(tmp_path))
        mid = await mgr.add_long_term_memory("original")
        result = await mgr.apply_ltm_batch([
            {"kind": "edit", "id": mid, "content": "edited"},
        ])
        assert result["edited"] == 1
        ltm = mgr.get_long_term_memory()
        assert ltm[0]["content"] == "edited"

    _run(run())


def test_apply_ltm_batch_mixed(tmp_path):
    async def run():
        mgr = RAGMemoryManager(str(tmp_path))
        mid1 = await mgr.add_long_term_memory("keep me")
        mid2 = await mgr.add_long_term_memory("edit me")
        mid3 = await mgr.add_long_term_memory("delete me")
        result = await mgr.apply_ltm_batch([
            {"kind": "add", "content": "new fact"},
            {"kind": "edit", "id": mid2, "content": "edited"},
            {"kind": "delete", "id": mid3},
        ])
        assert result["added"] == 1
        assert result["edited"] == 1
        assert result["deleted"] == 1
        ltm = mgr.get_long_term_memory()
        contents = [e["content"] for e in ltm]
        assert "keep me" in contents
        assert "edited" in contents
        assert "new fact" in contents
        assert "delete me" not in contents

    _run(run())