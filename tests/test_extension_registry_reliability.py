"""Owner isolation, hook parity, and stable prompt/schema composition."""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace

import pytest

from maxwell_core.hooks import HookBus, HookResult
from maxwell_core.plugins.context import PluginContext
from maxwell_core.plugins.manager import PluginManager
from maxwell_core.prompts.component import PromptComponent, PromptRequest
from maxwell_core.prompts.manager import PromptManager
from maxwell_core.services import ServiceContainer
from maxwell_core.tools.registry import ToolRegistry
from maxwell_core.tools.spec import ToolSpec


@pytest.mark.parametrize("result", [{"skip": True}, {"stop": True}, HookResult(skip=True), HookResult(stop=True)])
def test_sync_and_async_hooks_apply_identical_stop_semantics(result):
    bus = HookBus()
    seen = []
    bus.register("first", "before_prompt", lambda bag: result, priority=1)
    bus.register("second", "before_prompt", lambda bag: seen.append("unexpected"))
    sync = bus.emit_sync("before_prompt")
    asynchronous = asyncio.run(bus.emit("before_prompt"))
    assert sync.data == asynchronous.data == {"stop": True, "skipped_by": "first", **(result if isinstance(result, dict) else {})}
    assert seen == []


@pytest.mark.parametrize("asynchronous", [False, True])
def test_hook_snapshot_does_not_execute_unloaded_handler(asynchronous):
    bus = HookBus()
    seen = []
    bus.register("first", "before_prompt", lambda bag: bus.unregister_plugin("retired"), priority=1)
    bus.register("retired", "before_prompt", lambda bag: seen.append("stale"))
    if asynchronous:
        asyncio.run(bus.emit("before_prompt"))
    else:
        bus.emit_sync("before_prompt")
    assert seen == []


def test_async_hook_timeout_cancels_callback_and_runs_remaining_plugins():
    bus = HookBus()
    seen = []

    async def hung(bag):
        try:
            await asyncio.Event().wait()
        finally:
            seen.append("cancelled")

    bus.register("hung", "before_tool", hung, priority=1, timeout=0.01)
    bus.register("healthy", "before_tool", lambda bag: {"healthy": True})
    result = asyncio.run(bus.emit("before_tool"))
    assert result["healthy"] is True
    assert seen == ["cancelled"]


def test_cancelling_hook_emit_propagates_and_cleans_callback():
    bus = HookBus()

    async def run():
        started = asyncio.Event()
        stopped = asyncio.Event()

        async def callback(bag):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        bus.register("demo", "before_tool", callback)
        task = asyncio.create_task(bus.emit("before_tool"))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stopped.is_set()

    asyncio.run(run())


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), -float("inf")])
def test_hooks_reject_unbounded_or_invalid_timeout(timeout):
    bus = HookBus()
    with pytest.raises(ValueError, match="timeout"):
        bus.register("demo", "before_prompt", lambda bag: None, timeout=timeout)
    assert bus.handlers("before_prompt") == []


def test_sync_emit_cancels_accidental_scheduled_awaitable():
    bus = HookBus()

    async def run():
        scheduled = []

        def callback(bag):
            task = asyncio.create_task(asyncio.sleep(30))
            scheduled.append(task)
            return task

        bus.register("demo", "before_prompt", callback)
        bus.emit_sync("before_prompt")
        with pytest.raises(asyncio.CancelledError):
            await scheduled[0]

    asyncio.run(run())


def test_prompt_registration_isolated_from_caller_mutations():
    manager = PromptManager()
    component = PromptComponent(id=" instructions ", plugin="demo", text="original")
    manager.register(component)
    component.id = "changed"
    component.plugin = "other"
    component.text = "changed"
    exposed = manager.components()[0]
    exposed.text = "changed again"
    assert manager.assemble(PromptRequest(), enabled_plugins=["demo"]) == "original"
    assert manager.plugin_components("demo") == ["instructions"]
    assert manager.unregister_plugin("demo") == 1
    assert manager.components() == []


def test_prompt_predicate_can_register_component_without_mutating_current_iteration():
    manager = PromptManager()

    def predicate(request):
        manager.register(PromptComponent(id="new", plugin="demo", text="new"))
        return True

    manager.register(PromptComponent(id="original", plugin="demo", text="original", when=predicate))
    assert manager.assemble(PromptRequest()) == "original"
    assert manager.assemble(PromptRequest()) == "new\n\noriginal"


def test_same_prompt_object_does_not_transfer_ownership_between_plugin_contexts(tmp_path):
    manager = PluginManager(SimpleNamespace(tools={}), plugins_dir=str(tmp_path / "plugins"), data_dir=str(tmp_path / "data"))
    first = PluginContext(manager, "first")
    second = PluginContext(manager, "second")
    component = PromptComponent(id="shared", plugin="placeholder", text="original")
    first.register_prompt(component)
    with pytest.raises(ValueError, match="already registered"):
        second.register_prompt(component)
    assert component.plugin == "placeholder"
    assert manager.prompts.plugin_components("first") == ["shared"]
    manager.prompts.unregister_plugin("second")
    assert manager.prompts.assemble(PromptRequest(), enabled_plugins=["first"]) == "original"


@pytest.mark.parametrize("callback_field", ["when", "render"])
def test_prompt_manager_rejects_async_renderers_and_predicates(callback_field):
    async def asynchronous(request):
        return "bad"

    manager = PromptManager()
    with pytest.raises(TypeError, match="synchronous"):
        manager.register(PromptComponent(id="demo", plugin="demo", **{callback_field: asynchronous}))
    assert manager.components() == []


