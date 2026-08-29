"""
Checkers plugin setup entrypoint.
"""
from .tools import (
    CheckersStartTool,
    CheckersMoveTool,
    CheckersStateTool,
    CheckersResignTool,
)


def setup(bot):
    """Register tools into Maxwell's plugin ecosystem."""
    return [
        CheckersStartTool(bot),
        CheckersMoveTool(bot),
        CheckersStateTool(bot),
        CheckersResignTool(bot),
    ]
