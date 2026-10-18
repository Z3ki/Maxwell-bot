"""Plugin host surface: PluginContext, services, hooks and manifests."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from maxwell_core import PLUGIN_API_VERSION
from maxwell_core.capabilities import missing_capabilities, normalize_capabilities
from maxwell_core.hooks import HOOK_NAMES, HookBus, HookPayload, HookResult
from maxwell_core.plugins.context import MIN_JOB_INTERVAL_SECONDS, PluginContext
from maxwell_core.plugins.manifest import (
    ManifestError,
    load_manifest_file,
    validate_manifest,
)
from maxwell_core.prompts.component import PromptComponent
from maxwell_core.prompts.manager import PromptManager
from maxwell_core.services import ServiceContainer
from maxwell_core.tools.registry import ToolRegistry

HOOK = sorted(HOOK_NAMES)[0]


class FakeManager:
    def __init__(self, tmp_path, *, granted=(), bot=None):
        self.bot = bot if bot is not None else SimpleNamespace(tools={})
        self.services = ServiceContainer()
        self.tool_registry = ToolRegistry()
        self.prompts = PromptManager()
        self.hooks = HookBus()
        self._granted = list(granted)
        self._tmp = tmp_path
        self.listeners, self.jobs, self.extensions = [], [], []
        self.spawned, self.views, self.dynamic = [], [], []

    def get_plugin_data_dir(self, name):
        path = self._tmp / name
        path.mkdir(exist_ok=True)
        return path

    def plugin_config(self, name):
        return {"plugin": name}

    def granted_permissions(self, name):
        return self._granted

    def _register_listener(self, plugin, event, cb):
        self.listeners.append((plugin, event, cb))

    def _register_job(self, plugin, interval, cb, *, run_immediately):
        self.jobs.append((plugin, interval, run_immediately))

    def _register_extension(self, plugin, kind, key, value):
        self.extensions.append((plugin, kind, key, value))

    def spawn_task(self, plugin, awaitable, *, name=None):
        self.spawned.append((plugin, name))
        awaitable.close()
        return "task"

    def _track_view(self, plugin, view):
        self.views.append((plugin, view))

    def _track_dynamic_items(self, plugin, items):
        self.dynamic.append((plugin, items))


@pytest.fixture
def ctx(tmp_path):
    return PluginContext(FakeManager(tmp_path, granted=["network"]), "demo")


async def _noop(*_a, **_k):
    return None


# --------------------------------------------------------------------------
# PluginContext
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["../../etc/passwd", "sub/dir/file.json"])
def test_store_path_stays_inside_the_plugin_dir(ctx, tmp_path, name):
    path = ctx.store_path(name)
    assert path.parent == tmp_path / "demo"
    assert path.name == name.rsplit("/", 1)[-1]


@pytest.mark.parametrize("name", ["", "  ", "..", ".", "dir/", None])
def test_store_path_rejects_empty_or_traversal_names(ctx, name):
    with pytest.raises(ValueError):
        ctx.store_path(name)


def test_config_and_permissions(ctx):
    assert ctx.config == {"plugin": "demo"}
    assert ctx.has_permission("network") and not ctx.has_permission("shell")
    ctx.require_capability("network")
    with pytest.raises(PermissionError, match="shell"):
        ctx.require_capability("network", "shell")


def test_on_event_validation(ctx):
    with pytest.raises(ValueError, match="not a subscribable event"):
        ctx.on_event("on_everything", _noop)
    with pytest.raises(TypeError, match="callable"):
        ctx.on_event("on_message", "nope")
    with pytest.raises(TypeError, match="async def"):
        ctx.on_event("on_message", lambda *_: None)
    ctx.on_event("on_message", _noop)
    assert ctx._manager.listeners == [("demo", "on_message", _noop)]


@pytest.mark.parametrize(
    "seconds", [0, MIN_JOB_INTERVAL_SECONDS - 0.1, float("inf"), float("nan"), "x"]
)
def test_every_rejects_tight_or_invalid_intervals(ctx, seconds):
    with pytest.raises(ValueError):
        ctx.every(seconds, _noop)


def test_every_requires_async_callback_and_registers(ctx):
    with pytest.raises(TypeError):
        ctx.every(10, lambda: None)
    ctx.every("7.5", _noop, run_immediately=1)
    assert ctx._manager.jobs == [("demo", 7.5, True)]


def test_retired_context_refuses_new_work_and_closes_coroutines(ctx):
    ctx._retire()
    for call in (
        lambda: ctx.on_event("on_message", _noop),
        lambda: ctx.every(10, _noop),
        lambda: ctx.register_tool(object()),
        lambda: ctx.register_service("s", 1),
        lambda: ctx.register_command(SimpleNamespace(name="c")),
    ):
        with pytest.raises(RuntimeError, match="unloaded"):
            call()
    coro = _noop()
    with pytest.raises(RuntimeError):
        ctx.spawn(coro)
    # The coroutine was closed, so no "never awaited" warning is emitted.
    assert coro.cr_frame is None


def test_spawn_delegates_while_active(ctx):
    assert ctx.spawn(_noop(), name="job") == "task"
    assert ctx._manager.spawned == [("demo", "job")]


def test_register_prompt_cannot_impersonate_core(ctx):
    ctx.register_prompt(PromptComponent(id="demo.style", plugin="core", text="x"))
    stored = ctx._manager.prompts._components["demo.style"]
    assert stored.plugin == "demo"


def test_register_hook_validates_name(ctx):
    with pytest.raises(ValueError, match="Unknown hook"):
        ctx.register_hook("not_a_hook", _noop)
    ctx.register_hook(HOOK, _noop, priority=5)
    assert ctx._manager.hooks.plugin_hooks("demo") == [HOOK]


def test_register_provider_memory_and_command_names(ctx):
    with pytest.raises(ValueError):
        ctx.register_provider(SimpleNamespace(name="  "))
    ctx.register_provider(SimpleNamespace(name="llm"))
    assert ctx.service("provider:llm").name == "llm"
    ctx.register_memory(SimpleNamespace())
    assert ctx.service("memory:memory") is not None
    with pytest.raises(ValueError):
        ctx.register_command(SimpleNamespace())
    ctx.register_command(SimpleNamespace(name="ping"))
    kinds = [(k, key) for _, k, key, _ in ctx._manager.extensions]
    assert ("provider", "llm") in kinds and ("command", "ping") in kinds


@pytest.mark.parametrize(
    "path",
    [
        "/api/control",
        "/api/plugin/other/x",
        "/api/plugin/demo2/x",
        "api/plugin/demo/x",
        "",
    ],
)
def test_api_routes_must_live_under_the_plugin_prefix(ctx, path):
    with pytest.raises(ValueError):
        ctx.register_api_route("POST", path, _noop)


def test_api_route_registration_defaults_to_owner_only(ctx):
    ctx.register_api_route("get", "/api/plugin/demo/status", _noop)
    ctx.register_api_route("post", "/api/plugin/demo", _noop, owner_only=False)
    routes = {
        key: value
        for _, kind, key, value in ctx._manager.extensions
        if kind == "api_route"
    }
    assert routes["GET /api/plugin/demo/status"]["owner_only"] is True
    assert routes["POST /api/plugin/demo"]["owner_only"] is False


def test_dashboard_panel_needs_id_or_title(ctx):
    with pytest.raises(ValueError):
        ctx.register_dashboard_panel({})
    with pytest.raises(ValueError):
        ctx.register_dashboard_panel(None)
    panel = {"title": "Stats"}
    ctx.register_dashboard_panel(panel)
    stored = ctx._manager.extensions[-1][3]
    assert stored == panel and stored is not panel


def test_views_require_bot_support(tmp_path):
    bare = PluginContext(FakeManager(tmp_path, bot=SimpleNamespace()), "demo")
    with pytest.raises(TypeError):
        bare.add_view(object())
    with pytest.raises(TypeError):
        bare.add_dynamic_items(object())

    added = []
    bot = SimpleNamespace(
        add_view=lambda view, message_id=None: added.append((view, message_id)),
        add_dynamic_items=lambda *items: added.append(items),
    )
    ctx = PluginContext(FakeManager(tmp_path, bot=bot), "demo")
    view = object()
    assert ctx.add_view(view, message_id=5) is view
    ctx.add_dynamic_items("a", "b")
    assert added == [(view, 5), ("a", "b")]
    assert ctx._manager.views == [("demo", view)]


def test_tool_lookup_prefers_registry_then_bot_tools_then_snapshot(tmp_path):
    legacy = SimpleNamespace(execute=_noop)
    manager = FakeManager(tmp_path, bot=SimpleNamespace(tools={"legacy": legacy}))
    registered = SimpleNamespace(execute=_noop)
    manager.tool_registry.register_tool(registered, name="reg", plugin="p")
    ctx = PluginContext(manager, "demo")
    assert ctx.tool("reg") is registered
    assert ctx.tool("legacy") is legacy
    assert ctx.tool("missing") is None
    manager.services.register("svc", "value", owner="demo")
    ctx._retire()
    manager.tool_registry.unregister_plugin("p", force=True)
    manager.services.unregister_owner("demo")
    # A retired context keeps answering from what existed at unload.
    assert ctx.tool("reg") is registered
    assert ctx.service("svc") == "value"
    assert ctx.service("gone", "default") == "default"


def test_is_admin_and_is_owner_fail_closed(tmp_path):
    def boom(_uid):
        raise RuntimeError

    for bot, admin, owner in [
        (SimpleNamespace(), False, False),
        (SimpleNamespace(_is_admin=boom, _is_owner=boom), False, False),
        (SimpleNamespace(_is_admin=lambda u: u == "1"), True, True),
        (
            SimpleNamespace(_is_admin=lambda u: True, _is_owner=lambda u: False),
            True,
            False,
        ),
    ]:
        ctx = PluginContext(FakeManager(tmp_path, bot=bot), "demo")
        assert ctx.is_admin("1") is admin
        assert ctx.is_owner("1") is owner


# --------------------------------------------------------------------------
# ServiceContainer
# --------------------------------------------------------------------------


def test_services_ownership_and_lifecycle():
    services = ServiceContainer()
    services.register("db", "conn", owner="a")
    with pytest.raises(ValueError, match="already owned"):
        services.register("db", "other", owner="b")
    services.register("db", "conn2", owner="a")  # same owner may replace
    assert services.get("db") == "conn2"
    with pytest.raises(ValueError):
        services.register(" ", 1)
    with pytest.raises(ValueError):
        services.register("x", 1, owner=" ")
    with pytest.raises(KeyError):
        services.get("missing")
    assert services.get("missing", None) is None


def test_service_factories_are_lazy_and_cached():
    calls = []
    services = ServiceContainer()
    services.factory("lazy", lambda: calls.append(1) or object(), owner="a")
    assert services.snapshot() == {}
    assert services.names() == ["lazy"]
    first = services.get("lazy")
    assert services.get("lazy") is first and calls == [1]
    with pytest.raises(TypeError):
        services.factory("bad", "not callable", owner="a")
    assert services.owned_by("a") == ["lazy"]
    assert services.unregister_owner("a") == 1
    assert services.names() == []


# --------------------------------------------------------------------------
# HookBus
# --------------------------------------------------------------------------


def test_hook_registration_validation():
    bus = HookBus()
    with pytest.raises(ValueError):
        bus.register("p", "nope", _noop)
    with pytest.raises(TypeError):
        bus.register("p", HOOK, "x")
    for bad in (0, -1, float("inf")):
        with pytest.raises(ValueError):
            bus.register("p", HOOK, _noop, timeout=bad)


def test_hooks_run_in_priority_order_and_first_stop_wins():
    bus = HookBus()
    seen = []

    def make(tag, result=None):
        def cb(bag):
            seen.append(tag)
            return result

        return cb

    bus.register("late", HOOK, make("late"), priority=200)
    bus.register("b", HOOK, make("b", {"x": 1}), priority=10)
    bus.register(
        "a", HOOK, make("a", HookResult(value={"y": 2}, stop=True)), priority=10
    )
    bag = asyncio.run(bus.emit(HOOK, {"start": True}))
    assert seen == ["a"]  # same priority sorts by plugin name; "a" stops
    assert bag["y"] == 2 and bag["stop"] is True and bag["skipped_by"] == "a"
    assert bag.get("x") is None and bag["start"] is True


def test_hook_failures_and_timeouts_are_isolated():
    bus = HookBus()

    def broken(bag):
        raise RuntimeError("plugin bug")

    async def slow(bag):
        await asyncio.sleep(10)

    async def good(bag):
        return {"ok": True}

    bus.register("a", HOOK, broken, priority=1)
    bus.register("b", HOOK, slow, priority=2, timeout=0.01)
    bus.register("c", HOOK, good, priority=3)
    bag = asyncio.run(bus.emit(HOOK))
    assert bag["ok"] is True and bag.get("stop") is None


def test_hook_plugin_filter_and_unregister():
    bus = HookBus()
    bus.register("a", HOOK, lambda bag: {"a": 1})
    bus.register("b", HOOK, lambda bag: {"b": 1})
    bag = asyncio.run(bus.emit(HOOK, plugins=["b"]))
    assert "a" not in bag.data and bag["b"] == 1
    assert bus.unregister_plugin("a") == 1
    assert [h.plugin for h in bus.handlers(HOOK)] == ["b"]


def test_hook_unloaded_mid_emit_is_not_called():
    bus = HookBus()
    called = []

    def first(bag):
        bus.unregister_plugin("z")

    bus.register("a", HOOK, first, priority=1)
    bus.register("z", HOOK, lambda bag: called.append(1), priority=2)
    asyncio.run(bus.emit(HOOK))
    assert called == []


def test_emit_sync_skips_async_callbacks_and_awaitables():
    bus = HookBus()

    async def async_cb(bag):
        return {"async": True}

    def returns_coro(bag):
        return async_cb(bag)

    bus.register("a", HOOK, async_cb, priority=1)
    bus.register("b", HOOK, returns_coro, priority=2)
    bus.register("c", HOOK, lambda bag: {"sync": True}, priority=3)
    bag = bus.emit_sync(HOOK, HookPayload({}))
    assert bag.data == {"sync": True}


# --------------------------------------------------------------------------
# capabilities + manifests
# --------------------------------------------------------------------------


def test_capability_normalization_and_missing():
    assert normalize_capabilities([" shell ", "shell", "", None, "network"]) == [
        "shell",
        "network",
    ]
    assert normalize_capabilities(None) == []
    assert missing_capabilities(["shell"], ["shell", "network", "network"]) == [
        "network"
    ]
    assert missing_capabilities(None, None) == []


def _manifest(**data):
    base = {"id": "demo", "api_version": PLUGIN_API_VERSION}
    base.update(data)
    return validate_manifest(base, directory_name="demo")


def test_manifest_defaults_and_aliases():
    m = validate_manifest({}, directory_name="from_dir")
    assert m.id == "from_dir" and m.version == "0.0.0" and m.platforms == ["any"]
    assert m.enabled_by_default is False and m.uninstallable is True
    m = _manifest(
        title="Demo",
        enabled_globally="yes",
        requires=["shell"],
        provides=["x"],
        requested_permissions=["network", "network"],
        optional_python=["numpy"],
        config={"a": 1},
    )
    assert m.name == "Demo" and m.enabled_by_default is True
    assert m.required_capabilities == ["shell"] and m.provided_capabilities == ["x"]
    assert m.permissions == ["network"] and m.optional_dependencies == ["numpy"]
    assert m.default_config == {"a": 1}


@pytest.mark.parametrize(
    "data, message",
    [
        ({"id": "bad-id"}, "valid Python identifier"),
        ({"api_version": "x"}, "api_version must be an integer"),
        ({"api_version": PLUGIN_API_VERSION + 1}, "targets API"),
        ({"manifest_version": 99}, "unsupported manifest version"),
        ({"manifest_version": "v1"}, "manifest_version must be an integer"),
        ({"entry": "../escape.py"}, "relative Python file"),
        ({"entry": "/abs/plugin.py"}, "relative Python file"),
        ({"entry": "main.sh"}, "relative Python file"),
        ({"entry": "sub\\win.py"}, "relative Python file"),
        ({"data_version": 0}, "positive integer"),
        ({"data_version": "x"}, "positive integer"),
        ({"tools": [{"name": "t", "transports": 5}]}, "transports must be a list"),
        (
            {"tools": [{"name": "t", "returns_result": True, "ends_turn": True}]},
            "cannot both return a result",
        ),
    ],
)
def test_manifest_rejections(data, message):
    with pytest.raises(ManifestError, match=message):
        _manifest(**data)


def test_manifest_tools_dedupe_and_parse():
    m = _manifest(
        tools=[
            "a",
            "a",
            " ",
            7,
            {"name": "b", "platforms": "discord", "requires_admin": "true"},
            {"name": "b"},
            {"description": "nameless"},
        ]
    )
    assert m.tool_names() == ["a", "b"]
    b = m.tools[1]
    assert b.transports == ("discord",) and b.requires_admin is True
    assert _manifest(tools="not a list").tools == []


def test_load_manifest_file_errors(tmp_path):
    bad = tmp_path / "plugin.json"
    bad.write_text("{nope", encoding="utf-8")
    with pytest.raises(ManifestError, match="invalid JSON") as info:
        load_manifest_file(bad, directory_name="demo")
    assert info.value.path == bad
    bad.write_text("[]", encoding="utf-8")
    with pytest.raises(ManifestError, match="JSON object"):
        load_manifest_file(bad, directory_name="demo")
    with pytest.raises(ManifestError, match="cannot read"):
        load_manifest_file(tmp_path / "missing.json", directory_name="demo")
    bad.write_text(json.dumps({"id": "demo"}), encoding="utf-8")
    assert load_manifest_file(bad, directory_name="demo").id == "demo"


def test_every_bundled_manifest_validates():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "plugins"
    manifests = sorted(root.glob("*/plugin.json"))
    assert manifests
    for path in manifests:
        m = load_manifest_file(path, directory_name=path.parent.name)
        assert m.id.isidentifier()
