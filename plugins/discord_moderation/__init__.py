"""Register Discord Moderation tools with Maxwell."""
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
    from .impl import KickMemberTool, BanMemberTool, UnbanMemberTool, SoftbanMemberTool, ListBansTool, TimeoutMemberTool, ListTimeoutsTool, ManageRoleTool, VoiceModTool, LockChannelTool, LockdownTool, SetChannelPermissionsTool, SetMemberNicknameTool

    mapping = [
        ('kick_member', KickMemberTool, None),
        ('ban_member', BanMemberTool, None),
        ('unban_member', UnbanMemberTool, None),
        ('softban_member', SoftbanMemberTool, None),
        ('list_bans', ListBansTool, None),
        ('timeout_member', TimeoutMemberTool, None),
        ('list_timeouts', ListTimeoutsTool, None),
        ('manage_role', ManageRoleTool, None),
        ('voice_mod', VoiceModTool, None),
        ('lock_channel', LockChannelTool, None),
        ('lockdown', LockdownTool, None),
        ('set_channel_permissions', SetChannelPermissionsTool, None),
        ('set_member_nickname', SetMemberNicknameTool, None),
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
