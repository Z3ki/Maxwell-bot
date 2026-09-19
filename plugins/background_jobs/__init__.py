"""Register the spawn_background tool."""
from jobs import SpawnBackgroundTool


def setup(bot, ctx=None):
    tool = SpawnBackgroundTool(bot)
    tool.name = "spawn_background"
    tool.tool_name = "spawn_background"
    tool.returns_result = True
    return [tool]
