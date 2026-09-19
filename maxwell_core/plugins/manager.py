"""Plugin loading, lifecycle, registries, and the host-facing manager."""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import inspect
import json
import logging
import os
import shutil
import sys
import time
from importlib import import_module, reload
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from maxwell_core.hooks import HookBus
from maxwell_core.plugins.catalog import iter_plugin_dirs
from maxwell_core.plugins.context import PluginContext
from maxwell_core.plugins.manifest import (
    ManifestError,
    PluginManifest,
    load_manifest_file,
    validate_manifest,
)
from maxwell_core.prompts.manager import PromptManager
from maxwell_core.services import ServiceContainer
from maxwell_core.tools.registry import ToolRegistry, set_global_registry

logger = logging.getLogger("maxwell.plugins")

_EVENT_TIMEOUT = max(1.0, float(os.getenv("MAXWELL_PLUGIN_EVENT_TIMEOUT", "15") or 15))
_JOB_TIMEOUT = max(5.0, float(os.getenv("MAXWELL_PLUGIN_JOB_TIMEOUT", "120") or 120))
_MAX_TRACKED_TASKS_PER_PLUGIN = 32


def _topo_sort(nodes: dict[str, list[str]]) -> list[str]:
    """Return a dependency-respecting order. Raises ValueError on cycles."""
    incoming: dict[str, int] = dict.fromkeys(nodes, 0)
    edges: dict[str, list[str]] = {name: [] for name in nodes}
    for name, deps in nodes.items():
        for dep in deps:
            if dep not in nodes:
                continue
            edges[dep].append(name)
            incoming[name] += 1
    queue = sorted(name for name, count in incoming.items() if count == 0)
    out: list[str] = []
    while queue:
        name = queue.pop(0)
        out.append(name)
        for child in edges[name]:
            incoming[child] -= 1
            if incoming[child] == 0:
                queue.append(child)
                queue.sort()
    if len(out) != len(nodes):
        leftover = [name for name, count in incoming.items() if count]
        raise ValueError(f"dependency cycle among plugins: {', '.join(leftover)}")
    return out


