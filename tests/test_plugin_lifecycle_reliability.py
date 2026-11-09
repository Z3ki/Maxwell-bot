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


@pytest.mark.parametrize("invalid", [
    {"data_version": "bad"},
    {"data_version": 0},
    {"tools": [{"name": "demo", "transports": 123}]},
    {"api_version": float("inf")},
    {"manifest_version": float("inf")},
    *({field: 123} for field in (
        "permissions", "requested_permissions", "required_capabilities",
        "requires", "provided_capabilities", "provides",
    )),
])
def test_malformed_manifest_isolated_from_other_plugins(runtime, invalid):
    _, manager, write = runtime
    write("lifecycle_bad_manifest", "raise AssertionError('must not import')", **invalid)
    write("lifecycle_valid_manifest", "def setup(bot):\n    return []\n")
    manager.load_plugins()
    assert set(manager.loaded_plugins) == {"lifecycle_valid_manifest"}
    assert "lifecycle_bad_manifest" in manager.load_errors


def test_non_utf8_manifest_does_not_abort_other_plugin_loading(runtime):
    _, manager, write = runtime
    bad = write("lifecycle_bad_encoding", "raise AssertionError('must not import')")
    bad.joinpath("plugin.json").write_bytes(b'{"description": "\xff"}')
    write("lifecycle_valid_encoding", "def setup(bot):\n    return []\n")

    manager.load_plugins()

    assert set(manager.loaded_plugins) == {"lifecycle_valid_encoding"}
    assert "cannot read" in manager.load_errors["lifecycle_bad_encoding"]


@pytest.mark.parametrize("target_kind", ["absolute", "installed_root", "nested"])
def test_uninstall_rejects_paths_without_moving_unrelated_code(runtime, tmp_path, target_kind):
    _, manager, _ = runtime
    root = manager.installed_plugins_dir()
    if target_kind == "absolute":
        victim = tmp_path / "unrelated"
        argument = str(victim)
    elif target_kind == "installed_root":
        victim = root
        argument = "."
    else:
        victim = root / "parent" / "child"
        argument = "parent/child"
    victim.mkdir(parents=True, exist_ok=True)
    sentinel = victim / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")

    result = asyncio.run(manager.uninstall_plugin(argument))

    assert result.startswith("Error:")
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert not (manager.data_dir / "plugin_trash").exists()


@pytest.mark.parametrize("target", ["parent/child", "other_plugin"])
def test_uninstall_rejects_symlink_to_other_installed_directory(runtime, target):
    _, manager, _ = runtime
    root = manager.installed_plugins_dir()
    victim = root / target
    victim.mkdir(parents=True)
    sentinel = victim / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    (root / "alias").symlink_to(victim, target_is_directory=True)

    result = asyncio.run(manager.uninstall_plugin("alias"))

    assert "not independently installed" in result
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_uninstall_honors_manifest_uninstallable_flag(runtime):
    _, manager, write = runtime
    manager.plugins_dir = manager.installed_plugins_dir()
    directory = write(
        "lifecycle_permanent", "def setup(bot):\n    return []\n",
        uninstallable=False,
    )
    manager.load_plugins()

    result = asyncio.run(manager.uninstall_plugin("lifecycle_permanent"))

    assert "does not allow uninstallation" in result
    assert directory.is_dir()
    assert "lifecycle_permanent" in manager.loaded_plugins


def test_user_disable_overrides_server_enablement_for_tools_and_events(runtime):
    bot, manager, write = runtime
    write("lifecycle_scoped", TOOL_SOURCE + """
def setup(bot, ctx):
    async def listener(message):
        bot.events.append("received")
    ctx.on_event("on_message", listener)
    return [DemoTool()]
""", enabled_globally=False)
    bot._control = {"guild_plugin_overrides": {"10": {"lifecycle_scoped": True}}}
    manager.load_plugins()
    message = SimpleNamespace(
        author=SimpleNamespace(id=123), guild=SimpleNamespace(id=10)
    )
    assert "lifecycle_echo" in manager.get_available_tools("123", guild_id="10")

    assert "disabled for user" in manager.disable_plugin("lifecycle_scoped", user_id="123")

    assert manager.get_available_tools("123", guild_id="10") == {}
    assert asyncio.run(manager.dispatch_event("on_message", message)) == 0
    assert bot.events == []
    assert "enabled for user" in manager.enable_plugin("lifecycle_scoped", user_id="123")
    assert "lifecycle_echo" in manager.get_available_tools("123", guild_id="10")


