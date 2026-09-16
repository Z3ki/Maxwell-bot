from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from plugin_manager import PluginContext, PluginManager
from plugins.maxwell_extras import plugin_runtime


def _manager(tmp_path: Path):
    bot = SimpleNamespace()
    manager = PluginManager(
        bot,
        plugins_dir=str(tmp_path / "plugins"),
        data_dir=str(tmp_path / "data"),
        state_file=str(tmp_path / "data" / "plugins.json"),
    )
    bot.plugin_manager = manager
    return bot, manager


def test_runtime_guard_times_out_slow_events_and_records_health(tmp_path, monkeypatch):
    bot, manager = _manager(tmp_path)
    monkeypatch.setattr(plugin_runtime, "_EVENT_TIMEOUT", 0.01)
    plugin_runtime.install_plugin_runtime_guards(bot)
    manager.state["plugins"]["demo"] = {
        "enabled_globally": True,
        "allowed_users": [],
        "denied_users": [],
    }

    async def slow():
        await asyncio.sleep(1)

    manager._register_listener("demo", "on_ready", slow)

    async def run():
        count = await manager.dispatch_event("on_ready")
        assert count == 1
        health = manager.runtime_health("demo")
        assert health["event_calls"] == 1
        assert health["event_timeouts"] == 1
        assert health["event_errors"] == 1

    asyncio.run(run())


def test_ctx_spawn_is_tracked_and_cancelled_on_reload(tmp_path):
    bot, manager = _manager(tmp_path)
    plugin_runtime.install_plugin_runtime_guards(bot)
    ctx = PluginContext(manager, "demo")

    async def run():
        task = ctx.spawn(asyncio.sleep(60), name="demo-background")
        assert task in manager._maxwell_managed_tasks["demo"]
        assert manager.runtime_health("demo")["background_started"] == 1
        manager.reload_plugins()
        await asyncio.sleep(0)
        assert task.cancelled() or task.done()
        assert not manager._maxwell_managed_tasks

    asyncio.run(run())


def test_ctx_after_runs_one_shot_callback(tmp_path):
    bot, manager = _manager(tmp_path)
    plugin_runtime.install_plugin_runtime_guards(bot)
    ctx = PluginContext(manager, "demo")
    seen = []

    async def run():
        async def callback():
            seen.append("yes")

        task = ctx.after(0, callback)
        await task
        assert seen == ["yes"]

    asyncio.run(run())
