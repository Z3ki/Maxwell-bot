"""Register Chess tools with Maxwell."""

from maxwell_core.plugins.bundled import build_tools


def setup(bot, ctx=None):
    from tooling.helpers import __CHESS_IMPORTED__

    if not __CHESS_IMPORTED__:
        return []
    return build_tools(bot, __package__, [
        ('chess_start', 'ChessStartTool', None),
        ('chess_move', 'ChessMoveTool', None),
        ('chess_state', 'ChessStateTool', None),
        ('chess_resign', 'ChessResignTool', None),
    ])
