"""Plugin startup, reload, and shutdown behavior under partial failures."""

from __future__ import annotations

import asyncio
import inspect
import json
from types import SimpleNamespace

import pytest

from maxwell_core.plugins.context import PluginContext
from maxwell_core.plugins.manager import PluginManager
import maxwell_core.plugins.manager as manager_module


TOOL_SOURCE = """
class DemoTool:
    tool_name = "lifecycle_echo"
    returns_result = True
    def get_description(self):
        return "echo"
    def get_parameters(self):
        return {"type": "object", "properties": {"text": {"type": "string"}}}
    async def execute(self, message=None, text=""):
        return text
"""


@pytest.fixture
def runtime(tmp_path):
    bot = SimpleNamespace(tools={}, events=[])
    manager = PluginManager(
        bot,
        plugins_dir=str(tmp_path / "plugins"),
        data_dir=str(tmp_path / "data"),
    )

    def write(name, source, **manifest):
        directory = manager.plugins_dir / name
        directory.mkdir()
        (directory / "plugin.json").write_text(
            json.dumps({"id": name, "enabled_globally": True, **manifest}),
            encoding="utf-8",
        )
        (directory / "__init__.py").write_text(source, encoding="utf-8")
        return directory

    return bot, manager, write


def test_tool_conflicts_do_not_publish_partial_dispatch_views(runtime):
    _, manager, _ = runtime
    original = SimpleNamespace(execute=lambda: None)
    manager.tool_registry.register_tool(original, name="shared", plugin="core")

    with pytest.raises(ValueError, match="already registered"):
        manager.register_context_tool("failed", SimpleNamespace(execute=lambda: None), name="shared")

    assert manager.tool_registry.tool("shared") is original
    assert "failed" not in manager.loaded_plugins
    assert "shared" not in manager.all_plugin_tools


def test_same_owner_conflict_retains_original_in_every_dispatch_view(runtime):
    _, manager, _ = runtime
    original = SimpleNamespace(execute=lambda: None)
    manager.register_context_tool("demo", original, name="shared")

    with pytest.raises(ValueError, match="already registered"):
        manager.register_context_tool("demo", SimpleNamespace(execute=lambda: None), name="shared")

    assert manager.loaded_plugins["demo"]["tools"]["shared"] is original
    assert manager.all_plugin_tools["shared"] == ("demo", original)
    assert manager.tool_registry.tool("shared") is original


def test_hookless_protected_plugin_is_fully_unregistered_on_shutdown(runtime):
    bot, manager, write = runtime
    write("lifecycle_hookless", TOOL_SOURCE + """
from maxwell_core.prompts.component import PromptComponent
async def listener():
    pass
def setup(bot, ctx):
    ctx.on_event("on_ready", listener)
    ctx.every(5, listener)
    ctx.register_hook("before_tool", lambda bag: {})
    ctx.register_prompt(PromptComponent(id="hookless.instructions", plugin=ctx.name, text="hello"))
    ctx.register_service("lifecycle_service", object())
    return [DemoTool()]
""", protected=True)
    manager.load_plugins()
    assert "lifecycle_echo" in bot.tools
    assert manager.tool_registry.plugin_tools("lifecycle_hookless") == ["lifecycle_echo"]

    asyncio.run(manager.teardown())

    assert manager.loaded_plugins == {}
    assert manager.tool_registry.names() == []
    assert manager.all_plugin_tools == {}
    assert manager.subscribed_events() == []
    assert manager.hooks.plugin_hooks("lifecycle_hookless") == []
    assert manager.prompts.components() == []
    assert manager.services.names() == []
    assert manager._job_specs == {}
    assert bot.tools == {}


def test_shutdown_preserves_dependencies_until_dependents_finish(runtime):
    bot, manager, write = runtime
    write("lifecycle_base", """
def setup(bot, ctx):
    ctx.register_service("dependency", "alive")
    return []
def teardown(bot, *, ctx):
    bot.events.append("base closed")
""")
    write("lifecycle_dependent", """
def setup(bot, ctx):
    return []
async def teardown(bot, ctx):
    bot.events.append(ctx.service("dependency"))
    raise RuntimeError("broken closer")
""", dependencies=["lifecycle_base"])
    manager.load_plugins()

    asyncio.run(manager.teardown())

    assert bot.events == ["alive", "base closed"]
    assert manager.services.names() == []
    assert manager.loaded_plugins == {}


