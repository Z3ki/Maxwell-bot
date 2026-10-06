"""Register Discord Messaging tools with Maxwell."""

from maxwell_core.plugins.bundled import build_tools


def setup(bot, ctx=None):
    return build_tools(bot, __package__, [
        ('react', 'ReactTool', None),
        ('edit_message', 'EditMessageTool', None),
        ('delete_message', 'DeleteMessageTool', None),
        ('create_poll', 'CreatePollTool', None),
        ('forward_message', 'ForwardMessageTool', None),
        ('typing', 'TypingTool', None),
        ('send_message', 'SendMessageTool', None),
        ('send_file', 'SendFileTool', None),
        ('pin_message', 'PinMessageTool', None),
        ('purge_messages', 'PurgeMessagesTool', None),
        ('search_messages', 'SearchMessagesTool', None),
        ('create_invite', 'CreateInviteTool', None),
        ('bot_invite_url', 'BotInviteUrlTool', None),
        ('no_response', 'NoResponseTool', None),
    ])
