import asyncio

from memory import MemoryManager


def test_apply_ltm_batch_deletes_correct_entries_no_renumber_corruption(tmp_path):
    """Regression: applying a multi-delete plan one-by-one renumbered LTM
    entries to positional ids after every save, so the 2nd+ deletes targeted
    the WRONG entries (silent memory corruption). apply_ltm_batch applies all
    ops in one pass with a single renumber at the end.
    """
    async def run():
        mgr = MemoryManager(str(tmp_path))
        mgr.load_from_disk()
        for i in range(5):
            await mgr.add_long_term_memory(f"fact {i + 1}")
        # ids are now 1..5 -> fact 1..fact 5
        before = [e["content"] for e in mgr.get_long_term_memory()]
        assert before == [f"fact {i}" for i in range(1, 6)]

        # Plan from a snapshot: delete ids 3 and 4 (the two middle facts).
        edited, deleted = await mgr.apply_ltm_batch(deletes=["3", "4"])
        assert deleted == 2
        assert edited == 0
        after = [e["content"] for e in mgr.get_long_term_memory()]
        # Without the batch fix, deleting 3 then 4 would remove fact 2 and fact 4
        # (renumber shifted fact 4->3, so "remove 4" deleted the original fact 5's
        # neighbor). The correct result is fact 1, 2, 5 remaining, renumbered 1..3.
        assert after == ["fact 1", "fact 2", "fact 5"], after
        assert [e["id"] for e in mgr.get_long_term_memory()] == [1, 2, 3]

    asyncio.run(run())


def test_apply_ltm_batch_edits_and_deletes_in_one_pass(tmp_path):
    async def run():
        mgr = MemoryManager(str(tmp_path))
        mgr.load_from_disk()
        for i in range(5):
            await mgr.add_long_term_memory(f"fact {i + 1}")
        # edit id 2, delete id 4, keep the rest
        edited, deleted = await mgr.apply_ltm_batch(
            edits={"2": "fact TWO"}, deletes=["4"]
        )
        assert edited == 1
        assert deleted == 1
        contents = {e["content"] for e in mgr.get_long_term_memory()}
        assert "fact TWO" in contents
        assert "fact 2" not in contents
        assert "fact 4" not in contents
        assert "fact 1" in contents and "fact 3" in contents and "fact 5" in contents
        # ids renumbered contiguously
        assert [e["id"] for e in mgr.get_long_term_memory()] == [1, 2, 3, 4]

    asyncio.run(run())


def test_apply_ltm_batch_merge_keep_and_delete(tmp_path):
    async def run():
        mgr = MemoryManager(str(tmp_path))
        mgr.load_from_disk()
        for i in range(4):
            await mgr.add_long_term_memory(f"fact {i + 1}")
        # merge: keep id 1 (edit), delete ids 2 and 4
        edited, deleted = await mgr.apply_ltm_batch(
            edits={"1": "merged fact 1+2"}, deletes=["2", "4"]
        )
        assert edited == 1
        assert deleted == 2
        after = [e["content"] for e in mgr.get_long_term_memory()]
        assert after == ["merged fact 1+2", "fact 3"], after

    asyncio.run(run())


def test_apply_ltm_batch_persists_to_disk(tmp_path):
    async def run():
        mgr = MemoryManager(str(tmp_path))
        mgr.load_from_disk()
        for i in range(5):
            await mgr.add_long_term_memory(f"fact {i + 1}")
        await mgr.apply_ltm_batch(deletes=["1", "3", "5"])
        # A fresh manager reads the persisted result
        mgr2 = MemoryManager(str(tmp_path))
        mgr2.load_from_disk()
        contents = [e["content"] for e in mgr2.get_long_term_memory()]
        assert contents == ["fact 2", "fact 4"], contents

    asyncio.run(run())
