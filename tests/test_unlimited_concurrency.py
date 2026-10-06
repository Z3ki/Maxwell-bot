"""Independent conversations reach inference without global admission waits."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from aiohttp import web

from api.state import _sanitize_control
from bot import MaxwellBot
from config import Config
from control_defaults import DEFAULT_CONTROL
from message_pipeline import ReplyQueue
from providers import OpenAICompatibleProvider


@pytest.mark.parametrize("count", [100, 1000])
def test_independent_channels_all_reach_inference_before_any_call_finishes(count):
    async def scenario():
        bot = object.__new__(MaxwellBot)
        bot._ai_call_kind = {}
        queue = ReplyQueue()
        all_started, release = asyncio.Event(), asyncio.Event()
        started = 0

        async def handler(message, _content):
            nonlocal started
            await bot._acquire_ai_slot(1, priority="user", key=str(message.channel.id))
            try:
                started += 1
                if started == count:
                    all_started.set()
                await release.wait()
            finally:
                await bot._release_ai_slot()

        queue.bind(handler)
        try:
            for index in range(count):
                message = SimpleNamespace(
                    id=index + 1,
                    channel=SimpleNamespace(id=index + 1),
                    guild=SimpleNamespace(id=index + 1),
                )
                assert queue.submit(index + 1, message, "question", directed=True) == "started"
            await asyncio.wait_for(all_started.wait(), timeout=5)
            assert queue.outstanding == count and not queue.full
            assert queue.stats()["queued"] == 0
            assert bot.ai_slot_stats()["active"] == count
            assert bot.ai_slot_stats()["waiting"] == 0
            # Cancelling one conversation must not cancel or stall the others.
            assert queue.cancel_channel("1")
            await queue._channels["1"].pump
            assert bot.ai_slot_stats()["active"] == count - 1
        finally:
            release.set()
            await queue.close()
        assert queue.outstanding == 0
        assert bot.ai_slot_stats()["active"] == 0

    asyncio.run(scenario())


def test_primary_and_fallback_clients_do_not_queue_connections_to_same_host():
    async def scenario():
        count = 24
        started = 0
        all_started, release = asyncio.Event(), asyncio.Event()

        async def handle(_request):
            nonlocal started
            started += 1
            if started == count * 2:
                all_started.set()
            await release.wait()
            return web.Response(text="ok")

        app = web.Application()
        app.router.add_get("/hold", handle)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        providers = [
            OpenAICompatibleProvider(f"http://127.0.0.1:{port}", name, 10, 0.5)
            for name in ("primary", "fallback")
        ]
        tasks = []
        try:
            for provider in providers:
                session = await provider._get_session()

                async def call(client=session):
                    async with client.get(f"http://127.0.0.1:{port}/hold") as response:
                        assert await response.text() == "ok"

                tasks.extend(asyncio.create_task(call()) for _ in range(count))
            # A local connection cap would prevent this barrier from completing.
            await asyncio.wait_for(all_started.wait(), timeout=5)
            release.set()
            await asyncio.gather(*tasks)
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            for provider in providers:
                await provider.close()
            await runner.cleanup()

    asyncio.run(asyncio.wait_for(scenario(), timeout=10))


def test_saved_controls_cannot_restore_the_legacy_inference_pool(tmp_path):
    stale = {"ai_concurrency": 2, "message_quota_enabled": True,
             "long_term_memory_enabled": True, "cross_context_enabled": True,
             "entity_memory_enabled": True, "knowledge_graph_enabled": True}
    (tmp_path / "bot_control.json").write_text(json.dumps(stale))
    bot = object.__new__(MaxwellBot)
    bot.config = SimpleNamespace(DATA_DIR=str(tmp_path))
    bot._control_mtime = 0
    bot._ai_call_kind = {}
    bot._conversation_watch_enabled = lambda: True
    bot._load_control(force=True)
    assert "ai_concurrency" not in bot._control
    assert bot._control["message_quota_enabled"] is False
    for key in ("long_term_memory_enabled", "cross_context_enabled", "entity_memory_enabled", "knowledge_graph_enabled"):
        assert key not in bot._control
    assert "ai_concurrency" not in DEFAULT_CONTROL
    assert "ai_concurrency" not in _sanitize_control({"ai_concurrency": 2})
    assert not hasattr(Config, "MAX_PENDING_REPLY_REQUESTS")
    assert bot.ai_slot_stats()["capacity"] == 0


def test_one_channel_accepts_a_large_burst_and_delivers_in_order():
    async def scenario():
        queue = ReplyQueue()
        release, first_started = asyncio.Event(), asyncio.Event()
        delivered = []

        async def handler(message, _content):
            first_started.set()
            await release.wait()
            delivered.append(message.id)

        queue.bind(handler)
        try:
            for index in range(100):
                result = queue.submit("channel", SimpleNamespace(id=index + 1), "question", directed=True)
                assert result in {"started", "queued"}
            await asyncio.wait_for(first_started.wait(), 5)
            assert queue.outstanding == 100
            assert queue.depth("channel") == 99
            release.set()
            await asyncio.wait_for(queue._channels["channel"].pump, 5)
            assert delivered == list(range(1, 101))
            assert queue.outstanding == 0
        finally:
            await queue.close()

    asyncio.run(scenario())
