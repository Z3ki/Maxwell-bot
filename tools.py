"""Base Tool class for Maxwell Bot"""

from abc import ABC, abstractmethod
from typing import Any

from discord import Message


class Tool(ABC):
    """Base class for bot tools"""

    # Destructive tools are blocked when the current turn is "tainted" by
    # fetched/web content. A fresh user message starts a clean turn; there is
    # no manual confirmation command. Default off for harmless read tools.
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

    def _get_channel_progress(self, message: Any = None) -> Any:
        bot = getattr(self, "bot", None)
        if bot is None:
            return None
        per_chan = getattr(bot, "_current_progress_by_channel", None)
        if not per_chan:
            return None
        if message is not None:
            chan_id = str(getattr(getattr(message, "channel", None), "id", ""))
            if chan_id:
                return per_chan.get(chan_id)
        if len(per_chan) == 1:
            return next(iter(per_chan.values()))
        return None

    def _signal_streaming(self, message: Any = None) -> None:
        """Notify the live progress message that this tool is about to post"""
        progress = self._get_channel_progress(message)
        if progress is not None:
            progress.notify_streaming()
