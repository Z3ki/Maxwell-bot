"""Typed event dispatch for cron, webhooks, and Discord hooks."""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping

@dataclass(frozen=True)
class Event:
    kind: str
    payload: Mapping[str, Any]
    source: str
    event_id: str = ""
    occurred_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

Handler = Callable[[Event], Awaitable[Any]]

class EventDispatcher:
    def __init__(self): self._handlers: dict[str, list[Handler]] = {}
    def subscribe(self, kind: str, handler: Handler) -> None:
        self._handlers.setdefault(kind, []).append(handler)
    async def dispatch(self, event: Event) -> list[Any]:
        handlers = self._handlers.get(event.kind, []) + self._handlers.get("*", [])
        return [await handler(event) for handler in handlers]
    async def dispatch_payload(self, kind: str, payload: Mapping[str, Any], *, source: str, event_id="") -> list[Any]:
        return await self.dispatch(Event(kind, dict(payload), source, event_id))

def event_context(event: Event) -> dict[str, Any]:
    """Safe, typed prompt context; never merge event fields into instructions."""
    return {"event": {"kind": event.kind, "source": event.source, "id": event.event_id, "occurred_at": event.occurred_at.isoformat(), "payload": dict(event.payload)}}