class PluginManager:
    """Manages dynamic modular plugins for Maxwell."""

    def __init__(
        self,
        bot,
        plugins_dir: Optional[str] = None,
        data_dir: Optional[str] = None,
        state_file: Optional[str] = None,
        extra_plugin_dirs: Optional[list[str]] = None,
    ):
        self.bot = bot
        self.root_dir = Path(os.path.abspath(os.path.dirname(__file__))).parents[1]
        # Historical default: repo_root/plugins. The core package lives one
        # level deeper, so parents[1] is the checkout root.
        self.plugins_dir = (
            Path(plugins_dir) if plugins_dir else (self.root_dir / "plugins")
        )
        self.data_dir = Path(data_dir) if data_dir else (self.root_dir / "data")
        self.state_file = (
            Path(state_file) if state_file else (self.data_dir / "plugins.json")
        )
        self.extra_plugin_dirs = [Path(p) for p in (extra_plugin_dirs or [])]
        if plugins_dir is None:
            installed = self.data_dir / "installed_plugins"
            installed.mkdir(parents=True, exist_ok=True)
            if installed not in self.extra_plugin_dirs:
                self.extra_plugin_dirs.append(installed)

        self.plugins_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.loaded_plugins: Dict[str, Dict[str, Any]] = {}
        self.all_plugin_tools: Dict[str, tuple[str, Any]] = {}
        self.load_errors: Dict[str, str] = {}

        self._listeners: Dict[str, List[tuple[str, Callable[..., Any]]]] = {}
        self._job_specs: Dict[str, List[dict]] = {}
        self._job_tasks: Dict[str, List[asyncio.Task]] = {}
        self._jobs_started = False
        self._extensions: Dict[str, Dict[str, dict[str, Any]]] = {}
        self._maxwell_managed_tasks: Dict[str, set[asyncio.Task]] = {}
        self._maxwell_runtime_stats: Dict[str, dict[str, Any]] = {}
        self._maxwell_plugin_views: Dict[str, set[Any]] = {}
        self._maxwell_dynamic_items: Dict[str, set[Any]] = {}
        self._pending_async_setup: list[Any] = []
        self._tool_wraps: Dict[str, list[tuple[Any, Any]]] = {}

        self.hooks = HookBus()
        self.prompts = PromptManager()
        self.services = ServiceContainer()
        self.tool_registry = ToolRegistry()
        set_global_registry(self.tool_registry)

        self.state: Dict[str, Any] = {"plugins": {}}
        self._load_state()

    # ------------------------------------------------------------------ #
    # state
    # ------------------------------------------------------------------ #

    def _load_state(self) -> None:
        loaded = None
        if self.state_file.exists():
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
            except Exception as e:
                logger.error(f"Failed to load plugins.json: {e}")
        raw_plugins = loaded.get("plugins") if isinstance(loaded, dict) else {}
        if not isinstance(raw_plugins, dict):
            raw_plugins = {}
        plugins = {}
        for name, raw_cfg in raw_plugins.items():
            if not isinstance(raw_cfg, dict):
                continue
            allowed = raw_cfg.get("allowed_users", [])
            denied = raw_cfg.get("denied_users", [])
            plugins[str(name)] = {
                "enabled_globally": self._as_bool(
                    raw_cfg.get("enabled_globally"), False
                ),
                "allowed_users": self._user_ids(allowed),
                "denied_users": self._user_ids(denied),
                "config": dict(raw_cfg.get("config") or {})
                if isinstance(raw_cfg.get("config"), dict)
                else {},
            }
        self.state = {"plugins": plugins}
        if (
            loaded is None
            or not isinstance(loaded, dict)
            or loaded.get("plugins") != plugins
        ):
            self._save_state()

    @staticmethod
    def _as_bool(value, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        text = str(value).strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
        return default

    @staticmethod
    def _user_ids(value) -> list[str]:
        if not isinstance(value, (list, tuple, set)):
            return []
        result = []
        for item in value:
            uid = str(item).strip()
            if uid and uid.isdigit() and uid not in result:
                result.append(uid)
        return result[:500]

    def _save_state(self) -> None:
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            temp_file = self.state_file.with_suffix(".tmp")
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(self.state, f, indent=2)
            temp_file.replace(self.state_file)
        except Exception as e:
            logger.error(f"Failed to save plugins.json: {e}")

    def get_plugin_data_dir(self, plugin_name: str) -> Path:
        safe = os.path.basename(str(plugin_name or "").strip())
        if not safe or not safe.isidentifier():
            raise ValueError(f"invalid plugin name: {plugin_name!r}")
        pdir = self.data_dir / "plugins" / safe
        pdir.mkdir(parents=True, exist_ok=True)
        return pdir

    def plugin_config(self, plugin_name: str) -> dict[str, Any]:
        data = self.loaded_plugins.get(plugin_name) or {}
        manifest: PluginManifest | None = data.get("typed_manifest")
        defaults = dict(manifest.default_config) if manifest is not None else {}
        stored = ((self.state.get("plugins") or {}).get(plugin_name) or {}).get("config")
        if isinstance(stored, dict):
            merged = dict(defaults)
            merged.update(stored)
            return merged
        return defaults

    def set_plugin_config(self, plugin_name: str, config: dict[str, Any]) -> str:
        if plugin_name not in self.loaded_plugins:
            return f"Plugin '{plugin_name}' is not installed."
        cfg = self.state["plugins"].setdefault(
            plugin_name,
            {"enabled_globally": False, "allowed_users": [], "denied_users": [], "config": {}},
        )
        if not isinstance(config, dict):
            return "Error: config must be a JSON object."
        cfg["config"] = dict(config)
        self._save_state()
        return f"Updated configuration for plugin '{plugin_name}'."

    def granted_permissions(self, plugin_name: str) -> list[str]:
        data = self.loaded_plugins.get(plugin_name) or {}
        manifest: PluginManifest | None = data.get("typed_manifest")
        if manifest is None:
            return []
        return list(manifest.permissions or manifest.required_capabilities)

    # ------------------------------------------------------------------ #
    # registration
    # ------------------------------------------------------------------ #

    def _register_listener(
        self, plugin_name: str, event: str, callback: Callable[..., Any]
    ) -> None:
        self._listeners.setdefault(event, []).append((plugin_name, callback))
        logger.info("Plugin %r subscribed to %s", plugin_name, event)

    def _register_job(
        self,
        plugin_name: str,
        interval: float,
        callback: Callable[[], Any],
        *,
        run_immediately: bool,
    ) -> None:
        self._job_specs.setdefault(plugin_name, []).append(
            {
                "interval": interval,
                "callback": callback,
                "run_immediately": run_immediately,
            }
        )
        logger.info(
            "Plugin %r scheduled %s every %.0fs",
            plugin_name,
            getattr(callback, "__name__", "job"),
            interval,
        )

    def _register_extension(
        self, plugin: str, kind: str, key: str, payload: Any
    ) -> None:
        bucket = self._extensions.setdefault(plugin, {})
        bucket[f"{kind}:{key}"] = {"kind": kind, "key": key, "payload": payload}

    def wrap_tool(self, plugin: str, name: str, wrapper: Callable[..., Any]) -> Any:
        """Replace tool.execute; restored on plugin teardown."""
        tool = self.get_tool(name) or (getattr(self.bot, "tools", None) or {}).get(name)
        if tool is None:
            raise KeyError(f"tool {name!r} is not registered")
        original = getattr(tool, "execute", None)
        if not callable(original):
            raise TypeError(f"tool {name!r} has no execute()")
        replacement = wrapper(original)
        if not callable(replacement):
            raise TypeError("wrap_tool factory must return a callable execute")
        tool.execute = replacement
        self._tool_wraps.setdefault(str(plugin), []).append((tool, original))
        return tool

    def _unwrap_tools(self, plugin: str) -> None:
        for tool, original in self._tool_wraps.pop(str(plugin), []):
            with contextlib.suppress(Exception):
                tool.execute = original

    def register_context_tool(
        self, plugin_name: str, tool: Any, *, name: str | None = None
    ) -> None:
        tool_name = (
            name
            or getattr(tool, "tool_name", None)
            or getattr(tool, "name", None)
            or ""
        )
        if not tool_name and hasattr(tool, "get_name") and callable(tool.get_name):
            tool_name = tool.get_name()
        if not tool_name:
            tool_name = getattr(tool, "__name__", type(tool).__name__)
        self._publish_tool(plugin_name, tool_name, tool)

    def _publish_tool(self, plugin_name: str, tool_name: str, tool: Any) -> None:
        if not isinstance(tool_name, str) or not tool_name.strip():
            raise TypeError(
                f"plugin {plugin_name!r} tool name must be a non-empty string"
            )
        tool_name = tool_name.strip()
        if tool_name in self.all_plugin_tools:
            owner, _existing = self.all_plugin_tools[tool_name]
            if owner != plugin_name:
                raise ValueError(
                    f"tool {tool_name!r} is already registered by plugin "
                    f"{owner!r}; {plugin_name!r} cannot shadow it"
                )
        data = self.loaded_plugins.setdefault(
            plugin_name,
            {"manifest": {}, "module": None, "tools": {}, "context": None},
        )
        data.setdefault("tools", {})[tool_name] = tool
        self.all_plugin_tools[tool_name] = (plugin_name, tool)
        manifest: PluginManifest | None = data.get("typed_manifest")
        protected = bool(manifest.protected) if manifest is not None else False
        self.tool_registry.register_tool(
            tool, name=tool_name, plugin=plugin_name, protected=protected
        )

    def _track_view(self, plugin: str, view: Any) -> None:
        self._maxwell_plugin_views.setdefault(plugin, set()).add(view)

    def _track_dynamic_items(self, plugin: str, items: tuple[Any, ...]) -> None:
        self._maxwell_dynamic_items.setdefault(plugin, set()).update(items)

    def _stats(self, plugin: str) -> dict[str, Any]:
        return self._maxwell_runtime_stats.setdefault(
            str(plugin),
            {
                "event_calls": 0,
                "event_errors": 0,
                "event_timeouts": 0,
                "job_calls": 0,
                "job_errors": 0,
                "job_timeouts": 0,
                "background_started": 0,
                "background_errors": 0,
                "last_error": "",
                "last_event": "",
                "last_ok_at": 0.0,
                "last_error_at": 0.0,
            },
        )

    def runtime_health(self, plugin_name: str | None = None) -> dict[str, Any]:
        table = self._maxwell_runtime_stats
        if plugin_name is not None:
            return dict(table.get(str(plugin_name)) or {})
        return {
            str(name): dict(row)
            for name, row in table.items()
            if isinstance(row, dict)
        }

    def spawn_task(
        self, plugin: str, awaitable: Any, *, name: str | None = None
    ) -> asyncio.Task:
        if not hasattr(awaitable, "__await__"):
            raise TypeError("ctx.spawn() expects a coroutine/awaitable")
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            close = getattr(awaitable, "close", None)
            if callable(close):
                close()
            raise RuntimeError(
                "ctx.spawn() needs a running event loop; call it from an event/job callback"
            ) from exc
        group = {t for t in self._maxwell_managed_tasks.get(plugin, set()) if not t.done()}
        self._maxwell_managed_tasks[plugin] = group
        if len(group) >= _MAX_TRACKED_TASKS_PER_PLUGIN:
            close = getattr(awaitable, "close", None)
            if callable(close):
                close()
            raise RuntimeError(
                f"plugin {plugin!r} already has {_MAX_TRACKED_TASKS_PER_PLUGIN} managed background tasks"
            )
        task = loop.create_task(awaitable, name=name or f"plugin-bg-{plugin}")
        group.add(task)
        row = self._stats(plugin)
        row["background_started"] += 1

        def done(t: asyncio.Task) -> None:
            group.discard(t)
            if t.cancelled():
                return
            try:
                exc = t.exception()
            except (asyncio.CancelledError, Exception) as error:
                exc = error
            if exc is not None:
                row["background_errors"] += 1
                row["last_error"] = f"{type(exc).__name__}: {exc}"[:1000]
                row["last_error_at"] = time.time()
                logger.error("plugin %s managed background task failed: %s", plugin, exc)

        task.add_done_callback(done)
        return task

    def spawn_after(
        self,
        plugin: str,
        seconds: float,
        callback: Any,
        *,
        name: str | None = None,
    ) -> asyncio.Task:
        if not callable(callback):
            raise TypeError("callback must be callable")
        delay = float(seconds)
        if delay < 0 or delay > 31_536_000:
            raise ValueError("seconds must be between 0 and 31536000")

        async def delayed() -> None:
            await asyncio.sleep(delay)
            result = callback()
            if hasattr(result, "__await__"):
                await result
            elif result is not None:
                raise TypeError("ctx.after callback must return an awaitable or None")

        return self.spawn_task(
            plugin, delayed(), name=name or f"plugin-after-{plugin}"
        )

    def _cancel_managed(self) -> list[asyncio.Task]:
        tasks = [
            task
            for values in self._maxwell_managed_tasks.values()
            for task in values
            if isinstance(task, asyncio.Task)
        ]
        self._maxwell_managed_tasks.clear()
        for task in tasks:
            if not task.done():
                task.cancel()
        return tasks

    def _cleanup_ui(self) -> None:
        for views in self._maxwell_plugin_views.values():
            for view in list(views):
                stop = getattr(view, "stop", None)
                if callable(stop):
                    with contextlib.suppress(Exception):
                        stop()
        self._maxwell_plugin_views.clear()
        remove = getattr(self.bot, "remove_dynamic_items", None)
        if callable(remove):
            for items in self._maxwell_dynamic_items.values():
                if items:
                    with contextlib.suppress(Exception):
                        remove(*tuple(items))
        self._maxwell_dynamic_items.clear()

    # ------------------------------------------------------------------ #
    # events + jobs
    # ------------------------------------------------------------------ #

    def subscribed_events(self) -> list[str]:
        return sorted(name for name, entries in self._listeners.items() if entries)

    async def dispatch_event(self, event: str, *args: Any, **kwargs: Any) -> int:
        entries = list(self._listeners.get(event) or [])
        if not entries:
            return 0
        actor = self._event_actor_id(args)
        filtered: list[tuple[str, Callable[..., Any]]] = []
        for plugin_name, callback in entries:
            if not self.is_plugin_enabled_for_user(plugin_name, actor):
                continue
            filtered.append((plugin_name, callback))
        if not filtered:
            return 0

        async def _run(plugin_name: str, callback: Callable[..., Any]) -> None:
            row = self._stats(plugin_name)
            row["event_calls"] += 1
            row["last_event"] = str(event)
            try:
                await asyncio.wait_for(
                    callback(*args, **kwargs), timeout=_EVENT_TIMEOUT
                )
                row["last_ok_at"] = time.time()
            except asyncio.TimeoutError as exc:
                row["event_timeouts"] += 1
                row["event_errors"] += 1
                row["last_error"] = f"{type(exc).__name__}: {exc}"[:1000]
                row["last_error_at"] = time.time()
                logger.warning(
                    "Plugin %r event %s timed out after %.1fs",
                    plugin_name,
                    event,
                    _EVENT_TIMEOUT,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                row["event_errors"] += 1
                row["last_error"] = f"{type(exc).__name__}: {exc}"[:1000]
                row["last_error_at"] = time.time()
                logger.exception("Plugin %r failed handling %s", plugin_name, event)

        await asyncio.gather(
            *(_run(name, cb) for name, cb in filtered), return_exceptions=True
        )
        return len(filtered)

    @staticmethod
    def _event_actor_id(args: tuple) -> str | None:
        for arg in args:
            author = getattr(arg, "author", None)
            uid = getattr(author, "id", None)
            if uid is not None:
                return str(uid)
            user = getattr(arg, "user", None)
            uid = getattr(user, "id", None)
            if uid is not None:
                return str(uid)
            if getattr(arg, "discriminator", None) is not None:
                uid = getattr(arg, "id", None)
                if uid is not None:
                    return str(uid)
        return None

    def start_jobs(self) -> int:
        if self._jobs_started:
            return 0
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("No running loop; plugin jobs not started")
            return 0
        started = 0
        for plugin_name, specs in self._job_specs.items():
            for spec in specs:
                task = loop.create_task(
                    self._run_job(plugin_name, spec),
                    name=f"plugin-job-{plugin_name}",
                )
                self._job_tasks.setdefault(plugin_name, []).append(task)
                started += 1
        self._jobs_started = True
        if started:
            logger.info("Started %d plugin job(s)", started)
        return started

    async def _run_job(self, plugin_name: str, spec: dict) -> None:
        interval = float(spec["interval"])
        callback = spec["callback"]
        if not spec.get("run_immediately"):
            await asyncio.sleep(interval)
        while True:
            started = time.monotonic()
            row = self._stats(plugin_name)
            try:
                if self.is_plugin_enabled_for_user(plugin_name, None):
                    row["job_calls"] += 1
                    await asyncio.wait_for(callback(), timeout=_JOB_TIMEOUT)
                    row["last_ok_at"] = time.time()
            except asyncio.TimeoutError as exc:
                row["job_timeouts"] += 1
                row["job_errors"] += 1
                row["last_error"] = f"{type(exc).__name__}: {exc}"[:1000]
                row["last_error_at"] = time.time()
                logger.warning(
                    "Plugin %r scheduled job timed out after %.1fs",
                    plugin_name,
                    _JOB_TIMEOUT,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                row["job_errors"] += 1
                row["last_error"] = f"{type(exc).__name__}: {exc}"[:1000]
                row["last_error_at"] = time.time()
                logger.exception("Plugin %r scheduled job failed", plugin_name)
            elapsed = time.monotonic() - started
            await asyncio.sleep(max(1.0, interval - elapsed))

    async def stop_jobs(self) -> None:
        tasks = [t for group in self._job_tasks.values() for t in group]
        self._job_tasks.clear()
        self._jobs_started = False
        for task in tasks:
            if not task.done():
                task.cancel()
        for task in tasks:
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await task

    async def teardown(self) -> None:
        await self.stop_jobs()
        managed = self._cancel_managed()
        for task in managed:
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await task
        self._cleanup_ui()
        for plugin_name, data in list(self.loaded_plugins.items()):
            hook = getattr((data or {}).get("module"), "teardown", None)
            if not callable(hook):
                continue
            try:
                result = hook(self.bot)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.exception("Plugin %r teardown failed", plugin_name)
            self._clear_plugin_registrations(plugin_name)
        self._listeners.clear()
        self._job_specs.clear()
        self._extensions.clear()

    def _clear_plugin_registrations(self, plugin_name: str) -> None:
        self._unwrap_tools(plugin_name)
        self._drop_registrations(plugin_name)
        self.hooks.unregister_plugin(plugin_name)
        self.prompts.unregister_plugin(plugin_name)
        self.services.unregister_owner(plugin_name)
        self.tool_registry.unregister_plugin(plugin_name)
        self._extensions.pop(plugin_name, None)

    # ------------------------------------------------------------------ #
    # loading
    # ------------------------------------------------------------------ #

    def _plugin_dirs(self) -> list[Path]:
        extra = [self.plugins_dir, *self.extra_plugin_dirs]
        if self.plugins_dir != self.root_dir / "plugins":
            # Tests pass an isolated plugins_dir; don't also scan the repo.
            if not self.extra_plugin_dirs:
                if self.plugins_dir.is_dir():
                    return sorted(
                        [
                            p
                            for p in self.plugins_dir.iterdir()
                            if p.is_dir()
                            and not p.name.startswith((".", "_"))
                            and p.name.isidentifier()
                        ],
                        key=lambda p: p.name,
                    )
                return []
        return iter_plugin_dirs(extra)

    def load_plugins(self) -> Dict[str, Any]:
        for plugin_name in list(self._tool_wraps):
            self._unwrap_tools(plugin_name)
        self.loaded_plugins.clear()
        self.all_plugin_tools.clear()
        self.load_errors.clear()
        self._listeners.clear()
        self._job_specs.clear()
        self._extensions.clear()
        self.hooks = HookBus()
        self.prompts = PromptManager()
        self.services = ServiceContainer()
        self.tool_registry = ToolRegistry()
        set_global_registry(self.tool_registry)

        root_str = str(self.root_dir)
        if root_str not in sys.path:
            sys.path.insert(0, root_str)

        discovered: dict[str, tuple[Path, PluginManifest]] = {}
        for entry in self._plugin_dirs():
            plugin_name = entry.name
            try:
                manifest = self._load_typed_manifest(entry, plugin_name)
            except ManifestError as exc:
                self.load_errors[plugin_name] = str(exc)
                logger.error("Invalid plugin %r: %s", plugin_name, exc)
                continue
            if plugin_name in discovered:
                self.load_errors[plugin_name] = (
                    f"duplicate plugin id {plugin_name!r} "
                    f"(already loaded from {discovered[plugin_name][0]})"
                )
                logger.error(self.load_errors[plugin_name])
                continue
            discovered[plugin_name] = (entry, manifest)

        order = self._resolve_load_order(discovered)
        for plugin_name in order:
            entry, _manifest = discovered[plugin_name]
            try:
                self._load_one(entry, plugin_name)
            except Exception as e:
                for loaded_name in list(sys.modules):
                    if loaded_name == f"plugins.{plugin_name}" or (
                        loaded_name.startswith(f"plugins.{plugin_name}.")
                    ):
                        sys.modules.pop(loaded_name, None)
                self._clear_plugin_registrations(plugin_name)
                self.loaded_plugins.pop(plugin_name, None)
                self.load_errors[plugin_name] = str(e)
                logger.exception(f"Error loading plugin '{plugin_name}': {e}")

        self._publish_result_tools()
        self.sync_bot_tools()
        return self.loaded_plugins

    def _resolve_load_order(
        self, discovered: dict[str, tuple[Path, PluginManifest]]
    ) -> list[str]:
        graph: dict[str, list[str]] = {}
        for name, (_path, manifest) in discovered.items():
            missing = [dep for dep in manifest.dependencies if dep not in discovered]
            if missing:
                self.load_errors[name] = (
                    f"missing required plugin dependencies: {', '.join(missing)}"
                )
                logger.error("Skipping plugin %r: %s", name, self.load_errors[name])
                continue
            graph[name] = list(manifest.dependencies)
        try:
            return _topo_sort(graph)
        except ValueError as exc:
            logger.error("%s", exc)
            # Fall back to protected-first alphabetical so a cycle cannot
            # take down the whole host.
            protected = sorted(
                n
                for n, (_p, m) in discovered.items()
                if n in graph and m.protected
            )
            rest = sorted(n for n in graph if n not in protected)
            return protected + rest

    def _drop_registrations(self, plugin_name: str) -> None:
        for event in list(self._listeners):
            kept = [
                (name, cb) for name, cb in self._listeners[event] if name != plugin_name
            ]
            if kept:
                self._listeners[event] = kept
            else:
                self._listeners.pop(event, None)
        self._job_specs.pop(plugin_name, None)
        for tname, (owner, _tool) in list(self.all_plugin_tools.items()):
            if owner == plugin_name:
                self.all_plugin_tools.pop(tname, None)

    def _load_one(self, entry: Path, plugin_name: str) -> None:
        manifest = self._load_typed_manifest(entry, plugin_name)
        module_name = f"plugins.{plugin_name}"
        init_py = entry / "__init__.py"
        tools_py = entry / "tools.py"
        target_file = (
            init_py if init_py.exists() else (tools_py if tools_py.exists() else None)
        )
        if not target_file:
            logger.warning(
                f"No __init__.py or tools.py found in plugin '{plugin_name}'"
            )
            return

        for loaded_name in list(sys.modules):
            if loaded_name == module_name or loaded_name.startswith(module_name + "."):
                sys.modules.pop(loaded_name, None)
        spec = importlib.util.spec_from_file_location(
            module_name,
            str(target_file),
            submodule_search_locations=[str(entry)] if target_file == init_py else None,
        )
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = mod
            spec.loader.exec_module(mod)
        elif module_name in sys.modules:
            mod = reload(sys.modules[module_name])
        else:
            mod = import_module(module_name)

        ctx = PluginContext(self, plugin_name)
        self.loaded_plugins[plugin_name] = {
            "manifest": manifest.raw
            or {
                "name": plugin_name,
                "description": manifest.description,
                "version": manifest.version,
                "enabled_globally": manifest.enabled_by_default,
            },
            "typed_manifest": manifest,
            "module": mod,
            "tools": {},
            "context": ctx,
            "path": str(entry),
        }
        tools_list = self._call_setup(mod, ctx)

        tool_dict = self.loaded_plugins[plugin_name]["tools"]
        for t in tools_list:
            name = getattr(t, "tool_name", None) or getattr(t, "name", None)
            if not name and hasattr(t, "get_name") and callable(t.get_name):
                name = t.get_name()
            if not name:
                name = getattr(t, "__name__", None) or type(t).__name__
            self._publish_tool(plugin_name, name, t)
            tool_dict[name] = t

        if plugin_name not in self.state["plugins"]:
            self.state["plugins"][plugin_name] = {
                "enabled_globally": self._as_bool(
                    manifest.enabled_by_default, False
                ),
                "allowed_users": self._user_ids(manifest.allowed_users),
                "denied_users": self._user_ids(manifest.denied_users),
                "config": dict(manifest.default_config),
            }
            self._save_state()

        prompt_spec = (manifest.raw or {}).get("prompt")
        if isinstance(prompt_spec, dict) and str(prompt_spec.get("text") or "").strip():
            from maxwell_core.prompts.component import PromptComponent

            requires = prompt_spec.get("requires_tools")
            if not isinstance(requires, list):
                requires = manifest.tool_names()
            try:
                self.prompts.register(
                    PromptComponent(
                        id=str(prompt_spec.get("id") or f"{plugin_name}.prompt"),
                        plugin=plugin_name,
                        text=str(prompt_spec.get("text") or ""),
                        scope=str(prompt_spec.get("scope") or "always"),
                        position=str(prompt_spec.get("position") or "tools"),
                        priority=int(prompt_spec.get("priority") or 80),
                        requires_tools=tuple(str(x) for x in requires),
                    )
                )
            except ValueError as exc:
                logger.warning("plugin %s prompt rejected: %s", plugin_name, exc)

        events = self.plugin_events(plugin_name)
        jobs = len(self._job_specs.get(plugin_name) or [])
        logger.info(
            "Loaded plugin '%s' with %d tool(s)%s%s",
            plugin_name,
            len(tool_dict),
            f", events: {', '.join(events)}" if events else "",
            f", {jobs} job(s)" if jobs else "",
        )

    def plugin_events(self, plugin_name: str) -> list[str]:
        return sorted(
            event
            for event, entries in self._listeners.items()
            if any(name == plugin_name for name, _cb in entries)
        )

    def plugin_extensions(self, plugin_name: str) -> dict[str, list[str]]:
        grouped: dict[str, list[str]] = {}
        for item in (self._extensions.get(plugin_name) or {}).values():
            grouped.setdefault(item["kind"], []).append(item["key"])
        grouped["tools"] = list((self.loaded_plugins.get(plugin_name) or {}).get("tools") or {})
        grouped["events"] = self.plugin_events(plugin_name)
        grouped["jobs"] = [
            getattr(spec.get("callback"), "__name__", "job")
            for spec in self._job_specs.get(plugin_name) or []
        ]
        grouped["hooks"] = self.hooks.plugin_hooks(plugin_name)
        grouped["prompts"] = self.prompts.plugin_components(plugin_name)
        grouped["services"] = self.services.owned_by(plugin_name)
        return grouped

    @staticmethod
    def _call_setup(mod: Any, ctx: PluginContext) -> list:
        for attr in ("setup", "get_tools"):
            hook = getattr(mod, attr, None)
            if not callable(hook):
                continue
            try:
                signature = inspect.signature(hook)
            except (TypeError, ValueError):
                res = hook(ctx.bot)
            else:
                try:
                    signature.bind(ctx.bot, ctx)
                except TypeError:
                    try:
                        signature.bind(ctx.bot, ctx=ctx)
                    except TypeError:
                        res = hook(ctx.bot)
                    else:
                        res = hook(ctx.bot, ctx=ctx)
                else:
                    res = hook(ctx.bot, ctx)
            if inspect.isawaitable(res):
                try:
                    asyncio.get_running_loop()
                except RuntimeError:
                    res = asyncio.run(res)
                else:
                    ctx._manager._pending_async_setup.append(res)
                    return []
            return res if isinstance(res, list) else []
        return []

    def _publish_result_tools(self) -> None:
        names = set()
        for data in self.loaded_plugins.values():
            for tool_name, tool in (data.get("tools") or {}).items():
                if getattr(tool, "returns_result", False):
                    names.add(tool_name)
        names.update(self.tool_registry.result_names())
        try:
            import tool_schemas

            tool_schemas.set_plugin_result_tools(names)
            for spec in self.tool_registry.specs():
                schema = spec.schema()
                if spec.name not in tool_schemas.TOOL_PARAMETERS:
                    tool_schemas.TOOL_PARAMETERS[spec.name] = schema
        except Exception as exc:  # pragma: no cover - import-order safety
            logger.debug("Could not publish plugin result contracts: %s", exc)

    def sync_bot_tools(self) -> None:
        """Copy globally-enabled plugin tools onto bot.tools for dispatch."""
        bot_tools = getattr(self.bot, "tools", None)
        if not isinstance(bot_tools, dict):
            return
        published = {
            name
            for name, (plugin, _tool) in self.all_plugin_tools.items()
            if self.is_plugin_enabled_for_user(plugin, None)
        }
        for name in list(bot_tools):
            if name in self.all_plugin_tools and name not in published:
                bot_tools.pop(name, None)
        for name, (_plugin, tool) in self.all_plugin_tools.items():
            if name in published:
                bot_tools[name] = tool

    def reload_plugins(self) -> str:
        for tasks in list(self._job_tasks.values()):
            for task in tasks:
                if not task.done():
                    task.cancel()
        self._job_tasks.clear()
        self._jobs_started = False
        self._cancel_managed()
        self._cleanup_ui()
        try:
            loaded = self.load_plugins()
        except Exception as exc:
            logger.exception("Failed to reload plugins")
            return f"Error reloading plugins: {exc}"
        try:
            self.start_jobs()
        except Exception:
            pass
        tool_count = sum(
            len(data.get("tools") or {})
            for data in loaded.values()
            if isinstance(data, dict)
        )
        jobs = sum(len(specs) for specs in self._job_specs.values())
        events = len(self.subscribed_events())
        extra = f" {jobs} job(s), {events} event hook(s)." if (jobs or events) else ""
        errors = len(self.load_errors)
        err = f" {errors} failed." if errors else ""
        return f"Reloaded {len(loaded)} plugin(s) with {tool_count} tool(s).{extra}{err}"

    def _load_typed_manifest(self, plugin_dir: Path, plugin_name: str) -> PluginManifest:
        manifest_file = plugin_dir / "plugin.json"
        if manifest_file.exists():
            return load_manifest_file(manifest_file, directory_name=plugin_name)
        return validate_manifest(
            {
                "name": plugin_name,
                "description": f"Maxwell plugin: {plugin_name}",
                "version": "1.0.0",
                "enabled_globally": False,
                "allowed_users": [],
                "denied_users": [],
            },
            directory_name=plugin_name,
            path=manifest_file,
        )

    def _load_manifest(self, plugin_dir: Path, plugin_name: str) -> Dict[str, Any]:
        try:
            return self._load_typed_manifest(plugin_dir, plugin_name).raw or {
                "name": plugin_name,
                "description": f"Maxwell plugin: {plugin_name}",
                "version": "1.0.0",
                "enabled_globally": False,
                "allowed_users": [],
                "denied_users": [],
            }
        except ManifestError as exc:
            logger.error("Invalid plugin.json in %s: %s", plugin_dir, exc)
            return {
                "name": plugin_name,
                "description": f"Maxwell plugin: {plugin_name}",
                "version": "1.0.0",
                "enabled_globally": False,
                "allowed_users": [],
                "denied_users": [],
            }

    # ------------------------------------------------------------------ #
    # scoping
    # ------------------------------------------------------------------ #

    def is_plugin_enabled_for_user(
        self, plugin_name: str, user_id: str | int | None
    ) -> bool:
        cfg = self.state["plugins"].get(plugin_name)
        if not isinstance(cfg, dict):
            return False

        uid = str(user_id) if user_id is not None else ""
        denied = set(self._user_ids(cfg.get("denied_users", [])))
        if uid and uid in denied:
            return False

        if self._as_bool(cfg.get("enabled_globally"), False):
            return True

        allowed = set(self._user_ids(cfg.get("allowed_users", [])))
        if uid and uid in allowed:
            return True

        return False

    def get_available_tools(
        self, user_id: str | int | None = None, platform: str = "discord"
    ) -> Dict[str, Any]:
        return self.get_available_tools_for_user(user_id=user_id, platform=platform)

    def get_available_tools_for_user(
        self, user_id: str | int | None, platform: str = "discord"
    ) -> Dict[str, Any]:
        tools = {}
        for plugin_name, data in self.loaded_plugins.items():
            if self.is_plugin_enabled_for_user(plugin_name, user_id):
                for tname, tobj in data["tools"].items():
                    spec = self.tool_registry.get(tname)
                    if spec is not None and not spec.available_on(platform):
                        continue
                    t_plat = getattr(tobj, "platform", "any")
                    if spec is None and t_plat not in ("any", platform):
                        continue
                    tools[tname] = tobj
        return tools

    def get_tool(self, tool_name: str) -> Optional[Any]:
        spec = self.tool_registry.get(tool_name)
        if spec is not None:
            return spec.tool
        entry = self.all_plugin_tools.get(tool_name)
        if entry:
            return entry[1]
        return None

    def is_protected(self, plugin_name: str) -> bool:
        data = self.loaded_plugins.get(plugin_name) or {}
        manifest = data.get("typed_manifest")
        if manifest is not None:
            return bool(manifest.protected)
        return False

    def enable_plugin(
        self,
        plugin_name: str,
        user_id: Optional[str | int] = None,
        is_global: bool = False,
    ) -> str:
        is_global = self._as_bool(is_global, False)
        if plugin_name not in self.loaded_plugins:
            return f"Plugin '{plugin_name}' is not installed."

        cfg = self.state["plugins"].setdefault(
            plugin_name,
            {
                "enabled_globally": False,
                "allowed_users": [],
                "denied_users": [],
                "config": {},
            },
        )
        if not isinstance(cfg, dict):
            cfg = {
                "enabled_globally": False,
                "allowed_users": [],
                "denied_users": [],
                "config": {},
            }
            self.state["plugins"][plugin_name] = cfg
        cfg["allowed_users"] = self._user_ids(cfg.get("allowed_users", []))
        cfg["denied_users"] = self._user_ids(cfg.get("denied_users", []))

        if is_global:
            cfg["enabled_globally"] = True
            self._save_state()
            self.sync_bot_tools()
            return f"Plugin '{plugin_name}' is now enabled GLOBALLY."

        if user_id is None:
            return "user_id is required when not enabling globally."

        uid = str(user_id)
        if (
            uid not in cfg.get("allowed_users", [])
            and len(cfg.get("allowed_users", [])) >= 500
        ):
            return "Error: plugin has reached its per-user enablement limit."
        if uid in cfg.get("denied_users", []):
            cfg["denied_users"].remove(uid)
        if uid not in cfg.setdefault("allowed_users", []):
            cfg["allowed_users"].append(uid)

        self._save_state()
        self.sync_bot_tools()
        return f"Plugin '{plugin_name}' is now enabled for user <@{uid}>."

    def disable_plugin(
        self,
        plugin_name: str,
        user_id: Optional[str | int] = None,
        is_global: bool = False,
    ) -> str:
        is_global = self._as_bool(is_global, False)
        if plugin_name not in self.loaded_plugins:
            return f"Plugin '{plugin_name}' is not installed."
        if is_global and self.is_protected(plugin_name):
            return (
                f"Plugin '{plugin_name}' is protected and cannot be disabled globally."
            )

        cfg = self.state["plugins"].setdefault(
            plugin_name,
            {
                "enabled_globally": False,
                "allowed_users": [],
                "denied_users": [],
                "config": {},
            },
        )
        if not isinstance(cfg, dict):
            cfg = {
                "enabled_globally": False,
                "allowed_users": [],
                "denied_users": [],
                "config": {},
            }
            self.state["plugins"][plugin_name] = cfg
        cfg["allowed_users"] = self._user_ids(cfg.get("allowed_users", []))
        cfg["denied_users"] = self._user_ids(cfg.get("denied_users", []))

        if is_global:
            cfg["enabled_globally"] = False
            self._save_state()
            self.sync_bot_tools()
            return f"Plugin '{plugin_name}' is now disabled GLOBALLY."

        if user_id is None:
            return "user_id is required when not disabling globally."

        uid = str(user_id)
        if uid in cfg.setdefault("allowed_users", []):
            cfg["allowed_users"].remove(uid)
        if cfg.get("enabled_globally", False):
            if uid not in cfg.setdefault("denied_users", []):
                if len(cfg["denied_users"]) >= 500:
                    return "Error: plugin has reached its per-user denial limit."
                cfg["denied_users"].append(uid)

        self._save_state()
        self.sync_bot_tools()
        return f"Plugin '{plugin_name}' is now disabled for user <@{uid}>."

    def list_plugins(self, user_id: Optional[str | int] = None) -> List[Dict[str, Any]]:
        result = []
        uid = str(user_id) if user_id is not None else ""
        for name, data in self.loaded_plugins.items():
            manifest = data.get("typed_manifest")
            raw = data["manifest"]
            cfg = self.state["plugins"].get(name, {})
            globally_enabled = self._as_bool(cfg.get("enabled_globally"), False)
            user_enabled = (
                self.is_plugin_enabled_for_user(name, uid) if uid else globally_enabled
            )
            extensions = self.plugin_extensions(name)
            result.append(
                {
                    "name": name,
                    "id": getattr(manifest, "id", name) if manifest else name,
                    "description": (
                        manifest.description if manifest else raw.get("description", "")
                    ),
                    "version": manifest.version if manifest else raw.get("version", "1.0.0"),
                    "author": getattr(manifest, "author", "") if manifest else "",
                    "tools": list(data["tools"].keys()),
                    "enabled_globally": globally_enabled,
                    "enabled_for_you": user_enabled,
                    "user_active": user_enabled,
                    "allowed_users_count": len(cfg.get("allowed_users", [])),
                    "events": self.plugin_events(name),
                    "jobs": len(self._job_specs.get(name) or []),
                    "hooks": extensions.get("hooks") or [],
                    "prompts": extensions.get("prompts") or [],
                    "protected": self.is_protected(name),
                    "requires_restart": bool(
                        getattr(manifest, "requires_restart", False)
                    )
                    if manifest
                    else False,
                    "permissions": list(getattr(manifest, "permissions", []) or [])
                    if manifest
                    else [],
                    "health": self.runtime_health(name),
                    "error": self.load_errors.get(name, ""),
                    "config": self.plugin_config(name),
                    "config_schema": dict(getattr(manifest, "config_schema", {}) or {})
                    if manifest
                    else {},
                    "bundled": bool(getattr(manifest, "bundled", False))
                    if manifest
                    else False,
                    "uninstallable": bool(getattr(manifest, "uninstallable", True))
                    if manifest
                    else True,
                }
            )
        for name, error in self.load_errors.items():
            if name in self.loaded_plugins:
                continue
            result.append(
                {
                    "name": name,
                    "id": name,
                    "description": "",
                    "version": "",
                    "author": "",
                    "tools": [],
                    "enabled_globally": False,
                    "enabled_for_you": False,
                    "user_active": False,
                    "allowed_users_count": 0,
                    "events": [],
                    "jobs": 0,
                    "hooks": [],
                    "prompts": [],
                    "protected": False,
                    "requires_restart": False,
                    "permissions": [],
                    "health": {},
                    "error": error,
                    "config": {},
                    "config_schema": {},
                    "bundled": False,
                    "uninstallable": True,
                }
            )
        return result

    def installed_plugins_dir(self) -> Path:
        path = self.data_dir / "installed_plugins"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def install_from_path(self, source: str | Path, *, replace: bool = False) -> str:
        """Copy a plugin directory into installed_plugins after validating it.

        Does not execute the new code until reload. Owner-only callers should
        reload afterwards. Bundled plugins cannot be overwritten this way.
        """
        src = Path(source).expanduser()
        try:
            src = src.resolve()
        except OSError as exc:
            return f"Error: cannot resolve {source}: {exc}"
        if src.is_file() and src.suffix.lower() == ".zip":
            extract = self.data_dir / "plugin_incoming" / src.stem
            if extract.exists():
                shutil.rmtree(extract)
            extract.mkdir(parents=True, exist_ok=True)
            shutil.unpack_archive(str(src), str(extract))
            nested = [p for p in extract.iterdir() if p.is_dir() and (p / "plugin.json").is_file()]
            src = nested[0] if len(nested) == 1 else extract
        if not src.is_dir() or not (src / "plugin.json").is_file():
            return "Error: plugin source must be a directory (or zip) containing plugin.json"
        try:
            manifest = load_manifest_file(src / "plugin.json", directory_name=src.name)
        except ManifestError as exc:
            return f"Error: invalid plugin.json: {exc}"
        if manifest.bundled or manifest.protected:
            return f"Error: cannot overwrite bundled/protected plugin {manifest.id!r}"
        dest = self.installed_plugins_dir() / manifest.id
        if dest.exists() and not replace:
            return (
                f"Error: plugin {manifest.id!r} is already installed. "
                "Pass replace=true to update it."
            )
        staging = dest.with_name(dest.name + ".incoming")
        if staging.exists():
            shutil.rmtree(staging)
        shutil.copytree(src, staging)
        backup = None
        if dest.exists():
            backup = dest.with_name(dest.name + ".bak")
            if backup.exists():
                shutil.rmtree(backup)
            dest.replace(backup)
        try:
            staging.replace(dest)
        except Exception as exc:
            if backup is not None and backup.exists() and not dest.exists():
                backup.replace(dest)
            return f"Error installing plugin: {exc}"
        if backup is not None and backup.exists():
            shutil.rmtree(backup, ignore_errors=True)
        if src.parent.name == "plugin_incoming":
            shutil.rmtree(src.parent, ignore_errors=True)
        return (
            f"Installed plugin '{manifest.id}' v{manifest.version} to {dest}. "
            "Reload plugins to activate it."
        )

    def uninstall_plugin(self, plugin_name: str) -> str:
        """Remove an independently installed plugin's code. Data is kept."""
        dest = self.installed_plugins_dir() / plugin_name
        if (
            plugin_name not in self.loaded_plugins
            and plugin_name not in self.load_errors
            and not dest.exists()
        ):
            return f"Plugin '{plugin_name}' is not installed."
        if self.is_protected(plugin_name):
            return f"Plugin '{plugin_name}' is protected and cannot be uninstalled."
        data = self.loaded_plugins.get(plugin_name) or {}
        manifest = data.get("typed_manifest")
        if manifest is not None and manifest.bundled:
            return (
                f"Plugin '{plugin_name}' is bundled. Disable it instead of uninstalling."
            )
        path = Path(str(data.get("path") or dest))
        installed_root = self.installed_plugins_dir().resolve()
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if installed_root not in resolved.parents and resolved != installed_root / plugin_name:
            return (
                f"Plugin '{plugin_name}' is not independently installed "
                f"(code lives at {path})."
            )
        if plugin_name in self.loaded_plugins:
            self.disable_plugin(plugin_name, is_global=True)
        trash = self.data_dir / "plugin_trash" / f"{plugin_name}-{int(time.time())}"
        trash.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(resolved), str(trash))
        except OSError as exc:
            return f"Error moving plugin code aside: {exc}"
        self._clear_plugin_registrations(plugin_name)
        self.loaded_plugins.pop(plugin_name, None)
        self.sync_bot_tools()
        return (
            f"Uninstalled plugin '{plugin_name}'. Code moved to {trash}. "
            f"Stored data in {self.data_dir / 'plugins' / plugin_name} was kept."
        )

