"""Register Runtime Controls tools with Maxwell."""

from maxwell_core.plugins.bundled import build_tools


def setup(bot, ctx=None):
    return build_tools(bot, __package__, [
        ('sleep', 'SleepTool', None),
        ('clear_sleep', 'ClearSleepTool', None),
        ('wait', 'WaitTool', None),
        ('more_tools', 'MoreToolsTool', None),
    ])