@pytest.mark.parametrize("failure", ["missing", "setup", "cycle", "entrypoint"])
def test_bad_dependencies_never_execute_dependent_setup(runtime, failure):
    bot, manager, write = runtime
    if failure == "missing":
        write("lifecycle_base", "raise AssertionError('must not import')", dependencies=["absent"])
    elif failure == "setup":
        write("lifecycle_base", "def setup(bot):\n    raise RuntimeError('failed')\n")
    elif failure == "cycle":
        write("lifecycle_base", "raise AssertionError('must not import')", dependencies=["lifecycle_dependent"])
    else:
        path = write("lifecycle_base", "")
        path.joinpath("__init__.py").unlink()
    write("lifecycle_dependent", "raise AssertionError('dependent was imported')", dependencies=["lifecycle_base"])
    write("lifecycle_independent", "def setup(bot):\n    bot.events.append('independent')\n    return []\n")

    manager.load_plugins()

    assert bot.events == ["independent"]
    assert set(manager.loaded_plugins) == {"lifecycle_independent"}
    assert "lifecycle_base" in manager.load_errors
    assert "lifecycle_dependent" in manager.load_errors
    assert "dependent was imported" not in manager.load_errors["lifecycle_dependent"]


def test_async_setup_is_awaited_before_dependent_tools_become_available(runtime):
    bot, manager, write = runtime
    write("lifecycle_base", """
import asyncio
async def setup(bot, ctx):
    await asyncio.sleep(0)
    ctx.register_service("async_provider", "ready")
    bot.events.append("base ready")
    return []
""")
    write("lifecycle_dependent", TOOL_SOURCE + """
def setup(bot, *, ctx):
    bot.events.append(ctx.service("async_provider"))
    return [DemoTool()]
""", dependencies=["lifecycle_base"])

    async def run():
        manager.load_plugins()
        assert bot.tools == {}
        assert manager.get_available_tools("1") == {}
        assert manager.start_jobs() == 0
        await manager.complete_pending_setups()
        assert bot.events == ["base ready", "ready"]
        assert "lifecycle_echo" in bot.tools
        assert manager._pending_async_setup == []
        await manager.teardown()

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["exception", "timeout", "cancel"])
def test_failed_async_setup_rolls_back_resources_and_closes_the_old_provider(runtime, monkeypatch, failure):
    bot, manager, write = runtime
    write("lifecycle_base", TOOL_SOURCE + """
import asyncio
async def setup(bot, ctx):
    ctx.register_tool(DemoTool())
    ctx.register_hook("before_tool", lambda bag: {})
    ctx.register_provider("old provider", name="demo")
    bot.started.set()
    if bot.failure == "exception":
        raise RuntimeError("setup crashed")
    await asyncio.Event().wait()
async def teardown(bot, ctx):
    bot.events.append(ctx.service("provider:demo"))
""")
    write("lifecycle_dependent", "raise AssertionError('must not run')", dependencies=["lifecycle_base"])
    bot.failure = failure
    monkeypatch.setattr(manager_module, "_SETUP_TIMEOUT", 0.02)

    async def run():
        bot.started = asyncio.Event()
        manager.load_plugins()
        task = asyncio.create_task(manager.complete_pending_setups())
        await bot.started.wait()
        assert manager.get_available_tools("1") == {}
        if failure == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await task
        assert manager.loaded_plugins == {}
        assert manager.tool_registry.names() == []
        assert manager.all_plugin_tools == {}
        assert manager.hooks.plugin_hooks("lifecycle_base") == []
        assert manager.services.names() == []
        assert manager._pending_async_setup == []
        assert bot.tools == {}
        assert bot.events == ["old provider"]
        assert "lifecycle_base" in manager.load_errors
        assert "lifecycle_dependent" in manager.load_errors

    asyncio.run(run())


def test_shutdown_discards_setup_coroutine_before_it_runs(runtime):
    bot, manager, write = runtime
    write("lifecycle_unstarted", """
async def setup(bot):
    bot.events.append("should not run")
    return []
""")

    async def run():
        manager.load_plugins()
        coroutine = manager._pending_async_setup[0].awaitable
        await manager.teardown()
        assert inspect.getcoroutinestate(coroutine) == inspect.CORO_CLOSED
        assert manager._pending_async_setup == []
        assert manager.loaded_plugins == {}
        assert bot.events == []

    asyncio.run(run())


