"""Memory service protocol.

The live SQLite history implementation is ``conversation_memory.py``. The core and
plugins should depend on this protocol rather than a concrete database.
"""

from __future__ import annotations

from typing import Any, Protocol

from .scope import MemoryRequester


class MemoryService(Protocol):
    async def get_channel_memory(
        self, channel_id: str, *, requester: MemoryRequester | None = None,
    ) -> list[dict[str, Any]]:
        ...

    async def get_relevant_channel_memory(
        self, channel_id: str, query: str, *, requester: MemoryRequester | None = None,
        exclude_ids=(), limit: int = 4,
    ) -> list[dict[str, Any]]:
        ...

    async def add_to_channel_memory(
        self, channel_id: str, message: dict[str, Any]
    ) -> None:
        ...

    def plugin_namespace(self, plugin_id: str) -> str:
        """Return the storage namespace this plugin owns. Never delete on disable."""
        return f"plugin:{plugin_id}"
