"""Maxwell core runtime: plugins, tools, prompts, hooks, and services.

This package is the stable host API. Feature code lives in ``plugins/``.
The Discord client in ``bot.py`` is a transport and composition root; it
should not grow new hardcoded feature lists.
"""

from __future__ import annotations

PLUGIN_API_VERSION = 1
PLUGIN_API_VERSION_NAME = "1"

__all__ = ["PLUGIN_API_VERSION", "PLUGIN_API_VERSION_NAME"]
