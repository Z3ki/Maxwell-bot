"""A modular slice of the assembled system prompt."""

from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)


def _discard_awaitable(value: Any) -> None:
    """Close an accidental coroutine without starting work during composition."""
    close = getattr(value, "close", None)
    if callable(close):
        close()

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
    # Advisory estimate only: instructions must never be truncated mid-rule.
    token_budget: int | None = None
    when: Predicate | None = None
    # If set, only include this component when these tools are on the turn.
    requires_tools: tuple[str, ...] = ()
    # If set, only include when these plugins are enabled for the caller.
    requires_plugins: tuple[str, ...] = ()

    def applies(self, request: PromptRequest) -> bool:
        if self.scope not in {"always", request.scope}:
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
                result = self.when(request)
                if inspect.isawaitable(result):
                    _discard_awaitable(result)
                    logger.warning("Prompt component %s predicate returned an awaitable", self.id)
                    return False
                return bool(result)
            except Exception:
                return False
        return True

    def body(self, request: PromptRequest) -> str:
        if self.render is not None:
            try:
                text = self.render(request)
                if inspect.isawaitable(text):
                    _discard_awaitable(text)
                    logger.warning("Prompt component %s renderer returned an awaitable", self.id)
                    text = self.text
            except Exception:
                text = self.text
        else:
            text = self.text
        return str(text or "").strip()
