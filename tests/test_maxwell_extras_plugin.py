"""Regression tests for plugins/maxwell_extras."""

import asyncio
from types import SimpleNamespace


from plugins.maxwell_extras.tools import (
    RecallCrossServerMemoryTool,
    ReminderStore,
)


def test_reminder_store_persists_and_scopes(tmp_path):
    async def run():
        path = tmp_path / "reminders.json"
        store = ReminderStore(path)
        a = await store.add(
            owner_id="1",
            channel_id="10",
            text="alpha",
            due_at=100.0,
        )
        await store.add(
            owner_id="2",
            channel_id="10",
            text="beta",
            due_at=200.0,
        )
        assert [row["text"] for row in await store.list_for("1")] == ["alpha"]

        fresh = ReminderStore(path)
        assert [row["text"] for row in await fresh.list_for("2")] == ["beta"]
        assert [row["id"] for row in await fresh.due(now=150.0)] == [a["id"]]
        assert await fresh.remove(a["id"], owner_id="2") is False
        assert await fresh.remove(a["id"], owner_id="1") is True

    asyncio.run(run())


def test_cross_server_recall_non_admin_is_author_scoped():
    class Memory:
        def __init__(self):
            self.calls = []

        async def rag_search(self, query, **kwargs):
            self.calls.append((query, kwargs))
            return [{"id": "1", "content": "my old message", "guild_id": "g"}]

        async def get_entity_facts(self, *args, **kwargs):
            return []

        async def get_relevant_shared_context(self, **kwargs):
            return []

    async def run():
        memory = Memory()
        bot = SimpleNamespace(memory=memory, _is_admin=lambda _uid: False)
        tool = RecallCrossServerMemoryTool(bot)
        message = SimpleNamespace(
            author=SimpleNamespace(id=123),
            guild=SimpleNamespace(id=456),
            channel=SimpleNamespace(id=789),
        )
        result = await tool.execute(message, query="old thing", scope="self")
        assert "my old message" in result
        assert memory.calls[0][1]["author_id"] == "123"
        assert "guild_id" not in memory.calls[0][1]
        denied = await tool.execute(message, query="anything", scope="global")
        assert "restricted" in denied.lower()

    asyncio.run(run())


