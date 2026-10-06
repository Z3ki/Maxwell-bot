"""Register Plugin Administration tools with Maxwell."""

from maxwell_core.plugins.bundled import build_tools


def setup(bot, ctx=None):
    return build_tools(bot, __package__, [
        ('manage_plugin', 'ManagePluginTool', None),
    ])
