import json
import logging
import os
import sys
from importlib import import_module, reload
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("maxwell.plugins")


class PluginManager:
    """Manages dynamic modular plugins for Maxwell.
    
    Plugins live in `plugins/<plugin_name>/` and contain:
      - plugin.json: manifest metadata and defaults
      - __init__.py / tools.py: exports setup(bot) -> list[BaseTool]
    
    Runtime states (global vs per-user enabled status) persist in `data/plugins.json`.
    """

    def __init__(self, bot, plugins_dir: Optional[str] = None, data_dir: Optional[str] = None, state_file: Optional[str] = None):
        self.bot = bot
        self.root_dir = Path(os.path.abspath(os.path.dirname(__file__)))
        self.plugins_dir = Path(plugins_dir) if plugins_dir else (self.root_dir / "plugins")
        self.data_dir = Path(data_dir) if data_dir else (self.root_dir / "data")
        self.state_file = Path(state_file) if state_file else (self.data_dir / "plugins.json")

        self.plugins_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # Loaded plugin metadata & tool objects:
        # {plugin_name: {"manifest": dict, "module": module, "tools": dict[tool_name, tool_obj]}}
        self.loaded_plugins: Dict[str, Dict[str, Any]] = {}
        # Global registry of all plugin tool instances: {tool_name: (plugin_name, tool_obj)}
        self.all_plugin_tools: Dict[str, tuple[str, Any]] = {}

        # Config / enabled status state:
        # {"plugins": {<name>: {"enabled_globally": bool, "allowed_users": list[str], "denied_users": list[str]}}}
        self.state: Dict[str, Any] = {"plugins": {}}
        self._load_state()

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
        if loaded is None or not isinstance(loaded, dict) or loaded.get("plugins") != plugins:
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
        """Return isolated storage directory for a plugin under data/plugins/<plugin_name>/."""
        pdir = self.data_dir / "plugins" / plugin_name
        pdir.mkdir(parents=True, exist_ok=True)
        return pdir

    def load_plugins(self) -> Dict[str, Any]:
        """Scan plugins directory, import each plugin, and register its tools."""
        self.loaded_plugins.clear()
        self.all_plugin_tools.clear()

        # Add plugins directory parent to sys.path if not present
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
                    "Skipping plugin %r: directory name is not a valid Python identifier",
                    plugin_name,
                )
                continue
            try:
                manifest = self._load_manifest(entry, plugin_name)
                module_name = f"plugins.{plugin_name}"
                # If custom plugins_dir or not under root plugins/, import via spec
                init_py = entry / "__init__.py"
                tools_py = entry / "tools.py"
                target_file = init_py if init_py.exists() else (tools_py if tools_py.exists() else None)
                if not target_file:
                    logger.warning(f"No __init__.py or tools.py found in plugin '{plugin_name}'")
                    continue

                import importlib.util
                # Remove the package and its submodules before every reload.
                # Re-executing only __init__.py otherwise leaves an old
                # plugins.<name>.tools module in sys.modules, so edited tool
                # code never reaches the live registry.
                for loaded_name in list(sys.modules):
                    if loaded_name == module_name or loaded_name.startswith(
                        module_name + "."
                    ):
                        sys.modules.pop(loaded_name, None)
                spec = importlib.util.spec_from_file_location(
                    module_name,
                    str(target_file),
                    submodule_search_locations=[str(entry)]
                    if target_file == init_py
                    else None,
                )
                if spec and spec.loader:
                    mod = importlib.util.module_from_spec(spec)
                    sys.modules[module_name] = mod
                    spec.loader.exec_module(mod)
                elif module_name in sys.modules:
                    mod = reload(sys.modules[module_name])
                else:
                    mod = import_module(module_name)

                tools_list = []
                if hasattr(mod, "setup"):
                    res = mod.setup(self.bot)
                    if isinstance(res, list):
                        tools_list = res
                elif hasattr(mod, "get_tools"):
                    res = mod.get_tools(self.bot)
                    if isinstance(res, list):
                        tools_list = res

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
                }
                
                # Initialize state entry if missing
                if plugin_name not in self.state["plugins"]:
                    self.state["plugins"][plugin_name] = {
                        "enabled_globally": self._as_bool(
                            manifest.get("enabled_globally"), False
                        ),
                        "allowed_users": self._user_ids(
                            manifest.get("allowed_users", [])
                        ),
                        "denied_users": self._user_ids(
                            manifest.get("denied_users", [])
                        ),
                    }
                    self._save_state()

                logger.info(f"Loaded plugin '{plugin_name}' with {len(tool_dict)} tool(s)")
            except Exception as e:
                for loaded_name in list(sys.modules):
                    if loaded_name == f"plugins.{plugin_name}" or loaded_name.startswith(
                        f"plugins.{plugin_name}."
                    ):
                        sys.modules.pop(loaded_name, None)
                logger.exception(f"Error loading plugin '{plugin_name}': {e}")

        return self.loaded_plugins

    def reload_plugins(self) -> str:
        """Reload the plugin directory and report the result to the caller."""
        try:
            loaded = self.load_plugins()
        except Exception as exc:
            logger.exception("Failed to reload plugins")
            return f"Error reloading plugins: {exc}"
        tool_count = sum(
            len(data.get("tools") or {})
            for data in loaded.values()
            if isinstance(data, dict)
        )
        return f"Reloaded {len(loaded)} plugin(s) with {tool_count} tool(s)."

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

    def is_plugin_enabled_for_user(self, plugin_name: str, user_id: str | int | None) -> bool:
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

    def get_available_tools(self, user_id: str | int | None = None, platform: str = "discord") -> Dict[str, Any]:
        """Alias for get_available_tools_for_user."""
        return self.get_available_tools_for_user(user_id=user_id, platform=platform)

    def get_available_tools_for_user(self, user_id: str | int | None, platform: str = "discord") -> Dict[str, Any]:
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

    def enable_plugin(self, plugin_name: str, user_id: Optional[str | int] = None, is_global: bool = False) -> str:
        """Enable a plugin globally or for a specific user."""
        is_global = self._as_bool(is_global, False)
        if plugin_name not in self.loaded_plugins:
            return f"Plugin '{plugin_name}' is not installed."

        cfg = self.state["plugins"].setdefault(plugin_name, {
            "enabled_globally": False,
            "allowed_users": [],
            "denied_users": [],
        })
        if not isinstance(cfg, dict):
            cfg = {
                "enabled_globally": False,
                "allowed_users": [],
                "denied_users": [],
            }
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

    def disable_plugin(self, plugin_name: str, user_id: Optional[str | int] = None, is_global: bool = False) -> str:
        """Disable a plugin globally or for a specific user."""
        is_global = self._as_bool(is_global, False)
        if plugin_name not in self.loaded_plugins:
            return f"Plugin '{plugin_name}' is not installed."

        cfg = self.state["plugins"].setdefault(plugin_name, {
            "enabled_globally": False,
            "allowed_users": [],
            "denied_users": [],
        })
        if not isinstance(cfg, dict):
            cfg = {
                "enabled_globally": False,
                "allowed_users": [],
                "denied_users": [],
            }
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
            user_enabled = self.is_plugin_enabled_for_user(name, uid) if uid else globally_enabled

            result.append({
                "name": name,
                "description": manifest.get("description", ""),
                "version": manifest.get("version", "1.0.0"),
                "tools": list(data["tools"].keys()),
                "enabled_globally": globally_enabled,
                "enabled_for_you": user_enabled,
                "allowed_users_count": len(cfg.get("allowed_users", [])),
            })
        return result
