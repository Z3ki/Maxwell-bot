"""Base Tool class for Maxwell Bot"""

from abc import ABC, abstractmethod
from typing import Any, ClassVar

from discord import Message


class Tool(ABC):
    """Base class for bot tools.

    A tool is self-describing. The live instance is the source of truth for
    schema, result contract, permissions, and dispatch. Static catalogs are
    derived from registered instances.
    """

    # Destructive tools are blocked when the current turn is "tainted" by
    # fetched/web content. A fresh user message starts a clean turn; there is
    # no manual confirmation command. Default off for harmless read tools.
    is_destructive: bool = False
    returns_result: bool = False
    ends_turn: bool = False
    requires_admin: bool = False
    required_capabilities: ClassVar[tuple[str, ...]] = ()
    required_discord_permissions: ClassVar[tuple[str, ...]] = ()
    transports: ClassVar[tuple[str, ...]] = ("any",)
    timeout_seconds: float | None = None
    side_effects: bool = True
    produces_visible_output: bool = False
    parameters: ClassVar[dict[str, Any] | None] = None
    tool_name: ClassVar[str | None] = None

    def __init__(self, bot):
        self.bot = bot
        self.name = self.tool_name or self.__class__.__name__

    def get_name(self) -> str:
        return str(self.tool_name or self.name or self.__class__.__name__)

    def get_parameters(self) -> dict[str, Any]:
        declared = getattr(self, "parameters", None)
        if isinstance(declared, dict) and declared.get("type") == "object":
            return declared
        try:
            from tool_schemas import TOOL_PARAMETERS

            schema = TOOL_PARAMETERS.get(self.get_name())
            if isinstance(schema, dict):
                return schema
        except Exception:
            pass
        return {"type": "object", "properties": {}, "additionalProperties": True}

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
