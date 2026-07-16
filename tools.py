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
