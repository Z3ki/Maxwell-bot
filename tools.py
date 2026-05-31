"""Base Tool class for Maxwell Bot"""

from abc import ABC, abstractmethod
from typing import Any

from discord import Message


class Tool(ABC):
    """Base class for bot tools"""

    def __init__(self, bot):
        self.bot = bot
        self.name = self.__class__.__name__

    @abstractmethod
    def get_description(self) -> str:
        pass

    @abstractmethod
    async def execute(self, message: Message, **kwargs) -> Any:
        pass
