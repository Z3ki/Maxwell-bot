"""Plugin loading, scoping, and the API a plugin is handed at setup time.

Plugins live in ``plugins/<name>/`` and contain:
  - ``plugin.json``: manifest metadata and defaults
  - ``__init__.py`` / ``tools.py``: exports ``setup(bot)`` -> list[Tool]

Runtime state (global vs per-user enablement) persists in ``data/plugins.json``.

Beyond tools, a plugin can react to Discord events and run work on a schedule.
Before this existed the only way in was an explicit tool call from the model, so
"accept friend requests as they arrive" was not expressible as a plugin: the
model had to happen to call something. A plugin now declares what it wants to
listen to and the host owns the wiring, which also means the host can take it
all back down on reload — a plugin that registered its own listener or task had
no way to unregister it, so every reload left the previous copy running.
"""

import asyncio
import contextlib
import inspect
import json
import logging
import os
import sys
import time
from importlib import import_module, reload
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("maxwell.plugins")

# Events a plugin may subscribe to. An allowlist rather than "anything goes":
# a typo like "on_mesage" would otherwise register a listener that silently
# never fires, which is a very quiet way to lose a whole feature.
ALLOWED_EVENTS: frozenset[str] = frozenset(
    {
        "on_message",
        "on_message_edit",
        "on_message_delete",
        "on_reaction_add",
        "on_reaction_remove",
        "on_member_join",
        "on_member_remove",
        "on_member_update",
        "on_guild_join",
        "on_guild_remove",
        "on_typing",
        "on_relationship_add",
        "on_relationship_update",
        "on_relationship_remove",
        "on_voice_state_update",
        "on_presence_update",
        "on_ready",
    }
)

# Floor on scheduled-job frequency. A plugin asking for every 0.01s would peg
# the loop and starve the reply path; a job is not the place for a busy-wait.
MIN_JOB_INTERVAL_SECONDS = 5.0


class PluginContext:
    """What a plugin gets to work with, and the only supported way in.

    Handed to ``setup(bot, ctx)`` when the plugin accepts a second argument.
    Plugins written against the old one-argument ``setup(bot)`` keep working —
    they just do not get events, jobs, or a scoped data directory.

    Everything registered here is tracked by the manager, so ``reload_plugins``
    can tear a plugin down completely instead of leaving orphaned listeners and
    background tasks behind.
    """

    def __init__(self, manager: "PluginManager", plugin_name: str) -> None:
        self._manager = manager
        self.name = plugin_name
        self.bot = manager.bot
        self.log = logging.getLogger(f"maxwell.plugins.{plugin_name}")

    # -- storage ----------------------------------------------------------- #
    @property
    def data_dir(self) -> Path:
        """A directory this plugin owns, for its own state files."""
        return self._manager.get_plugin_data_dir(self.name)

    def store_path(self, filename: str) -> Path:
        """A path inside this plugin's data dir. Traversal is refused."""
        safe = os.path.basename(str(filename or "").strip())
        if not safe or safe in {".", ".."}:
            raise ValueError("filename is required and must not traverse")
        return self.data_dir / safe

    # -- events ------------------------------------------------------------ #
    def on_event(self, event: str, callback: Callable[..., Any]) -> None:
        """Run ``callback`` whenever ``event`` fires.

        The callback must be a coroutine function: a sync callback would run
        inline on the event loop, and a slow one would stall every room at
        once. Exceptions are caught per invocation, so a broken plugin cannot
        take down the handler it is attached to.
        """
        if event not in ALLOWED_EVENTS:
            raise ValueError(
                f"{event!r} is not a subscribable event. "
                f"Known: {', '.join(sorted(ALLOWED_EVENTS))}"
            )
        if not callable(callback):
            raise TypeError("callback must be callable")
        if not inspect.iscoroutinefunction(callback):
            raise TypeError(
                f"{event} callback must be an async def — a blocking callback "
                "would stall every channel while it runs"
            )
        self._manager._register_listener(self.name, event, callback)

    def every(
        self,
        seconds: float,
        callback: Callable[[], Any],
        *,
        run_immediately: bool = False,
    ) -> None:
        """Run ``callback`` on a repeating schedule.

        Started once the bot's event loop is running (see ``start_jobs``), so a
        plugin whose ``setup`` runs during construction does not have to care
        that there is no loop yet — the alternative was calling
        ``asyncio.create_task`` in ``setup``, where it fails or is garbage
        collected mid-flight.
        """
        if not callable(callback):
            raise TypeError("callback must be callable")
        if not inspect.iscoroutinefunction(callback):
            raise TypeError("scheduled callback must be an async def")
        try:
            interval = float(seconds)
        except (TypeError, ValueError):
            raise ValueError("seconds must be a number") from None
        if interval < MIN_JOB_INTERVAL_SECONDS:
            raise ValueError(
                f"interval must be at least {MIN_JOB_INTERVAL_SECONDS}s "
                "(a tighter loop starves the reply path)"
            )
        self._manager._register_job(
            self.name, interval, callback, run_immediately=bool(run_immediately)
        )

    # -- reaching the rest of the bot -------------------------------------- #
    def tool(self, name: str) -> Any:
        """A built-in tool instance by name, or None."""
        return (getattr(self.bot, "tools", None) or {}).get(str(name))

    def is_admin(self, user_id: Any) -> bool:
        """Whether this user is a Maxwell admin. Fails closed."""
        checker = getattr(self.bot, "_is_admin", None)
        if not callable(checker):
            return False
        try:
            return bool(checker(user_id))
        except Exception:
            return False


