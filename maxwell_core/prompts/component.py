"""A modular slice of the assembled system prompt."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

# Where a component is inserted in the assembled prompt.
POSITIONS = (
    "preamble",
    "identity",
    "personality",
    "style",
    "protocol",
    "tools",
    "plugin",
    "server",
    "context",
    "suffix",
)

# When a component applies.
SCOPES = (
    "always",
    "discord",
    "telegram",
    "voice",
    "autonomy",
    "jobs",
    "memory",
    "background",
)


@dataclass
class PromptRequest:
    """What the host knows about the current turn when assembling prompts."""

    scope: str = "discord"
    platform: str = "discord"
    plugin_ids: tuple[str, ...] = ()
    tool_names: tuple[str, ...] = ()
    is_admin: bool = False
    guild_id: str | None = None
    channel_id: str | None = None
    user_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


Predicate = Callable[[PromptRequest], bool]
Renderer = Callable[[PromptRequest], str]


@dataclass
class PromptComponent:
    id: str
    plugin: str
    text: str = ""
    render: Renderer | None = None
    scope: str = "always"
    position: str = "plugin"
    priority: int = 100
    token_budget: int | None = None
    when: Predicate | None = None
    # If set, only include this component when these tools are on the turn.
    requires_tools: tuple[str, ...] = ()
    # If set, only include when these plugins are enabled for the caller.
    requires_plugins: tuple[str, ...] = ()

    def applies(self, request: PromptRequest) -> bool:
        if self.scope not in {"always", request.scope, request.platform}:
            return False
        if self.requires_plugins:
            have = set(request.plugin_ids)
            if not all(name in have for name in self.requires_plugins):
                return False
        if self.requires_tools:
            have = set(request.tool_names)
            if not any(name in have for name in self.requires_tools):
                return False
        if self.when is not None:
            try:
                return bool(self.when(request))
            except Exception:
                return False
        return True

    def body(self, request: PromptRequest) -> str:
        if self.render is not None:
            try:
                text = self.render(request)
            except Exception:
                text = self.text
        else:
            text = self.text
        return str(text or "").strip()
