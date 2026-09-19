"""Register Chess tools with Maxwell."""
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
    from tooling.helpers import __CHESS_IMPORTED__
    if not __CHESS_IMPORTED__:
        return []
    from .impl import ChessStartTool, ChessMoveTool, ChessStateTool, ChessResignTool

    mapping = [
        ('chess_start', ChessStartTool, None),
        ('chess_move', ChessMoveTool, None),
        ('chess_state', ChessStateTool, None),
        ('chess_resign', ChessResignTool, None),
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
