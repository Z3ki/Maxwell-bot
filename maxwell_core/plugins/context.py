"""PluginContext: the only supported way for a plugin to reach the host."""

from __future__ import annotations

import inspect
import logging
import math
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from maxwell_core.capabilities import missing_capabilities
from maxwell_core.hooks import HOOK_NAMES
from maxwell_core.prompts.component import PromptComponent

if TYPE_CHECKING:
    from .manager import PluginManager

logger = logging.getLogger("maxwell.plugins")

MIN_JOB_INTERVAL_SECONDS = 5.0

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
        "on_interaction",
    }
)


class PluginContext:
    """What a plugin gets to work with, and the only supported way in.

    Handed to ``setup(bot, ctx)`` when the plugin accepts a second argument.
    Plugins written against the old one-argument ``setup(bot)`` keep working.
    """

    def __init__(self, manager: PluginManager, plugin_name: str) -> None:
        self._manager = manager
        self.name = plugin_name
        self.bot = manager.bot
        self.log = logging.getLogger(f"maxwell.plugins.{plugin_name}")

    # -- storage ----------------------------------------------------------- #
    @property
    def data_dir(self) -> Path:
        return self._manager.get_plugin_data_dir(self.name)

    def store_path(self, filename: str) -> Path:
        safe = os.path.basename(str(filename or "").strip())
        if not safe or safe in {".", ".."}:
            raise ValueError("filename is required and must not traverse")
        return self.data_dir / safe

    @property
    def config(self) -> dict[str, Any]:
        """Merged default + persisted plugin configuration."""
        return self._manager.plugin_config(self.name)

    def granted_permissions(self) -> list[str]:
        return list(self._manager.granted_permissions(self.name))

    def has_permission(self, capability: str) -> bool:
        return capability in self.granted_permissions()

    def require_capability(self, *capabilities: str) -> None:
        missing = missing_capabilities(self.granted_permissions(), capabilities)
        if missing:
            raise PermissionError(
                f"plugin {self.name!r} is missing capabilities: {', '.join(missing)}"
            )

    # -- events + jobs ----------------------------------------------------- #
    def on_event(self, event: str, callback: Callable[..., Any]) -> None:
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
        if not callable(callback):
            raise TypeError("callback must be callable")
        if not inspect.iscoroutinefunction(callback):
            raise TypeError("scheduled callback must be an async def")
        try:
            interval = float(seconds)
        except (TypeError, ValueError):
            raise ValueError("seconds must be a number") from None
        if not math.isfinite(interval) or interval < MIN_JOB_INTERVAL_SECONDS:
            raise ValueError(
                f"interval must be at least {MIN_JOB_INTERVAL_SECONDS}s "
                "(a tighter loop starves the reply path)"
            )
        self._manager._register_job(
            self.name, interval, callback, run_immediately=bool(run_immediately)
        )

    def spawn(self, awaitable: Any, *, name: str | None = None) -> Any:
        return self._manager.spawn_task(self.name, awaitable, name=name)

    def after(
        self,
        seconds: float,
        callback: Any,
        *,
        name: str | None = None,
    ) -> Any:
        return self._manager.spawn_after(self.name, seconds, callback, name=name)

    # -- extensions -------------------------------------------------------- #
    def register_tool(self, tool: Any, *, name: str | None = None) -> None:
        self._manager.register_context_tool(self.name, tool, name=name)

    def register_hook(
        self,
        hook: str,
        callback: Callable[..., Any],
        *,
        priority: int = 100,
    ) -> None:
        if hook not in HOOK_NAMES:
            raise ValueError(
                f"Unknown hook {hook!r}. Known: {', '.join(sorted(HOOK_NAMES))}"
            )
        self._manager.hooks.register(self.name, hook, callback, priority=priority)

    def register_prompt(self, component: PromptComponent) -> None:
        if component.plugin != self.name:
            component.plugin = self.name
        self._manager.prompts.register(component)

    def register_service(self, name: str, service: Any) -> None:
        self._manager.services.register(name, service, owner=self.name)

    def register_provider(self, provider: Any, *, name: str | None = None) -> None:
        key = str(name or getattr(provider, "name", "") or "").strip()
        if not key:
            raise ValueError("provider name is required")
        self._manager.services.register(f"provider:{key}", provider, owner=self.name)
        self._manager._register_extension(self.name, "provider", key, provider)

    def register_memory(self, backend: Any, *, name: str | None = None) -> None:
        key = str(name or getattr(backend, "name", "memory") or "memory")
        self._manager.services.register(f"memory:{key}", backend, owner=self.name)
        self._manager._register_extension(self.name, "memory", key, backend)

    def register_command(self, command: Any, *, name: str | None = None) -> None:
        key = str(name or getattr(command, "name", "") or "").strip()
        if not key:
            raise ValueError("command name is required")
        self._manager._register_extension(self.name, "command", key, command)

    def register_api_route(
        self,
        method: str,
        path: str,
        handler: Callable[..., Any],
        *,
        owner_only: bool = True,
    ) -> None:
        method_u = str(method or "GET").upper()
        route = str(path or "").strip()
        if not route.startswith("/api/plugin/"):
            raise ValueError(
                "plugin API routes must live under /api/plugin/<plugin>/ "
                "so they cannot shadow protected admin routes"
            )
        expected = f"/api/plugin/{self.name}"
        if route != expected and not route.startswith(expected + "/"):
            raise ValueError(
                f"plugin {self.name!r} may only register routes under {expected}/"
            )
        self._manager._register_extension(
            self.name,
            "api_route",
            f"{method_u} {route}",
            {"method": method_u, "path": route, "handler": handler, "owner_only": owner_only},
        )

    def register_dashboard_panel(self, panel: dict[str, Any]) -> None:
        title = str((panel or {}).get("id") or (panel or {}).get("title") or "").strip()
        if not title:
            raise ValueError("dashboard panel needs an id or title")
        self._manager._register_extension(self.name, "dashboard_panel", title, dict(panel))

    def add_view(self, view: Any, *, message_id: int | None = None) -> Any:
        register = getattr(self.bot, "add_view", None)
        if not callable(register):
            raise TypeError("bot client does not support persistent views")
        register(view, message_id=message_id)
        self._manager._track_view(self.name, view)
        return view

    def add_dynamic_items(self, *items: Any) -> None:
        register = getattr(self.bot, "add_dynamic_items", None)
        if not callable(register):
            raise TypeError("installed discord.py does not support dynamic items")
        register(*items)
        self._manager._track_dynamic_items(self.name, items)

    # -- reaching the rest of the bot -------------------------------------- #
    def tool(self, name: str) -> Any:
        registry = getattr(self._manager, "tool_registry", None)
        if registry is not None:
            found = registry.tool(str(name))
            if found is not None:
                return found
        return (getattr(self.bot, "tools", None) or {}).get(str(name))

    def service(self, name: str, default: Any = None) -> Any:
        return self._manager.services.get(name, default)

    def is_admin(self, user_id: Any) -> bool:
        checker = getattr(self.bot, "_is_admin", None)
        if not callable(checker):
            return False
        try:
            return bool(checker(user_id))
        except Exception:
            return False

    def is_owner(self, user_id: Any) -> bool:
        checker = getattr(self.bot, "_is_owner", None)
        if callable(checker):
            try:
                return bool(checker(user_id))
            except Exception:
                return False
        return self.is_admin(user_id)