def test_prompt_callbacks_returning_awaitables_are_closed_and_not_injected():
    created = []

    def accidental(request):
        coroutine = asyncio.sleep(0)
        created.append(coroutine)
        return coroutine

    manager = PromptManager()
    manager.register(PromptComponent(id="predicate", plugin="demo", text="must not include", when=accidental))
    manager.register(PromptComponent(id="renderer", plugin="demo", text="fallback", render=accidental))
    assert manager.assemble(PromptRequest()) == "fallback"
    assert all(inspect.getcoroutinestate(coroutine) == inspect.CORO_CLOSED for coroutine in created)


@pytest.mark.parametrize("existing_kind", ["service", "factory"])
@pytest.mark.parametrize("replacement_kind", ["service", "factory"])
def test_service_ownership_cannot_be_stolen_before_or_after_materialization(existing_kind, replacement_kind):
    services = ServiceContainer()
    if existing_kind == "service":
        services.register("provider:primary", "original", owner="core")
    else:
        services.factory("provider:primary", lambda: "original", owner="core")
    with pytest.raises(ValueError, match="owned"):
        if replacement_kind == "service":
            services.register("provider:primary", "malicious", owner="plugin")
        else:
            services.factory("provider:primary", lambda: "malicious", owner="plugin")
    assert services.get("provider:primary") == "original"
    assert services.unregister_owner("plugin") == 0
    assert services.owned_by("core") == ["provider:primary"]


def test_same_owner_factory_replacement_invalidates_cached_instance():
    services = ServiceContainer()
    services.register("provider", "old", owner="demo")
    services.factory("provider", lambda: "new", owner="demo")
    assert services.get("provider") == "new"
    services.register("provider", "direct", owner="demo")
    assert services.get("provider") == "direct"
    assert services.unregister_owner("demo") == 1
    assert services.names() == []


def test_service_snapshot_does_not_initialize_factories():
    services = ServiceContainer()
    services.register("live", "instance", owner="demo")
    services.factory("unused", lambda: pytest.fail("factory ran during cleanup"), owner="demo")
    snapshot = services.snapshot()
    assert snapshot == {"live": "instance"}
    snapshot.clear()
    assert services.get("live") == "instance"
    assert services.unregister_owner("demo") == 2


def test_tool_spec_schema_is_detached_from_provider_mutation():
    source = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}
    spec = ToolSpec(name="demo", plugin="demo", tool=object(), parameters=source)
    exported = spec.openai_tool()["function"]["parameters"]
    exported["properties"]["text"]["type"] = "integer"
    exported["required"].clear()
    assert spec.schema() == source
    assert source["properties"]["text"]["type"] == "string"
    assert source["required"] == ["text"]


def test_registry_canonical_names_and_retained_protection_keep_owner_index():
    registry = ToolRegistry()
    spec = ToolSpec(name=" demo ", plugin="first", tool=SimpleNamespace(execute=lambda: None))
    registry.register(spec, protected=True)
    assert registry.names() == ["demo"]
    assert registry.get("demo").name == "demo"
    assert registry.unregister_plugin("first") == []
    assert registry.plugin_tools("first") == ["demo"]
    assert registry.unregister_plugin("first", force=True) == ["demo"]
    assert registry.plugin_tools("first") == []


def test_dynamic_schema_catalog_refreshes_and_preserves_another_manager(tmp_path, monkeypatch):
    import tool_schemas

    base_schema = {"type": "object", "properties": {"base": {"type": "string"}}}
    monkeypatch.setattr(tool_schemas, "TOOL_PARAMETERS", {"builtin": base_schema})
    monkeypatch.setattr(tool_schemas, "_maxwell_plugin_publications", None, raising=False)
    managers = [PluginManager(SimpleNamespace(tools={}), plugins_dir=str(tmp_path / str(index) / "plugins"), data_dir=str(tmp_path / str(index) / "data")) for index in range(2)]

    def register(manager, key):
        tool = SimpleNamespace(execute=lambda: None, get_parameters=lambda: {"type": "object", "properties": {key: {"type": "string"}}}, returns_result=True)
        manager.register_context_tool("demo", tool, name="dynamic")
        manager._publish_result_tools()

    register(managers[0], "first")
    register(managers[1], "second")
    assert set(tool_schemas.TOOL_PARAMETERS["dynamic"]["properties"]) == {"second"}
    managers[1]._clear_plugin_registrations("demo")
    managers[1]._publish_result_tools()
    assert set(tool_schemas.TOOL_PARAMETERS["dynamic"]["properties"]) == {"first"}
    managers[0]._clear_plugin_registrations("demo")
    register(managers[0], "updated")
    assert set(tool_schemas.TOOL_PARAMETERS["dynamic"]["properties"]) == {"updated"}
    managers[1].tool_registry.register(ToolSpec(name="builtin", plugin="demo", tool=object(), parameters={"type": "object"}))
    managers[1]._publish_result_tools()
    assert tool_schemas.TOOL_PARAMETERS["builtin"] is base_schema
    managers[0]._clear_plugin_registrations("demo")
    managers[0]._publish_result_tools()
    assert "dynamic" not in tool_schemas.TOOL_PARAMETERS
    assert tool_schemas.TOOL_PARAMETERS["builtin"] is base_schema
