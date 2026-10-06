"""Register Chess tools with Maxwell."""

from maxwell_core.plugins.bundled import build_tools


def setup(bot, ctx=None):
    from tooling.helpers import __CHESS_IMPORTED__

    if not __CHESS_IMPORTED__:
        return []
    if ctx is not None:
        from .impl import cancel_idle_games

        async def _chess_idle_tick():
            await cancel_idle_games(bot)

        ctx.every(60.0, _chess_idle_tick, run_immediately=True)
    return build_tools(bot, __package__, [
        ('chess_start', 'ChessStartTool', None),
        ('chess_move', 'ChessMoveTool', None),
        ('chess_state', 'ChessStateTool', None),
        ('chess_resign', 'ChessResignTool', None),
    ])
