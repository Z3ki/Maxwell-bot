"""MaxwellBot split into single-responsibility mixins.

bot.py composes them back into one class; method order and
bodies are unchanged.
"""

from bot_parts.core import CoreMixin
from bot_parts.typing_presence import TypingPresenceMixin
from bot_parts.ai import AIMixin
from bot_parts.memory import MemoryMixin
from bot_parts.requests import RequestPipelineMixin
from bot_parts.addressing import AddressingMixin
from bot_parts.reactions import ReactionsMixin
from bot_parts.watch import WatchMixin
from bot_parts.gateway import GatewayEventsMixin
from bot_parts.recovery import RecoveryMixin
from bot_parts.guild_events import GuildEventsMixin
from bot_parts.message_updates import MessageUpdatesMixin
from bot_parts.messages import MessagesMixin
from bot_parts.sleep import SleepMixin
from bot_parts.settings import SettingsMixin
from bot_parts.sites import SitesMixin
from bot_parts.sending import SendingMixin
from bot_parts.media import MediaMixin
from bot_parts.media_context import MediaContextMixin
from bot_parts.tool_calls import ToolCallsMixin
from bot_parts.prompting import PromptingMixin

__all__ = [
    "CoreMixin",
    "TypingPresenceMixin",
    "AIMixin",
    "MemoryMixin",
    "RequestPipelineMixin",
    "AddressingMixin",
    "ReactionsMixin",
    "WatchMixin",
    "GatewayEventsMixin",
    "RecoveryMixin",
    "GuildEventsMixin",
    "MessageUpdatesMixin",
    "MessagesMixin",
    "SleepMixin",
    "SettingsMixin",
    "SitesMixin",
    "SendingMixin",
    "MediaMixin",
    "MediaContextMixin",
    "ToolCallsMixin",
    "PromptingMixin",
]
