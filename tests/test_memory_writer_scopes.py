"""Exercise transcript writers against the real fail-closed SQLite reader."""

import asyncio
from types import SimpleNamespace

import pytest

from autonomy import AutonomyEngine
from bot import MaxwellBot
from conversation_memory import ConversationMemoryManager
from maxwell_core.memory.scope import MemoryRequester
from tooling.helpers import _chess_record


@pytest.mark.parametrize("guild_id", ["555", ""])
@pytest.mark.parametrize("writer", ["tool", "chess", "autonomy"])
def test_generated_history_is_visible_only_in_its_origin_scope(tmp_path, guild_id, writer):
    async def scenario():
        memory = ConversationMemoryManager(str(tmp_path))
        bot = object.__new__(MaxwellBot)
        bot._control = {"store_memory": True}
        bot.memory = memory
        bot._recent_users = {}
        guild = SimpleNamespace(id=int(guild_id), name="server") if guild_id else None
        channel = SimpleNamespace(id=777, guild=guild, name="general")
        message = SimpleNamespace(
            id=1, guild=guild, channel=channel,
            author=SimpleNamespace(id=42, display_name="alice", bot=False),
            type=SimpleNamespace(name="default"), created_at=None,
        )
        try:
            if writer == "tool":
                await bot._remember_tool_call(message, "web_search", {"query": "x"}, "3 results")
            elif writer == "chess":
                await _chess_record(bot, message, "chess move: e4")
            else:
                engine = AutonomyEngine(SimpleNamespace(
                    config=SimpleNamespace(DATA_DIR=str(tmp_path)),
                    _auto_channels=set(), _control={}, tools={}, memory=memory,
                    user=SimpleNamespace(id=99, display_name="Maxwell"),
                ))
                await engine._remember_visible_self_message(channel, message, "my autonomous reply")

            requester = MemoryRequester("42", "777", guild_id, is_dm=not guild_id)
            rows = await memory.get_channel_memory("777", requester=requester)
            assert len(rows) == 1
            if writer == "tool":
                assert rows[0]["is_tool"] is True
                assert rows[0]["tool_name"] == "web_search"
                assert rows[0]["tool_result"] == "3 results"
            elif writer == "chess":
                assert rows[0]["is_tool"] is True
                assert rows[0]["content"] == "chess move: e4"
            else:
                assert rows[0]["autonomy"] is True
                assert rows[0]["content"] == "my autonomous reply"
            assert await memory.get_channel_memory("777") == []
            assert await memory.get_channel_memory("777", requester=MemoryRequester("42", "777", "other")) == []
            assert await memory.get_channel_memory("778", requester=requester) == []
            if guild_id:
                assert await memory.get_channel_memory("777", requester=MemoryRequester("42", "777", is_dm=True)) == []
        finally:
            await memory.flush()

    asyncio.run(scenario())
