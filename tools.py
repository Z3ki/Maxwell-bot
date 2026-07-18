"""Base Tool class for Maxwell Bot"""

from abc import ABC, abstractmethod
from typing import Any

from discord import Message


class Tool(ABC):
    """Base class for bot tools"""

    # Tools flagged destructive require user confirmation when the current
    # message context is "tainted" (e.g. just received content from
    # fetch_url / web_search). This is the second line of defense against
    # indirect prompt injection: even if a malicious page tricks the model
    # into proposing a shell command, the user has to click Confirm before
    # it runs. Default off for harmless read tools.
    is_destructive: bool = False

    def __init__(self, bot):
        self.bot = bot
        self.name = self.__class__.__name__

    @abstractmethod
    def get_description(self) -> str:
        pass

    @abstractmethod
    async def execute(self, message: Message, **kwargs) -> Any:
        pass

    def _signal_streaming(self) -> None:
        """Notify the live progress message that this tool is about to post
        its own output (shell stdout, an image, a poll, an attachment).

        Tools that call message.channel.send() mid-execution should call this
        right before the first send. The progress message in the channel
        (managed by tool_progress.ToolProgress) will then delete itself so
        the user doesn't see two parallel "working on it" + tool output
        streams.

        No-op when:
          - The control flag progress_messages is off (no progress object)
          - A sibling tool in the same batch is still running and the
            progress message is shared — the deletion only happens if this
            is the sole non-terminal tool in the batch. Either way the
            progress message is cleaned up at stop(), so a missed signal
            is just a few hundred ms of overlap, not a stuck message.
          - Tests / standalone use (no bot attached)

        The base class helper is preferred over a free function so tools
        can grep for it: `grep -n _signal_streaming bot_tools.py` shows
        every tool that streams its own output.
        """
        progress = getattr(getattr(self, "bot", None), "_current_progress", None)
        if progress is not None:
            # The progress object swallows its own errors; this is safe
            # to call from a hot loop.
            progress.notify_streaming()
