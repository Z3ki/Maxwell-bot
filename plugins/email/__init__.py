"""Register Email tools with Maxwell."""

from maxwell_core.plugins.bundled import build_tools


def setup(bot, ctx=None):
    return build_tools(bot, __package__, [
        ('email_send', 'EmailSendTool', 'ENABLE_EMAIL_TOOLS'),
        ('email_read_inbox', 'EmailReadInboxTool', 'ENABLE_EMAIL_TOOLS'),
        ('email_get_message', 'EmailGetMessageTool', 'ENABLE_EMAIL_TOOLS'),
        ('email_search', 'EmailSearchTool', 'ENABLE_EMAIL_TOOLS'),
    ])