def test_denial_limit_failure_does_not_partially_disable_allowed_user(runtime):
    _, manager, write = runtime
    write("lifecycle_full_denials", "def setup(bot):\n    return []\n")
    manager.load_plugins()
    cfg = manager.state["plugins"]["lifecycle_full_denials"]
    cfg["allowed_users"] = ["999"]
    cfg["denied_users"] = [str(uid) for uid in range(500)]

    result = manager.disable_plugin("lifecycle_full_denials", user_id="999")

    assert "denial limit" in result
    assert cfg["allowed_users"] == ["999"]
    assert manager.is_plugin_enabled_for_user("lifecycle_full_denials", "999")


@pytest.mark.parametrize("has_teardown", [False, True])
def test_failed_setup_waits_for_background_finalizers(runtime, has_teardown):
    bot, manager, write = runtime
    source = """
import asyncio
async def setup(bot, ctx):
    async def worker():
        bot.started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await bot.release.wait()
            bot.events.append("background stopped")
    ctx.spawn(worker())
    await bot.started.wait()
    raise RuntimeError("setup failed")
"""
    if has_teardown:
        source += """
def teardown(bot, ctx):
    bot.events.append("teardown")
"""
    write("lifecycle_failed_worker", source)

    async def run():
        bot.started = asyncio.Event()
        bot.release = asyncio.Event()
        manager.load_plugins()
        completion = asyncio.create_task(manager.complete_pending_setups())
        await bot.started.wait()
        await asyncio.sleep(0)
        assert not completion.done()
        bot.release.set()
        await completion
        expected = ["background stopped"] + (["teardown"] if has_teardown else [])
        assert bot.events == expected
        assert manager.loaded_plugins == {}
        assert manager._pending_task_cleanup == []

    asyncio.run(run())


def test_uninstall_awaits_runtime_and_teardown_before_removing_code(runtime):
    bot, manager, write = runtime
    manager.plugins_dir = manager.installed_plugins_dir()
    directory = write("lifecycle_uninstall", TOOL_SOURCE + """
import asyncio
async def worker(bot, ctx, key):
    bot.started[key].set()
    try:
        await asyncio.Event().wait()
    finally:
        await bot.release.wait()
        bot.events.append((key, ctx.service("resource")))
def setup(bot, ctx):
    ctx.register_service("resource", "alive")
    ctx.spawn(worker(bot, ctx, "background"))
    async def event():
        await worker(bot, ctx, "event")
    ctx.on_event("on_ready", event)
    async def job():
        await worker(bot, ctx, "job")
    ctx.every(5, job, run_immediately=True)
    return [DemoTool()]
async def teardown(bot, ctx):
    bot.events.append(("teardown", ctx.service("resource")))
    assert ctx.store_path("keep.txt").read_text() == "keep"
    assert bot.directory.is_dir()
""")
    bot.directory = directory
    manager.get_plugin_data_dir("lifecycle_uninstall").joinpath("keep.txt").write_text("keep")

    async def run():
        bot.started = {key: asyncio.Event() for key in ("background", "event", "job")}
        bot.release = asyncio.Event()
        manager.load_plugins()
        context = manager.loaded_plugins["lifecycle_uninstall"]["context"]
        assert manager.start_jobs() == 1
        dispatch = asyncio.create_task(manager.dispatch_event("on_ready"))
        await asyncio.gather(*(event.wait() for event in bot.started.values()))
        uninstall = asyncio.create_task(manager.uninstall_plugin("lifecycle_uninstall"))
        await asyncio.sleep(0)
        assert not uninstall.done()
        assert directory.is_dir()
        bot.release.set()
        result = await uninstall
        await dispatch
        assert "Uninstalled" in result
        assert set(bot.events[:-1]) == {("background", "alive"), ("event", "alive"), ("job", "alive")}
        assert bot.events[-1] == ("teardown", "alive")
        assert not directory.exists()
        assert manager.loaded_plugins == {}
        assert manager.tool_registry.names() == []
        assert manager.services.names() == []
        assert manager._job_specs == {}
        assert manager._event_tasks == {}
        assert manager._maxwell_managed_tasks == {}
        assert bot.tools == {}
        assert context.store_path("keep.txt").read_text() == "keep"
        with pytest.raises(RuntimeError, match="unloaded"):
            context.register_service("resource", "resurrected")

    asyncio.run(run())


