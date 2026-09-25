"""Tests for RAGMemoryManager.apply_ltm_batch."""
import asyncio

from rag_memory import MemoryRequester, RAGMemoryManager


def _run(coro):
    return asyncio.run(coro)


def _requester():
    return MemoryRequester(
        user_id="user-1", channel_id="channel-1", guild_id="guild-1",
        channel_is_public=True,
    )


def test_apply_ltm_batch_adds(tmp_path):
    async def run():
        mgr = RAGMemoryManager(str(tmp_path))
        result = await mgr.apply_ltm_batch([
            {"kind": "add", "content": "fact 1"},
            {"kind": "add", "content": "fact 2"},
            {"kind": "add", "content": "fact 3"},
        ], requester=_requester())
        assert result["added"] == 3
        assert result["errors"] == 0
        ltm = mgr.get_long_term_memory(_requester())
        assert len(ltm) == 3
        assert [e["content"] for e in ltm] == ["fact 1", "fact 2", "fact 3"]

    _run(run())


def test_apply_ltm_batch_deletes(tmp_path):
    async def run():
        mgr = RAGMemoryManager(str(tmp_path))
        # Add 5 facts
        ids = []
        for i in range(5):
            mid = await mgr.add_long_term_memory(f"fact {i + 1}", requester=_requester())
            ids.append(mid)
        # Delete ids 2 and 3 (0-indexed: positions 2, 3)
        result = await mgr.apply_ltm_batch([
            {"kind": "delete", "id": ids[2]},
            {"kind": "delete", "id": ids[3]},
        ], requester=_requester())
        assert result["deleted"] == 2
        ltm = mgr.get_long_term_memory(_requester())
        assert len(ltm) == 3
        contents = [e["content"] for e in ltm]
        assert "fact 3" not in contents
        assert "fact 4" not in contents

    _run(run())


def test_apply_ltm_batch_edits(tmp_path):
    async def run():
        mgr = RAGMemoryManager(str(tmp_path))
        mid = await mgr.add_long_term_memory("original", requester=_requester())
        result = await mgr.apply_ltm_batch([
            {"kind": "edit", "id": mid, "content": "edited"},
        ], requester=_requester())
        assert result["edited"] == 1
        ltm = mgr.get_long_term_memory(_requester())
        assert ltm[0]["content"] == "edited"

    _run(run())


def test_apply_ltm_batch_mixed(tmp_path):
    async def run():
        mgr = RAGMemoryManager(str(tmp_path))
        # id unused — this entry is asserted by content below.
        await mgr.add_long_term_memory("keep me", requester=_requester())
        mid2 = await mgr.add_long_term_memory("edit me", requester=_requester())
        mid3 = await mgr.add_long_term_memory("delete me", requester=_requester())
        result = await mgr.apply_ltm_batch([
            {"kind": "add", "content": "new fact"},
            {"kind": "edit", "id": mid2, "content": "edited"},
            {"kind": "delete", "id": mid3},
        ], requester=_requester())
        assert result["added"] == 1
        assert result["edited"] == 1
        assert result["deleted"] == 1
        ltm = mgr.get_long_term_memory(_requester())
        contents = [e["content"] for e in ltm]
        assert "keep me" in contents
        assert "edited" in contents
        assert "new fact" in contents
        assert "delete me" not in contents

    _run(run())
