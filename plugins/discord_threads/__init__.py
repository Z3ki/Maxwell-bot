"""Register Discord thread tools."""
from discord_threads import CreateThreadTool, ThreadControlTool


def setup(bot, ctx=None):
    start = CreateThreadTool(bot)
    start.name = "create_thread"
    start.tool_name = "create_thread"
    start.returns_result = True
    control = ThreadControlTool(bot)
    control.name = "thread_control"
    control.tool_name = "thread_control"
    control.returns_result = True
    return [start, control]