class PluginManager:
    """Manages dynamic modular plugins for Maxwell."""

    def __init__(
        self,
        bot,
        plugins_dir: Optional[str] = None,
        data_dir: Optional[str] = None,
        state_file: Optional[str] = None,
    ):
        self.bot = bot
        self.root_dir = Path(os.path.abspath(os.path.dirname(__file__)))
        self.plugins_dir = (
            Path(plugins_dir) if plugins_dir else (self.root_dir / "plugins")
        )
        self.data_dir = Path(data_dir) if data_dir else (self.root_dir / "data")
        self.state_file = (
            Path(state_file) if state_file else (self.data_dir / "plugins.json")
        )

        self.plugins_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # Loaded plugin metadata & tool objects:
        # {plugin_name: {"manifest": dict, "module": module, "tools": dict}}
        self.loaded_plugins: Dict[str, Dict[str, Any]] = {}
        # Global registry of all plugin tool instances: {tool_name: (plugin, obj)}
        self.all_plugin_tools: Dict[str, tuple[str, Any]] = {}

        # event name -> [(plugin_name, callback)]
        self._listeners: Dict[str, List[tuple[str, Callable[..., Any]]]] = {}
        # plugin_name -> job specs registered during setup
        self._job_specs: Dict[str, List[dict]] = {}
        # plugin_name -> running job tasks
        self._job_tasks: Dict[str, List[asyncio.Task]] = {}
        self._jobs_started = False

        # Config / enabled status state:
        # {"plugins": {<name>: {"enabled_globally": bool,
        #                       "allowed_users": [...], "denied_users": [...]}}}
        self.state: Dict[str, Any] = {"plugins": {}}
        self._load_state()

    # ------------------------------------------------------------------ #
    # state
    # ------------------------------------------------------------------ #

    def _load_state(self) -> None:
        """Load state from data/plugins.json."""
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
        """Persist state to data/plugins.json atomically."""
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            temp_file = self.state_file.with_suffix(".tmp")
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(self.state, f, indent=2)
            temp_file.replace(self.state_file)
        except Exception as e:
            logger.error(f"Failed to save plugins.json: {e}")

    def get_plugin_data_dir(self, plugin_name: str) -> Path:
        """Isolated storage for a plugin under data/plugins/<plugin_name>/."""
        safe = os.path.basename(str(plugin_name or "").strip())
        if not safe or not safe.isidentifier():
            raise ValueError(f"invalid plugin name: {plugin_name!r}")
        pdir = self.data_dir / "plugins" / safe
        pdir.mkdir(parents=True, exist_ok=True)
        return pdir

    # ------------------------------------------------------------------ #
    # registration (called from PluginContext during setup)
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

    # ------------------------------------------------------------------ #
    # events + jobs
    # ------------------------------------------------------------------ #

    def subscribed_events(self) -> list[str]:
        """Events at least one loaded plugin listens to."""
        return sorted(name for name, entries in self._listeners.items() if entries)

    async def dispatch_event(self, event: str, *args: Any, **kwargs: Any) -> int:
        """Deliver an event to every subscribed plugin. Returns how many ran.

        Callbacks are gathered rather than awaited in sequence, so one slow
        plugin does not delay the others, and every exception is logged against
        its own plugin instead of propagating into the host handler.
        """
        entries = list(self._listeners.get(event) or [])
        if not entries:
            return 0
        # Only deliver to plugins the acting user is allowed to use. A disabled
        # plugin must stop reacting, not keep running because it was enabled
        # when the bot started.
        actor = self._event_actor_id(args)
        filtered: list[tuple[str, Callable[..., Any]]] = []
        for plugin_name, callback in entries:
            if actor is not None and not self.is_plugin_enabled_for_user(
                plugin_name, actor
            ):
                continue
            filtered.append((plugin_name, callback))
        if not filtered:
            return 0

        async def _run(plugin_name: str, callback: Callable[..., Any]) -> None:
            try:
                await callback(*args, **kwargs)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Plugin %r failed handling %s", plugin_name, event)

        await asyncio.gather(
            *(_run(name, cb) for name, cb in filtered), return_exceptions=True
        )
        return len(filtered)

    @staticmethod
    def _event_actor_id(args: tuple) -> str | None:
        """The user an event is about, when it has one.

        Used to stop a disabled plugin from continuing to react. Events with no
        discernible user (on_ready, on_guild_join) deliver unconditionally.
        """
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
        """Start every registered scheduled job. Returns how many started.

        Called once the loop is running. ``load_plugins`` clears the task list,
        so a reload cannot end up with two copies of the same job.
        """
        started = 0
        for plugin_name, specs in self._job_specs.items():
            for spec in specs:
                try:
                    task = asyncio.create_task(
                        self._run_job(plugin_name, spec),
                        name=f"plugin-job-{plugin_name}",
                    )
                except RuntimeError:
                    logger.warning(
                        "No running loop; plugin %r jobs not started", plugin_name
                    )
                    return started
                self._job_tasks.setdefault(plugin_name, []).append(task)
                started += 1
        self._jobs_started = True
        if started:
            logger.info("Started %d plugin job(s)", started)
        return started

    async def _run_job(self, plugin_name: str, spec: dict) -> None:
        """One scheduled job: sleep, run, log, repeat.

        A raising job is logged and retried on the next tick rather than
        killing the schedule — one failure should not stop it forever.
        """
        interval = float(spec["interval"])
        callback = spec["callback"]
        if not spec.get("run_immediately"):
            await asyncio.sleep(interval)
        while True:
            started = time.monotonic()
            try:
                await callback()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Plugin %r scheduled job failed", plugin_name)
            elapsed = time.monotonic() - started
            await asyncio.sleep(max(1.0, interval - elapsed))

    async def stop_jobs(self) -> None:
        """Cancel every running job and wait for it to unwind."""
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
        """Take every loaded plugin fully back down.

        Reload used to clear only the tool registry, so a plugin's listeners
        and background tasks survived and the new copy ran alongside the old.
        """
        await self.stop_jobs()
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
        self._listeners.clear()
        self._job_specs.clear()

    # ------------------------------------------------------------------ #
    # loading
    # ------------------------------------------------------------------ #

    def load_plugins(self) -> Dict[str, Any]:
        """Scan the plugins directory, import each plugin, register its tools."""
        self.loaded_plugins.clear()
        self.all_plugin_tools.clear()
        # Listeners and job specs belong to the previous generation of plugin
        # objects; keeping them would run stale code against a live bot.
        self._listeners.clear()
        self._job_specs.clear()

        root_str = str(self.root_dir)
        if root_str not in sys.path:
            sys.path.insert(0, root_str)

        if not self.plugins_dir.exists():
            return {}

        for entry in self.plugins_dir.iterdir():
            if not entry.is_dir() or entry.name.startswith((".", "_")):
                continue
            plugin_name = entry.name
            if not plugin_name.isidentifier():
                logger.warning(
                    "Skipping plugin %r: directory name is not a valid Python "
                    "identifier",
                    plugin_name,
                )
                continue
            try:
                self._load_one(entry, plugin_name)
            except Exception as e:
                for loaded_name in list(sys.modules):
                    if loaded_name == f"plugins.{plugin_name}" or (
                        loaded_name.startswith(f"plugins.{plugin_name}.")
                    ):
                        sys.modules.pop(loaded_name, None)
                # A plugin that failed halfway may already have registered
                # listeners or jobs; drop them so nothing half-loaded runs.
                self._drop_registrations(plugin_name)
                logger.exception(f"Error loading plugin '{plugin_name}': {e}")

        self._publish_result_tools()
        return self.loaded_plugins

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

    def _load_one(self, entry: Path, plugin_name: str) -> None:
        manifest = self._load_manifest(entry, plugin_name)
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

        import importlib.util

        # Remove the package and its submodules before every reload.
        # Re-executing only __init__.py otherwise leaves an old
        # plugins.<name>.tools module in sys.modules, so edited tool
        # code never reaches the live registry.
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
        tools_list = self._call_setup(mod, ctx)

        tool_dict = {}
        for t in tools_list:
            name = getattr(t, "name", None)
            if not name and hasattr(t, "get_name") and callable(t.get_name):
                name = t.get_name()
            if not name:
                name = getattr(t, "__name__", str(t))
            tool_dict[name] = t
            self.all_plugin_tools[name] = (plugin_name, t)

        self.loaded_plugins[plugin_name] = {
            "manifest": manifest,
            "module": mod,
            "tools": tool_dict,
            "context": ctx,
        }

        # Initialize state entry if missing
        if plugin_name not in self.state["plugins"]:
            self.state["plugins"][plugin_name] = {
                "enabled_globally": self._as_bool(
                    manifest.get("enabled_globally"), False
                ),
                "allowed_users": self._user_ids(manifest.get("allowed_users", [])),
                "denied_users": self._user_ids(manifest.get("denied_users", [])),
            }
            self._save_state()

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
        """Events this one plugin subscribed to."""
        return sorted(
            event
            for event, entries in self._listeners.items()
            if any(name == plugin_name for name, _cb in entries)
        )

    @staticmethod
    def _call_setup(mod: Any, ctx: "PluginContext") -> list:
        """Call the plugin's entry point, passing ctx when it accepts one.

        The one-argument ``setup(bot)`` form predates the context and must keep
        loading — breaking existing plugins to add a feature they do not use
        would be a poor trade.
        """
        for attr in ("setup", "get_tools"):
            hook = getattr(mod, attr, None)
            if not callable(hook):
                continue
            try:
                params = list(inspect.signature(hook).parameters.values())
                wants_ctx = len(params) >= 2 or any(
                    p.kind is inspect.Parameter.VAR_KEYWORD for p in params
                )
            except (TypeError, ValueError):
                wants_ctx = False
            res = hook(ctx.bot, ctx) if wants_ctx else hook(ctx.bot)
            return res if isinstance(res, list) else []
        return []

    def _publish_result_tools(self) -> None:
        """Tell tool_schemas which plugin tools hand their output back.

        Without this every plugin tool got the "returns nothing" contract, so a
        plugin that looked something up could never have its result read: the
        dispatch loop ended the turn and the output was discarded.
        """
        names = set()
        for data in self.loaded_plugins.values():
            for tool_name, tool in (data.get("tools") or {}).items():
                if getattr(tool, "returns_result", False):
                    names.add(tool_name)
        try:
            import tool_schemas

            tool_schemas.set_plugin_result_tools(names)
        except Exception as exc:  # pragma: no cover - import-order safety
            logger.debug("Could not publish plugin result contracts: %s", exc)

    def reload_plugins(self) -> str:
        """Reload the plugin directory and report the result to the caller."""
        for tasks in list(self._job_tasks.values()):
            for task in tasks:
                if not task.done():
                    task.cancel()
        self._job_tasks.clear()
        self._jobs_started = False
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
        return f"Reloaded {len(loaded)} plugin(s) with {tool_count} tool(s).{extra}"

    def _load_manifest(self, plugin_dir: Path, plugin_name: str) -> Dict[str, Any]:
        manifest_file = plugin_dir / "plugin.json"
        if manifest_file.exists():
            try:
                with open(manifest_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        return data
            except Exception as e:
                logger.error(f"Invalid plugin.json in {plugin_dir}: {e}")
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
        """Check whether a plugin is enabled for a given user or globally."""
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
        """Alias for get_available_tools_for_user."""
        return self.get_available_tools_for_user(user_id=user_id, platform=platform)

    def get_available_tools_for_user(
        self, user_id: str | int | None, platform: str = "discord"
    ) -> Dict[str, Any]:
        """Return dict of {tool_name: tool_obj} active for this user / turn."""
        tools = {}
        for plugin_name, data in self.loaded_plugins.items():
            if self.is_plugin_enabled_for_user(plugin_name, user_id):
                for tname, tobj in data["tools"].items():
                    t_plat = getattr(tobj, "platform", "any")
                    if t_plat in ("any", platform):
                        tools[tname] = tobj
        return tools

    def get_tool(self, tool_name: str) -> Optional[Any]:
        """Retrieve a loaded plugin tool instance by tool name."""
        entry = self.all_plugin_tools.get(tool_name)
        if entry:
            return entry[1]
        return None

    def enable_plugin(
        self,
        plugin_name: str,
        user_id: Optional[str | int] = None,
        is_global: bool = False,
    ) -> str:
        """Enable a plugin globally or for a specific user."""
        is_global = self._as_bool(is_global, False)
        if plugin_name not in self.loaded_plugins:
            return f"Plugin '{plugin_name}' is not installed."

        cfg = self.state["plugins"].setdefault(
            plugin_name,
            {"enabled_globally": False, "allowed_users": [], "denied_users": []},
        )
        if not isinstance(cfg, dict):
            cfg = {"enabled_globally": False, "allowed_users": [], "denied_users": []}
            self.state["plugins"][plugin_name] = cfg
        cfg["allowed_users"] = self._user_ids(cfg.get("allowed_users", []))
        cfg["denied_users"] = self._user_ids(cfg.get("denied_users", []))

        if is_global:
            cfg["enabled_globally"] = True
            self._save_state()
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
        return f"Plugin '{plugin_name}' is now enabled for user <@{uid}>."

    def disable_plugin(
        self,
        plugin_name: str,
        user_id: Optional[str | int] = None,
        is_global: bool = False,
    ) -> str:
        """Disable a plugin globally or for a specific user."""
        is_global = self._as_bool(is_global, False)
        if plugin_name not in self.loaded_plugins:
            return f"Plugin '{plugin_name}' is not installed."

        cfg = self.state["plugins"].setdefault(
            plugin_name,
            {"enabled_globally": False, "allowed_users": [], "denied_users": []},
        )
        if not isinstance(cfg, dict):
            cfg = {"enabled_globally": False, "allowed_users": [], "denied_users": []}
            self.state["plugins"][plugin_name] = cfg
        cfg["allowed_users"] = self._user_ids(cfg.get("allowed_users", []))
        cfg["denied_users"] = self._user_ids(cfg.get("denied_users", []))

        if is_global:
            cfg["enabled_globally"] = False
            self._save_state()
            return f"Plugin '{plugin_name}' is now disabled GLOBALLY."

        if user_id is None:
            return "user_id is required when not disabling globally."

        uid = str(user_id)
        if uid in cfg.setdefault("allowed_users", []):
            cfg["allowed_users"].remove(uid)
        if cfg.get("enabled_globally", False):
            # If enabled globally, add to denied list to explicitly opt out
            if uid not in cfg.setdefault("denied_users", []):
                if len(cfg["denied_users"]) >= 500:
                    return "Error: plugin has reached its per-user denial limit."
                cfg["denied_users"].append(uid)

        self._save_state()
        return f"Plugin '{plugin_name}' is now disabled for user <@{uid}>."

    def list_plugins(self, user_id: Optional[str | int] = None) -> List[Dict[str, Any]]:
        """Return list of plugins and their status relative to user_id."""
        result = []
        uid = str(user_id) if user_id is not None else ""
        for name, data in self.loaded_plugins.items():
            manifest = data["manifest"]
            cfg = self.state["plugins"].get(name, {})
            globally_enabled = self._as_bool(cfg.get("enabled_globally"), False)
            user_enabled = (
                self.is_plugin_enabled_for_user(name, uid) if uid else globally_enabled
            )

            result.append(
                {
                    "name": name,
                    "description": manifest.get("description", ""),
                    "version": manifest.get("version", "1.0.0"),
                    "tools": list(data["tools"].keys()),
                    "enabled_globally": globally_enabled,
                    "enabled_for_you": user_enabled,
                    # The ,plugin command path reads user_active; the tool path
                    # reads enabled_for_you. Both names, one value, so neither
                    # caller has to guess (the command used to KeyError here).
                    "user_active": user_enabled,
                    "allowed_users_count": len(cfg.get("allowed_users", [])),
                    "events": self.plugin_events(name),
                    "jobs": len(self._job_specs.get(name) or []),
                }
            )
        return result
