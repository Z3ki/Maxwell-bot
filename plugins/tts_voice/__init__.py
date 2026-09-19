"""Register Voice and TTS tools with Maxwell."""
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
    from .impl import TtsTool, JoinVcTool, VcStatusTool, VcWhereTool, LeaveVcTool

    mapping = [
        ('tts', TtsTool, 'ENABLE_TTS'),
        ('join_vc', JoinVcTool, None),
        ('vc_status', VcStatusTool, None),
        ('vc_where', VcWhereTool, None),
        ('leave_vc', LeaveVcTool, None),
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
