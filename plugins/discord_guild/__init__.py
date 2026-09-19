"""Register Discord Servers tools with Maxwell."""
from __future__ import annotations

from typing import Any


def _flag(bot: Any, name: str | None) -> bool:
    if not name:
        return True
    cfg = getattr(bot, "config", None)
    if cfg is None:
        return True
    return bool(getattr(cfg, name, True))


def setup(bot, ctx=None):
    pass
    from .impl import LeaveServerTool, LookupUserTool, ListServersTool, ListAdminServersTool, ListChannelsTool, ListRolesTool, ListMembersTool, CreateCategoryTool, CreateChannelTool, EditChannelTool, DeleteChannelTool, EditCategoryTool, MoveChannelTool, CloneChannelTool, SyncChannelTool, ListPermissionsTool, ManageInvitesTool, EditServerTool, AuditLogTool, ManageEmojiTool

    mapping = [
        ('leave_server', LeaveServerTool, None),
        ('lookup_user', LookupUserTool, None),
        ('list_servers', ListServersTool, None),
        ('list_admin_servers', ListAdminServersTool, None),
        ('list_channels', ListChannelsTool, None),
        ('list_roles', ListRolesTool, None),
        ('list_members', ListMembersTool, None),
        ('create_category', CreateCategoryTool, None),
        ('create_channel', CreateChannelTool, None),
        ('edit_channel', EditChannelTool, None),
        ('delete_channel', DeleteChannelTool, None),
        ('edit_category', EditCategoryTool, None),
        ('move_channel', MoveChannelTool, None),
        ('clone_channel', CloneChannelTool, None),
        ('sync_channel', SyncChannelTool, None),
        ('list_permissions', ListPermissionsTool, None),
        ('manage_invites', ManageInvitesTool, None),
        ('edit_server', EditServerTool, None),
        ('audit_log', AuditLogTool, None),
        ('manage_emoji', ManageEmojiTool, None),
    ]
    tools = []
    for runtime_name, cls, enable_name in mapping:
        if not _flag(bot, enable_name):
            continue
        inst = cls(bot)
        inst.name = runtime_name
        inst.tool_name = runtime_name
        tools.append(inst)
    return tools
