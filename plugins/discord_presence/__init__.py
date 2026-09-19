"""Register Discord Presence tools with Maxwell."""
from __future__ import annotations

from typing import Any


def _flag(bot: Any, name: str | None) -> bool:
    if not name:
        return True
    cfg = getattr(bot, "config", None)
    if cfg is None:
        return True
    return bool(getattr(cfg, name, True))


def setup(bot, ctx=None):
    pass
    from .impl import ChangePresenceTool, SetActivityTool, SetNicknameTool, ChangeAvatarTool

    mapping = [
        ('change_presence', ChangePresenceTool, None),
        ('set_activity', SetActivityTool, None),
        ('set_nickname', SetNicknameTool, None),
        ('change_avatar', ChangeAvatarTool, 'ENABLE_AVATAR'),
    ]
    tools = []
    for runtime_name, cls, enable_name in mapping:
        if not _flag(bot, enable_name):
            continue
        inst = cls(bot)
        inst.name = runtime_name
        inst.tool_name = runtime_name
        tools.append(inst)
    return tools
