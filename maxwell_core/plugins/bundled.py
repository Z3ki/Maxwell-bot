"""Shared, lazy registration for bundled tool plugins."""

from importlib import import_module
from typing import Any


def build_tools(bot: Any, package: str, declarations: list[tuple]) -> list[Any]:
    """Import implementations only when a configured tool will be exposed."""
    config = getattr(bot, "config", None)
    enabled = []
    for name, class_name, features in declarations:
        if isinstance(features, str):
            features = (features,)
        if all(bool(getattr(config, flag, False)) for flag in features or ()):
            enabled.append((name, class_name))
    if not enabled:
        return []
    implementation = import_module(f"{package}.impl")
    tools = []
    for name, class_name in enabled:
        tool = getattr(implementation, class_name)(bot)
        tool.name = tool.tool_name = name
        tools.append(tool)
    return tools
