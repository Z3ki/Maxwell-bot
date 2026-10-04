"""Integration gaps found in an independent review of the reliability refactor."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from bot import MaxwellBot
from maxwell_core.plugins.manager import PluginManager
from maxwell_core.prompts.component import PromptComponent
from maxwell_core.tools.registry import get_global_registry
from rag_memory import MemoryRequester, RAGMemoryManager


@pytest.mark.parametrize("async_setup", [False, True])
def test_plugin_reload_preserves_host_aliases_and_core_services(tmp_path, async_setup):
    directory = tmp_path / "plugins" / "review_service_consumer"
    directory.mkdir(parents=True)
    (directory / "plugin.json").write_text(
        json.dumps({"id": directory.name, "enabled_globally": True}),
        encoding="utf-8",
    )
    declaration = "async def" if async_setup else "def"
    (directory / "__init__.py").write_text(
        f"{declaration} setup(bot, ctx):\n"
        "    assert ctx.service('memory') is bot.memory\n"
        "    assert ctx.service('provider:primary') is bot.ai_provider\n"
        "    assert bot.hooks is ctx._manager.hooks\n"
        "    assert bot.prompts is ctx._manager.prompts\n"
        "    assert bot.tool_registry is ctx._manager.tool_registry\n"
        "    bot.generations.append(object())\n"
        "    ctx.register_service('review_generation', bot.generations[-1])\n"
        "    return []\n",
        encoding="utf-8",
    )
    bot = SimpleNamespace(tools={}, memory=object(), ai_provider=object(), generations=[])
    manager = PluginManager(
        bot,
        plugins_dir=str(directory.parent),
        data_dir=str(tmp_path / "data"),
    )
    bot.plugin_manager = manager
    bot.hooks = manager.hooks
    bot.prompts = manager.prompts
    bot.tool_registry = manager.tool_registry
    services = manager.services
    services.register("memory", bot.memory, owner="core")
    services.register("provider:primary", bot.ai_provider, owner="core")
    created = []

    def lazy_provider():
        instance = object()
        created.append(instance)
        return instance

    services.factory("provider:lazy", lazy_provider, owner="core")
    manager.prompts.register(
        PromptComponent(id="core.review", plugin="core", text="Core instructions")
    )

    async def run():
        try:
            manager.load_plugins()
            await manager.complete_pending_setups()
            assert manager.load_errors == {}
            assert len(bot.generations) == 1
            first = manager.services.get("review_generation")
            await manager.reload_plugins_async()
            assert manager.load_errors == {}
            assert len(bot.generations) == 2
            assert manager.services.get("review_generation") is not first
            assert bot.hooks is manager.hooks
            assert bot.prompts is manager.prompts
            assert bot.tool_registry is manager.tool_registry is get_global_registry()
            assert manager.services is services
            assert manager.services.get("memory") is bot.memory
            assert manager.services.get("provider:primary") is bot.ai_provider
            assert any(component.id == "core.review" for component in manager.prompts.components())
            assert created == []
            lazy = manager.services.get("provider:lazy")
            assert lazy is manager.services.get("provider:lazy")
            assert created == [lazy]
        finally:
            await manager.teardown()

    asyncio.run(run())


@pytest.mark.parametrize("guild", ["guild", ""])
def test_rem_reply_backfill_retains_recorded_guild_provenance(tmp_path, monkeypatch, guild):
    def skip_background(_memory, coroutine):
        coroutine.close()

    monkeypatch.setattr(RAGMemoryManager, "_spawn", skip_background)
    memory = RAGMemoryManager(str(tmp_path))
    event = {
        "role": "assistant",
        "user_id": "1",
        "channel_id": "room",
        "guild_id": guild or None,
        "message_id": "inbound-request-id",
        "ts": "2026-10-04T20:15:00Z",
        "content": "Recovered bot reply",
    }
    bot = SimpleNamespace(
        _control={"store_memory": True},
        memory=memory,
        rem_log=SimpleNamespace(events=[event]),
        user=SimpleNamespace(id=1),
        bot_name="Maxwell",
    )

    async def run():
        await memory.add_to_channel_memory("room", {
            "message_id": event["message_id"], "guild_id": guild,
            "author_id": "alice", "content": "Original user request",
        })
        await MaxwellBot._backfill_bot_replies_from_rem(bot)
        await MaxwellBot._backfill_bot_replies_from_rem(bot)
        requester = MemoryRequester(
            user_id="alice", channel_id="room", guild_id=guild, is_dm=not guild,
        )
        transcript = await memory.get_channel_memory("room", requester=requester)
        assert [row["content"] for row in transcript].count(event["content"]) == 1
        original = next(row for row in transcript if row["message_id"] == event["message_id"])
        assert original["content"] == "Original user request"
        restored = memory._db.execute(
            "SELECT guild_id FROM vectors WHERE content=?", (event["content"],),
        ).fetchone()
        assert restored["guild_id"] == guild

    try:
        asyncio.run(run())
    finally:
        memory._db.close()
