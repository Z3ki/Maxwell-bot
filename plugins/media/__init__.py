"""Register Media Understanding tools with Maxwell."""
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
    from .impl import SeeImageTool, SeeVideoTool, SendMemeTool, SendMediaTool

    mapping = [
        ('see_image', SeeImageTool, None),
        ('see_video', SeeVideoTool, None),
        ('send_meme', SendMemeTool, None),
        ('send_media', SendMediaTool, None),
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
