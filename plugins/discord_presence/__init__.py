"""Register Discord Presence tools with Maxwell."""

from maxwell_core.plugins.bundled import build_tools


def setup(bot, ctx=None):
    return build_tools(bot, __package__, [
        ('change_presence', 'ChangePresenceTool', None),
        ('set_activity', 'SetActivityTool', None),
        ('set_nickname', 'SetNicknameTool', None),
        ('change_avatar', 'ChangeAvatarTool', 'ENABLE_AVATAR'),
    ])
