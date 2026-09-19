"""Memory service protocol.

The SQLite/RAG implementation stays in ``rag_memory.py``. The core and
plugins should depend on this protocol rather than a concrete database.
"""

from __future__ import annotations

from typing import Any, Protocol


class MemoryService(Protocol):
    async def get_channel_memory(self, channel_id: str) -> list[dict[str, Any]]:
        ...

    async def add_to_channel_memory(
        self, channel_id: str, message: dict[str, Any]
    ) -> None:
        ...

    def get_server_prompt(self, server_id: str) -> str | None:
        ...

    def set_server_prompt(self, server_id: str, prompt: str) -> None:
        ...

    def plugin_namespace(self, plugin_id: str) -> str:
        """Return the storage namespace this plugin owns. Never delete on disable."""
        return f"plugin:{plugin_id}"