@pytest.mark.parametrize("asynchronous", [False, True])
def test_uninstall_refuses_required_dependency_without_stopping_it(runtime, asynchronous):
    bot, manager, write = runtime
    manager.plugins_dir = manager.installed_plugins_dir()
    directory = write("lifecycle_required", """
def setup(bot, ctx):
    ctx.register_service("required_service", "alive")
    return []
def teardown(bot):
    bot.events.append("closed")
""")
    write("lifecycle_consumer", (
        "async def setup(bot):\n    return []\n" if asynchronous
        else "def setup(bot):\n    return []\n"
    ), dependencies=["lifecycle_required"])

    async def run():
        manager.load_plugins()
        result = await manager.uninstall_plugin("lifecycle_required")
        assert "required by: lifecycle_consumer" in result
        assert directory.is_dir()
        assert bot.events == []
        assert manager.services.get("required_service") == "alive"
        assert manager.is_plugin_enabled_for_user("lifecycle_required", "1")
        await manager.teardown()

    asyncio.run(run())


def test_retired_context_does_not_inherit_replacement_capabilities(runtime):
    _, manager, write = runtime
    directory = write(
        "lifecycle_permissions", "def setup(bot):\n    return []\n",
        permissions=["network"],
    )
    manager.load_plugins()
    previous = manager.loaded_plugins["lifecycle_permissions"]["context"]
    directory.joinpath("plugin.json").write_text(json.dumps({
        "id": "lifecycle_permissions", "permissions": ["shell"],
    }))

    async def run():
        await manager.reload_plugins_async()
        current = manager.loaded_plugins["lifecycle_permissions"]["context"]
        assert current.has_permission("shell")
        assert previous.granted_permissions() == ["network"]
        permissions = previous.granted_permissions()
        permissions.append("shell")
        with pytest.raises(PermissionError, match="shell"):
            previous.require_capability("shell")
        previous.require_capability("network")
        await manager.teardown()

    asyncio.run(run())


@pytest.mark.parametrize("field", [
    "permissions", "requested_permissions", "required_capabilities",
    "requires", "provided_capabilities", "provides",
])
def test_manifest_permission_maps_do_not_grant_false_capabilities(runtime, field):
    bot, manager, write = runtime
    write("lifecycle_permission_map", """
def setup(bot, ctx):
    ctx.require_capability("shell")
    bot.events.append("shell granted")
    return []
""", **{field: {"shell": False}})

    manager.load_plugins()

    assert manager.loaded_plugins == {}
    assert bot.events == []
    assert "must be a string or a list" in manager.load_errors["lifecycle_permission_map"]


def test_runtime_rejects_manifest_identity_mismatch_before_import(runtime):
    bot, manager, write = runtime
    write(
        "lifecycle_directory", "raise AssertionError('mismatched plugin imported')",
        id="lifecycle_declared",
    )
    write(
        "lifecycle_identity_consumer",
        "raise AssertionError('dependent imported')",
        dependencies=["lifecycle_declared"],
    )
    write("lifecycle_identity_valid", "def setup(bot):\n    bot.events.append('valid')\n")

    manager.load_plugins()

    assert set(manager.loaded_plugins) == {"lifecycle_identity_valid"}
    assert bot.events == ["valid"]
    assert "must match directory" in manager.load_errors["lifecycle_directory"]
    assert "missing required plugin dependencies" in manager.load_errors["lifecycle_identity_consumer"]
