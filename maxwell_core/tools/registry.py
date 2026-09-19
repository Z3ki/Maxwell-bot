"""Canonical live tool registry.

Native tool-calling, compatibility parsers, the dashboard, autonomy, and
plugin management all resolve tools through this registry.
"""

from __future__ import annotations

from typing import Any, Iterable, Iterator

from .spec import (
    CONTRACT_ENDING,
    CONTRACT_RESULT,
    ToolSpec,
    spec_from_tool,
)

_GLOBAL: ToolRegistry | None = None


class ToolRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}
        self._plugin_tools: dict[str, set[str]] = {}
        self._protected: set[str] = set()

    def __contains__(self, name: str) -> bool:
        return str(name) in self._specs

    def __len__(self) -> int:
        return len(self._specs)

    def __iter__(self) -> Iterator[str]:
        return iter(self._specs)

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(str(name))

    def tool(self, name: str) -> Any | None:
        spec = self.get(name)
        return None if spec is None else spec.tool

    def names(self) -> list[str]:
        return sorted(self._specs)

    def specs(self) -> list[ToolSpec]:
        return [self._specs[name] for name in sorted(self._specs)]

    def register(
        self,
        spec: ToolSpec,
        *,
        protected: bool = False,
        allow_override: bool = False,
    ) -> None:
        name = str(spec.name or "").strip()
        if not name:
            raise ValueError("tool name is required")
        existing = self._specs.get(name)
        if existing is not None:
            if name in self._protected and not allow_override:
                raise ValueError(
                    f"tool {name!r} is a protected core capability owned by "
                    f"{existing.plugin!r}; {spec.plugin!r} cannot replace it"
                )
            if not (allow_override or spec.override):
                raise ValueError(
                    f"tool {name!r} is already registered by plugin "
                    f"{existing.plugin!r}; {spec.plugin!r} cannot shadow it "
                    "without override=true"
                )
            self._plugin_tools.get(existing.plugin, set()).discard(name)
        self._specs[name] = spec
        self._plugin_tools.setdefault(spec.plugin, set()).add(name)
        if protected:
            self._protected.add(name)
        # Keep the live instance's name in sync with the registry key.
        try:
            spec.tool.name = name
            spec.tool.tool_name = name
        except Exception:
            pass

    def register_tool(
        self,
        tool: Any,
        *,
        name: str,
        plugin: str,
        protected: bool = False,
        allow_override: bool = False,
    ) -> ToolSpec:
        spec = spec_from_tool(tool, name=name, plugin=plugin)
        self.register(spec, protected=protected, allow_override=allow_override)
        return spec

    def unregister_plugin(self, plugin: str) -> list[str]:
        names = list(self._plugin_tools.pop(str(plugin), set()))
        removed: list[str] = []
        for name in names:
            spec = self._specs.get(name)
            if spec is None or spec.plugin != plugin:
                continue
            if name in self._protected:
                # Protected tools stay until a replacement is registered.
                continue
            self._specs.pop(name, None)
            self._protected.discard(name)
            removed.append(name)
        return removed

    def plugin_tools(self, plugin: str) -> list[str]:
        return sorted(self._plugin_tools.get(str(plugin), set()))

    def snapshot_as_dict(self) -> dict[str, Any]:
        """``{name: tool}`` view used by the existing dispatch loop."""
        return {name: spec.tool for name, spec in self._specs.items()}

    def visible(
        self,
        *,
        platform: str = "discord",
        disabled: Iterable[str] | None = None,
        allowed_plugins: Iterable[str] | None = None,
        include_admin: bool = True,
    ) -> dict[str, ToolSpec]:
        blocked = {str(n) for n in (disabled or ())}
        plugins = None if allowed_plugins is None else set(allowed_plugins)
        out: dict[str, ToolSpec] = {}
        for name, spec in self._specs.items():
            if name in blocked:
                continue
            if plugins is not None and spec.plugin not in plugins:
                continue
            if not spec.available_on(platform):
                continue
            if spec.requires_admin and not include_admin:
                continue
            out[name] = spec
        return out

    def openai_tools(
        self,
        *,
        platform: str = "discord",
        disabled: Iterable[str] | None = None,
        allowed_names: Iterable[str] | None = None,
        allowed_plugins: Iterable[str] | None = None,
        include_admin: bool = True,
        max_description_chars: int = 1024,
    ) -> list[dict[str, Any]]:
        allowed = None if allowed_names is None else set(allowed_names)
        visible = self.visible(
            platform=platform,
            disabled=disabled,
            allowed_plugins=allowed_plugins,
            include_admin=include_admin,
        )
        out: list[dict[str, Any]] = []
        for name, spec in visible.items():
            if allowed is not None and name not in allowed:
                continue
            out.append(spec.openai_tool(max_description_chars=max_description_chars))
        return out

    def result_names(self) -> set[str]:
        return {name for name, spec in self._specs.items() if spec.contract == CONTRACT_RESULT}

    def ending_names(self) -> set[str]:
        return {name for name, spec in self._specs.items() if spec.contract == CONTRACT_ENDING}

    def validate_contracts(self) -> list[str]:
        """Return problems: missing schema, missing impl, dual contract."""
        problems: list[str] = []
        for name, spec in self._specs.items():
            if spec.tool is None or not callable(getattr(spec.tool, "execute", None)):
                problems.append(f"{name}: no executable implementation")
            params = spec.schema()
            if params.get("type") != "object":
                problems.append(f"{name}: parameters are not a JSON object schema")
            if spec.returns_result and spec.ends_turn:
                problems.append(f"{name}: cannot both return a result and end the turn")
        return problems


def get_global_registry() -> ToolRegistry:
    global _GLOBAL
    if _GLOBAL is None:
        _GLOBAL = ToolRegistry()
    return _GLOBAL


def set_global_registry(registry: ToolRegistry | None) -> None:
    global _GLOBAL
    _GLOBAL = registry
