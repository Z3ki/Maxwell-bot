import asyncio

from memory import MemoryManager


def test_long_term_memory_reloads_external_file_edits(tmp_path):
    async def run():
        mgr = MemoryManager(str(tmp_path))
        mgr.load_from_disk()
        await mgr.add_long_term_memory("bot fact")

        (tmp_path / "long_term_memory.txt").write_text("dashboard fact\n", encoding="utf-8")

        assert mgr.get_long_term_memory() == [{"id": 1, "content": "dashboard fact"}]
        await mgr.add_long_term_memory("bot fact 2")
        assert (tmp_path / "long_term_memory.txt").read_text(encoding="utf-8").splitlines() == [
            "dashboard fact",
            "bot fact 2",
        ]

    asyncio.run(run())


def test_long_term_memory_reloads_external_file_deletion(tmp_path):
    async def run():
        mgr = MemoryManager(str(tmp_path))
        mgr.load_from_disk()
        await mgr.add_long_term_memory("old fact")

        (tmp_path / "long_term_memory.txt").unlink()

        await mgr.add_long_term_memory("new fact")
        assert (tmp_path / "long_term_memory.txt").read_text(encoding="utf-8").splitlines() == ["new fact"]

    asyncio.run(run())


def test_shared_context_reloads_external_file_edits_before_bot_write(tmp_path):
    async def run():
        mgr = MemoryManager(str(tmp_path))
        mgr.load_from_disk()
        await mgr.add_shared_context({"scope": "global", "content": "bot fact"})

        (tmp_path / "shared_context.json").write_text(
            '[{"id":"dash","scope":"global","visibility":"shared","importance":5,"content":"dashboard fact"}]',
            encoding="utf-8",
        )

        await mgr.add_shared_context({"scope": "global", "content": "bot fact 2"})
        contents = [entry["content"] for entry in await mgr.list_shared_context()]
        assert "dashboard fact" in contents
        assert "bot fact 2" in contents
        assert "bot fact" not in contents

    asyncio.run(run())


def test_shared_context_reloads_external_file_deletion(tmp_path):
    async def run():
        mgr = MemoryManager(str(tmp_path))
        mgr.load_from_disk()
        await mgr.add_shared_context({"scope": "global", "content": "old fact"})

        (tmp_path / "shared_context.json").unlink()

        await mgr.add_shared_context({"scope": "global", "content": "new fact"})
        contents = [entry["content"] for entry in await mgr.list_shared_context()]
        assert contents == ["new fact"]

    asyncio.run(run())
