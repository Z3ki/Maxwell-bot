"""Versioned plugin.json validation.

Manifests are validated before the plugin's Python code is imported.
A bad manifest is rejected with an actionable error; it must not crash
the host or silently overwrite another plugin.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from maxwell_core import PLUGIN_API_VERSION
from maxwell_core.capabilities import normalize_capabilities

MANIFEST_SPEC_VERSION = 1

_LEGACY_KEYS = {
    "name",
    "version",
    "description",
    "enabled_globally",
    "allowed_users",
    "denied_users",
}


class ManifestError(ValueError):
    """Invalid plugin.json. ``path`` is set when known."""

    def __init__(self, message: str, *, path: Path | None = None) -> None:
        super().__init__(message)
        self.path = path


def _as_bool(value: Any, default: bool = False) -> bool:
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


def _as_str_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = str(item).strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _as_str_map(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


@dataclass
class ToolDeclaration:
    name: str
    description: str = ""
    returns_result: bool = False
    ends_turn: bool = False
    is_destructive: bool = False
    requires_admin: bool = False
    transports: tuple[str, ...] = ("any",)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "returns_result": self.returns_result,
            "ends_turn": self.ends_turn,
            "is_destructive": self.is_destructive,
            "requires_admin": self.requires_admin,
            "transports": list(self.transports),
        }


@dataclass
class PluginManifest:
    spec_version: int
    id: str
    name: str
    version: str
    description: str
    author: str
    api_version: int
    entry: str
    dependencies: list[str] = field(default_factory=list)
    optional_dependencies: list[str] = field(default_factory=list)
    required_capabilities: list[str] = field(default_factory=list)
    provided_capabilities: list[str] = field(default_factory=list)
    permissions: list[str] = field(default_factory=list)
    config_schema: dict[str, Any] = field(default_factory=dict)
    default_config: dict[str, Any] = field(default_factory=dict)
    platforms: list[str] = field(default_factory=lambda: ["any"])
    tools: list[ToolDeclaration] = field(default_factory=list)
    enabled_by_default: bool = False
    protected: bool = False
    uninstallable: bool = True
    bundled: bool = False
    requires_restart: bool = False
    data_version: int = 1
    raw: dict[str, Any] = field(default_factory=dict)

    # Legacy scoping fields kept so existing plugins.json still works.
    allowed_users: list[str] = field(default_factory=list)
    denied_users: list[str] = field(default_factory=list)

    @property
    def enabled_globally(self) -> bool:
        return self.enabled_by_default

    def tool_names(self) -> list[str]:
        return [item.name for item in self.tools]

    def as_public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "author": self.author,
            "api_version": self.api_version,
            "entry": self.entry,
            "dependencies": list(self.dependencies),
            "optional_dependencies": list(self.optional_dependencies),
            "required_capabilities": list(self.required_capabilities),
            "provided_capabilities": list(self.provided_capabilities),
            "permissions": list(self.permissions),
            "platforms": list(self.platforms),
            "tools": [t.as_dict() for t in self.tools],
            "enabled_by_default": self.enabled_by_default,
            "protected": self.protected,
            "uninstallable": self.uninstallable,
            "bundled": self.bundled,
            "requires_restart": self.requires_restart,
            "data_version": self.data_version,
            "config_schema": self.config_schema,
            "default_config": self.default_config,
        }


def _parse_tools(raw: Any) -> list[ToolDeclaration]:
    if not isinstance(raw, list):
        return []
    out: list[ToolDeclaration] = []
    seen: set[str] = set()
    for item in raw:
        if isinstance(item, str):
            name = item.strip()
            if not name or name in seen:
                continue
            seen.add(name)
            out.append(ToolDeclaration(name=name))
            continue
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        transports = item.get("transports") or item.get("platforms") or ["any"]
        if isinstance(transports, str):
            transports = [transports]
        out.append(
            ToolDeclaration(
                name=name,
                description=str(item.get("description") or ""),
                returns_result=_as_bool(item.get("returns_result"), False),
                ends_turn=_as_bool(item.get("ends_turn"), False),
                is_destructive=_as_bool(item.get("is_destructive"), False),
                requires_admin=_as_bool(item.get("requires_admin"), False),
                transports=tuple(str(x) for x in transports),
            )
        )
    return out


def validate_manifest(
    data: dict[str, Any],
    *,
    directory_name: str,
    path: Path | None = None,
) -> PluginManifest:
    if not isinstance(data, dict):
        raise ManifestError("plugin.json must be a JSON object", path=path)

    plugin_id = str(data.get("id") or data.get("name") or directory_name).strip()
    if not plugin_id.isidentifier():
        raise ManifestError(
            f"plugin id {plugin_id!r} is not a valid Python identifier "
            f"(directory {directory_name!r})",
            path=path,
        )
    version = str(data.get("version") or "0.0.0").strip() or "0.0.0"
    api_version = data.get("api_version", data.get("plugin_api", PLUGIN_API_VERSION))
    try:
        api_version_i = int(api_version)
    except (TypeError, ValueError) as exc:
        raise ManifestError(
            f"api_version must be an integer (got {api_version!r})", path=path
        ) from exc
    if api_version_i != PLUGIN_API_VERSION:
        raise ManifestError(
            f"plugin {plugin_id!r} targets API v{api_version_i}, host is "
            f"v{PLUGIN_API_VERSION}",
            path=path,
        )

    spec_version = data.get("manifest_version", data.get("spec_version", MANIFEST_SPEC_VERSION))
    try:
        spec_version_i = int(spec_version)
    except (TypeError, ValueError):
        spec_version_i = MANIFEST_SPEC_VERSION

    tools = _parse_tools(data.get("tools"))
    dual = [t.name for t in tools if t.returns_result and t.ends_turn]
    if dual:
        raise ManifestError(
            f"plugin {plugin_id!r} tools cannot both return a result and "
            f"end the turn: {', '.join(dual)}",
            path=path,
        )

    display = str(data.get("display_name") or data.get("title") or plugin_id)
    enabled = _as_bool(
        data.get("enabled_by_default", data.get("enabled_globally")), False
    )
    return PluginManifest(
        spec_version=spec_version_i,
        id=plugin_id,
        name=display,
        version=version,
        description=str(data.get("description") or ""),
        author=str(data.get("author") or ""),
        api_version=api_version_i,
        entry=str(data.get("entry") or data.get("entrypoint") or ""),
        dependencies=_as_str_list(data.get("dependencies")),
        optional_dependencies=_as_str_list(
            data.get("optional_dependencies") or data.get("optional_python")
        ),
        required_capabilities=normalize_capabilities(
            data.get("required_capabilities") or data.get("requires")
        ),
        provided_capabilities=normalize_capabilities(
            data.get("provided_capabilities") or data.get("provides")
        ),
        permissions=normalize_capabilities(
            data.get("permissions") or data.get("requested_permissions")
        ),
        config_schema=_as_str_map(data.get("config_schema")),
        default_config=_as_str_map(data.get("default_config") or data.get("config")),
        platforms=_as_str_list(data.get("platforms") or data.get("supported_platforms"))
        or ["any"],
        tools=tools,
        enabled_by_default=enabled,
        protected=_as_bool(data.get("protected"), False),
        uninstallable=_as_bool(data.get("uninstallable"), True),
        bundled=_as_bool(data.get("bundled"), False),
        requires_restart=_as_bool(data.get("requires_restart"), False),
        data_version=int(data.get("data_version") or 1),
        raw=dict(data),
        allowed_users=_as_str_list(data.get("allowed_users")),
        denied_users=_as_str_list(data.get("denied_users")),
    )


def load_manifest_file(path: Path, *, directory_name: str) -> PluginManifest:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ManifestError(
            f"invalid JSON in {path}: {exc}", path=path
        ) from exc
    except OSError as exc:
        raise ManifestError(f"cannot read {path}: {exc}", path=path) from exc
    if not isinstance(raw, dict):
        raise ManifestError("plugin.json must be a JSON object", path=path)
    return validate_manifest(raw, directory_name=directory_name, path=path)
