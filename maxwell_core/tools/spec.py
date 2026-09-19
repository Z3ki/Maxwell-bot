"""Self-describing tool metadata.

A registered tool is the single source of truth for schema, contract,
permissions, and dispatch. Static catalogs are derived from this.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


CONTRACT_RESULT = "result"
CONTRACT_ENDING = "ending"
CONTRACT_SILENT = "silent"

_CONTRACT_LABELS = {
    CONTRACT_RESULT: " [returns output]",
    CONTRACT_ENDING: " [ends the turn]",
    CONTRACT_SILENT: " [returns nothing]",
}


def result_contract_label(kind: str) -> str:
    return _CONTRACT_LABELS.get(kind, _CONTRACT_LABELS[CONTRACT_SILENT])


@dataclass
class ToolSpec:
    name: str
    plugin: str
    tool: Any
    description: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)
    returns_result: bool = False
    ends_turn: bool = False
    is_destructive: bool = False
    requires_admin: bool = False
    required_capabilities: tuple[str, ...] = ()
    required_discord_permissions: tuple[str, ...] = ()
    transports: tuple[str, ...] = ("any",)
    timeout_seconds: float | None = None
    side_effects: bool = True
    produces_visible_output: bool = False
    override: bool = False

    @property
    def contract(self) -> str:
        if self.returns_result:
            return CONTRACT_RESULT
        if self.ends_turn:
            return CONTRACT_ENDING
        return CONTRACT_SILENT

    def available_on(self, platform: str) -> bool:
        allowed = set(self.transports) or {"any"}
        return "any" in allowed or platform in allowed

    def schema(self) -> dict[str, Any]:
        params = dict(self.parameters or {})
        if params.get("type") != "object":
            params = {
                "type": "object",
                "properties": params.get("properties") or {},
                "additionalProperties": True,
            }
        props = params.get("properties")
        if not isinstance(props, dict):
            params["properties"] = {}
        required = params.get("required")
        if not isinstance(required, list):
            params["required"] = []
        return params

    def openai_tool(self, *, max_description_chars: int = 1024) -> dict[str, Any]:
        desc = str(self.description or self.name).strip() or self.name
        try:
            limit = max(1, int(max_description_chars))
        except (TypeError, ValueError):
            limit = 1024
        if len(desc) > limit:
            desc = desc[: limit - 1] + "…"
        desc = desc + result_contract_label(self.contract)
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": desc,
                "parameters": self.schema(),
            },
        }

    def as_public_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "plugin": self.plugin,
            "description": self.description,
            "returns_result": self.returns_result,
            "ends_turn": self.ends_turn,
            "is_destructive": self.is_destructive,
            "requires_admin": self.requires_admin,
            "required_capabilities": list(self.required_capabilities),
            "transports": list(self.transports),
            "side_effects": self.side_effects,
            "produces_visible_output": self.produces_visible_output,
            "contract": self.contract,
        }


def spec_from_tool(tool: Any, *, name: str, plugin: str) -> ToolSpec:
    """Build a ToolSpec from a live Tool instance."""
    runtime_name = str(name or getattr(tool, "tool_name", None) or getattr(tool, "name", "") or "").strip()
    description = ""
    getter = getattr(tool, "get_description", None)
    if callable(getter):
        try:
            description = str(getter() or "").strip()
        except Exception:
            description = runtime_name
    parameters: dict[str, Any] = {}
    params_getter = getattr(tool, "get_parameters", None)
    if callable(params_getter):
        try:
            raw = params_getter()
            if isinstance(raw, dict):
                parameters = raw
        except Exception:
            parameters = {}
    transports = getattr(tool, "transports", None) or getattr(tool, "platform", "any")
    if isinstance(transports, str):
        transports = (transports,)
    caps = getattr(tool, "required_capabilities", ()) or ()
    discord_perms = getattr(tool, "required_discord_permissions", ()) or ()
    timeout = getattr(tool, "timeout_seconds", None)
    try:
        timeout_val = float(timeout) if timeout is not None else None
    except (TypeError, ValueError):
        timeout_val = None
    return ToolSpec(
        name=runtime_name,
        plugin=plugin,
        tool=tool,
        description=description,
        parameters=parameters,
        returns_result=bool(getattr(tool, "returns_result", False)),
        ends_turn=bool(getattr(tool, "ends_turn", False)),
        is_destructive=bool(getattr(tool, "is_destructive", False)),
        requires_admin=bool(getattr(tool, "requires_admin", False)),
        required_capabilities=tuple(str(x) for x in caps),
        required_discord_permissions=tuple(str(x) for x in discord_perms),
        transports=tuple(str(x) for x in transports),
        timeout_seconds=timeout_val,
        side_effects=bool(getattr(tool, "side_effects", True)),
        produces_visible_output=bool(getattr(tool, "produces_visible_output", False)),
    )
