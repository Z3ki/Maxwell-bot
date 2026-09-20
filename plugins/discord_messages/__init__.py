"""Register Discord Messaging tools with Maxwell."""
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
    from .impl import ReactTool, EditMessageTool, DeleteMessageTool, CreatePollTool, ForwardMessageTool, TypingTool, SendMessageTool, SendFileTool, PinMessageTool, PurgeMessagesTool, SearchMessagesTool, CreateInviteTool, BotInviteUrlTool, NoResponseTool

    mapping = [
        ('react', ReactTool, None),
        ('edit_message', EditMessageTool, None),
        ('delete_message', DeleteMessageTool, None),
        ('create_poll', CreatePollTool, None),
        ('forward_message', ForwardMessageTool, None),
        ('typing', TypingTool, None),
        ('send_message', SendMessageTool, None),
        ('send_file', SendFileTool, None),
        ('pin_message', PinMessageTool, None),
        ('purge_messages', PurgeMessagesTool, None),
        ('search_messages', SearchMessagesTool, None),
        ('create_invite', CreateInviteTool, None),
        ('bot_invite_url', BotInviteUrlTool, None),
        ('no_response', NoResponseTool, None),
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
