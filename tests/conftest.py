"""Mirror ``bot_tools.X`` monkeypatches onto the modules that run the tools."""

from __future__ import annotations

import sys

import pytest

_MIRROR_MODULES = (
    "tooling.helpers",
    "plugins.web.impl",
    "plugins.media.impl",
    "plugins.images.impl",
    "plugins.sites.impl",
    "plugins.tts_voice.impl",
    "plugins.shell.impl",
    "plugins.discord_messages.impl",
    "plugins.discord_guild.impl",
    "plugins.discord_moderation.impl",
    "plugins.discord_presence.impl",
    "plugins.email.impl",
    "plugins.inbox.impl",
    "plugins.runtime_controls.impl",
    "plugins.personality.impl",
    "plugins.diagnostics.impl",
    "plugins.chess.impl",
    "plugins.plugin_admin.impl",
)

_NOTSET = object()


@pytest.fixture(autouse=True)
def _mirror_bot_tools_monkeypatches(monkeypatch):
    orig = monkeypatch.setattr

    def wrapped(target, name=_NOTSET, value=_NOTSET, raising=True):
        kwargs = {"raising": raising}
        if value is _NOTSET:
            result = orig(target, name, **kwargs)
        else:
            result = orig(target, name, value, **kwargs)
        attr = None
        attr_value = None
        if isinstance(target, str) and value is _NOTSET and target.startswith("bot_tools."):
            attr = target.split(".", 1)[1]
            attr_value = name
        elif (
            getattr(target, "__name__", "") == "bot_tools"
            and isinstance(name, str)
            and value is not _NOTSET
        ):
            attr = name
            attr_value = value
        if not attr or "." in attr:
            return result
        for modname in _MIRROR_MODULES:
            if sys.modules.get(modname) is None:
                continue
            orig(f"{modname}.{attr}", attr_value, raising=False)
        return result

    monkeypatch.setattr = wrapped  # type: ignore[method-assign]
    return monkeypatch
