"""Explicit, user-controlled memory primitives with layered scopes."""
from __future__ import annotations
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any, Protocol

class MemoryBackend(Protocol):
    async def save_memory(self, record: dict[str, Any]) -> Any: ...
    async def update_memory(self, memory_id: str, record: dict[str, Any]) -> Any: ...
    async def forget_memory(self, memory_id: str) -> Any: ...

@dataclass
class MemoryRecord:
    content: str
    layer: str = "profile"  # profile, project, episodic, transcript
    user_id: str | None = None
    project_id: str | None = None
    source: str = "user"
    memory_id: str | None = None
    updated_at: str = ""

    def normalized(self) -> dict[str, Any]:
        if self.layer not in {"profile", "project", "episodic", "transcript"}:
            raise ValueError("invalid memory layer")
        if not self.content.strip():
            raise ValueError("memory content cannot be empty")
        value = asdict(self)
        value["content"] = self.content.strip()
        value["updated_at"] = self.updated_at or datetime.now(timezone.utc).isoformat()
        return value

async def save_memory(backend: MemoryBackend, content: str, *, layer="profile", user_id=None, project_id=None):
    return await backend.save_memory(MemoryRecord(content, layer, user_id, project_id).normalized())

async def update_memory(backend: MemoryBackend, memory_id: str, content: str, *, layer="profile", user_id=None, project_id=None):
    return await backend.update_memory(memory_id, MemoryRecord(content, layer, user_id, project_id, memory_id=memory_id).normalized())

async def forget_memory(backend: MemoryBackend, memory_id: str):
    if not memory_id or not memory_id.strip():
        raise ValueError("memory_id is required")
    return await backend.forget_memory(memory_id)

# Deterministic handling for the natural-language commands before model/RAG.
def parse_memory_command(text: str) -> tuple[str, str] | None:
    import re
    value = text.strip()
    match = re.match(r"^(?:remember(?: that)?|save memory)\s+(.+)$", value, re.I)
    if match: return ("save", match.group(1).strip())
    match = re.match(r"^(?:forget|delete memory)\s+(.+)$", value, re.I)
    if match: return ("forget", match.group(1).strip())
    return None
