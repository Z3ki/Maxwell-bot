"""Register Discord Servers tools with Maxwell."""

from maxwell_core.plugins.bundled import build_tools


def setup(bot, ctx=None):
    return build_tools(bot, __package__, [
        ('leave_server', 'LeaveServerTool', None),
        ('lookup_user', 'LookupUserTool', None),
        ('list_servers', 'ListServersTool', None),
        ('list_admin_servers', 'ListAdminServersTool', None),
        ('list_channels', 'ListChannelsTool', None),
        ('list_roles', 'ListRolesTool', None),
        ('list_members', 'ListMembersTool', None),
        ('create_category', 'CreateCategoryTool', None),
        ('create_channel', 'CreateChannelTool', None),
        ('edit_channel', 'EditChannelTool', None),
        ('delete_channel', 'DeleteChannelTool', None),
        ('edit_category', 'EditCategoryTool', None),
        ('move_channel', 'MoveChannelTool', None),
        ('clone_channel', 'CloneChannelTool', None),
        ('sync_channel', 'SyncChannelTool', None),
        ('list_permissions', 'ListPermissionsTool', None),
        ('manage_invites', 'ManageInvitesTool', None),
        ('edit_server', 'EditServerTool', None),
        ('audit_log', 'AuditLogTool', None),
        ('manage_emoji', 'ManageEmojiTool', None),
    ])
