"""Register Discord Moderation tools with Maxwell."""

from maxwell_core.plugins.bundled import build_tools


def setup(bot, ctx=None):
    return build_tools(bot, __package__, [
        ('kick_member', 'KickMemberTool', None),
        ('ban_member', 'BanMemberTool', None),
        ('unban_member', 'UnbanMemberTool', None),
        ('softban_member', 'SoftbanMemberTool', None),
        ('list_bans', 'ListBansTool', None),
        ('timeout_member', 'TimeoutMemberTool', None),
        ('list_timeouts', 'ListTimeoutsTool', None),
        ('manage_role', 'ManageRoleTool', None),
        ('voice_mod', 'VoiceModTool', None),
        ('lock_channel', 'LockChannelTool', None),
        ('lockdown', 'LockdownTool', None),
        ('set_channel_permissions', 'SetChannelPermissionsTool', None),
        ('set_member_nickname', 'SetMemberNicknameTool', None),
    ])