def test_reload_closes_old_resources_before_new_setup_and_retires_old_context(runtime):
    bot, manager, write = runtime
    write("lifecycle_reload", TOOL_SOURCE + """
def setup(bot, ctx):
    bot.events.append("setup")
    ctx.register_service("resource", len(bot.events))
    return [DemoTool()]
async def teardown(bot, ctx):
    bot.events.append(("closed", ctx.service("resource")))
""")

    async def run():
        manager.load_plugins()
        old = manager.loaded_plugins["lifecycle_reload"]["context"]
        assert await manager.reload_plugins_async() == "Reloaded 1 plugin(s) with 1 tool(s)."
        assert bot.events == ["setup", ("closed", 1), "setup"]
        with pytest.raises(RuntimeError, match="unloaded"):
            old.register_service("resource", "corrupted")
        assert manager.services.get("resource") == 3
        await manager.teardown()

    asyncio.run(run())


def test_shutdown_cancels_active_event_and_managed_task_finalizers(runtime):
    bot, manager, write = runtime
    write("lifecycle_runtime", """
import asyncio
async def worker(bot, key):
    bot.started[key].set()
    try:
        await asyncio.Event().wait()
    finally:
        bot.events.append(key + " stopped")
def setup(bot, ctx):
    async def event():
        await worker(bot, "event")
    ctx.on_event("on_ready", event)
    ctx.spawn(worker(bot, "background"))
    return []
async def teardown(bot):
    bot.events.append("teardown")
""")

    async def run():
        bot.started = {key: asyncio.Event() for key in ("event", "background")}
        manager.load_plugins()
        dispatch = asyncio.create_task(manager.dispatch_event("on_ready"))
        await asyncio.gather(*(event.wait() for event in bot.started.values()))
        await manager.teardown()
        await dispatch
        assert set(bot.events[:-1]) == {"event stopped", "background stopped"}
        assert bot.events[-1] == "teardown"
        assert manager._event_tasks == {}
        assert manager._maxwell_managed_tasks == {}

    asyncio.run(run())


def test_wrapper_removal_preserves_other_plugins_in_any_order(runtime):
    _, manager, _ = runtime
    tool = SimpleNamespace(execute=lambda text: text)
    manager.register_context_tool("base", tool, name="wrapped")

    def wrapper(prefix):
        return lambda original: lambda text: prefix + original(text)

    manager.wrap_tool("first", "wrapped", wrapper("a"))
    manager.wrap_tool("second", "wrapped", wrapper("b"))
    manager.wrap_tool("first", "wrapped", wrapper("c"))
    assert tool.execute("x") == "cbax"
    manager._unwrap_tools("first")
    assert tool.execute("x") == "bx"
    manager._unwrap_tools("second")
    assert tool.execute("x") == "x"


def test_managed_tasks_accept_futures_and_release_completed_references(runtime):
    _, manager, _ = runtime

    async def run():
        future = asyncio.get_running_loop().create_future()
        first = manager.spawn_task("demo", future)
        second = manager.spawn_task("demo", asyncio.sleep(0))
        future.set_result("result")
        assert await first == "result"
        await second
        await asyncio.sleep(0)
        assert manager._maxwell_managed_tasks["demo"] == set()

    asyncio.run(run())


@pytest.mark.parametrize("delay", [float("nan"), float("inf"), -float("inf"), -1])
def test_delayed_tasks_reject_invalid_delays(runtime, delay):
    _, manager, _ = runtime
    with pytest.raises(ValueError, match="seconds"):
        manager.spawn_after("demo", delay, lambda: None)


def test_retired_context_closes_rejected_coroutine(runtime):
    _, manager, _ = runtime
    ctx = PluginContext(manager, "demo")
    ctx._retire()
    coroutine = asyncio.sleep(0)
    with pytest.raises(RuntimeError, match="unloaded"):
        ctx.spawn(coroutine)
    assert inspect.getcoroutinestate(coroutine) == inspect.CORO_CLOSED


@pytest.mark.parametrize("invalid", [{"data_version": "bad"}, {"data_version": 0}, {"tools": [{"name": "demo", "transports": 123}]}])
def test_malformed_manifest_isolated_from_other_plugins(runtime, invalid):
    _, manager, write = runtime
    write("lifecycle_bad_manifest", "raise AssertionError('must not import')", **invalid)
    write("lifecycle_valid_manifest", "def setup(bot):\n    return []\n")
    manager.load_plugins()
    assert set(manager.loaded_plugins) == {"lifecycle_valid_manifest"}
    assert "lifecycle_bad_manifest" in manager.load_errors
