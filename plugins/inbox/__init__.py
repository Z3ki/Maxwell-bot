"""Register Inbox tools with Maxwell."""

from maxwell_core.plugins.bundled import build_tools


def setup(bot, ctx=None):
    return build_tools(bot, __package__, [
        ('inbox_list', 'InboxListTool', None),
        ('inbox_act', 'InboxActTool', None),
    ])
