"""Maxwell's notification inbox.

Friend requests and other actionable events land here. The planner sees
unread items only in the volatile context tail — never in the cached
system prefix.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Any

import discord

from utils import (
    _atomic_json_write_sync,
    _load_json_safe,
    _utcnow_iso,
)

logger = logging.getLogger(__name__)

INBOX_RING_SIZE = 200
INBOX_PLANNER_BUDGET = 500
ACTIONABLE_STATES = frozenset({"unread", "read"})

try:
    from discord.enums import RelationshipType
except Exception:  # pragma: no cover - discord.py-self always has this
    RelationshipType = None


def _rel_type_name(rel: Any) -> str:
    typ = getattr(rel, "type", None)
    return str(getattr(typ, "name", typ) or "")


def _is_incoming_request(rel: Any) -> bool:
    typ = getattr(rel, "type", None)
    if RelationshipType is not None and typ is RelationshipType.incoming_request:
        return True
    return _rel_type_name(rel) == "incoming_request"


def _is_friend(rel: Any) -> bool:
    typ = getattr(rel, "type", None)
    if RelationshipType is not None and typ is RelationshipType.friend:
        return True
    return _rel_type_name(rel) == "friend"


def friend_item_id(user_id: str) -> str:
    return f"friend_{str(user_id).strip()}"


class InboxStore:
    """JSON ring of inbox items. One lock around every read-modify-write."""

    def __init__(self, data_dir: str):
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / "inbox.json"
        self._lock = asyncio.Lock()

    async def load_items(self) -> list[dict]:
        async with self._lock:
            return await self._load_unlocked()

    async def _load_unlocked(self) -> list[dict]:
        data = await asyncio.to_thread(_load_json_safe, self.path, dict)
        items = data.get("items", []) if isinstance(data, dict) else []
        return items if isinstance(items, list) else []

    async def _save_unlocked(self, items: list[dict]) -> None:
        await asyncio.to_thread(
            _atomic_json_write_sync,
            self.path,
            {"items": items[-INBOX_RING_SIZE:]},
        )

    def actionable(self, items: list[dict] | None = None) -> list[dict]:
        rows = items if items is not None else []
        return [
            i
            for i in rows
            if isinstance(i, dict) and i.get("state") in ACTIONABLE_STATES
        ]

    def render_planner(self, items: list[dict]) -> str:
        """Volatile tail only. Empty → '' so the cached prefix never moves."""
        pending = self.actionable(items)
        if not pending:
            return ""
        lines = ["=== INBOX (unread / actionable — you may ignore) ==="]
        for item in pending[:12]:
            iid = str(item.get("id") or "")
            kind = str(item.get("kind") or "notice")
            actor = str(item.get("actor_name") or "?")
            aid = str(item.get("actor_id") or "")
            who = f"{actor}({aid})" if aid else actor
            acts = ",".join(str(a) for a in (item.get("actions") or [])[:4])
            summary = str(item.get("summary") or "")[:160]
            lines.append(f"- [{iid}] {kind} {who}: {summary} [{acts}]")
        text = "\n".join(lines)
        if len(text) > INBOX_PLANNER_BUDGET:
            text = text[: INBOX_PLANNER_BUDGET - 20] + "\n… (inbox truncated)"
        return text

    async def upsert(self, item: dict) -> dict:
        iid = str(item.get("id") or f"inb_{uuid.uuid4().hex[:8]}")
        now = _utcnow_iso()
        async with self._lock:
            items = await self._load_unlocked()
            existing = None
            for row in items:
                if isinstance(row, dict) and str(row.get("id") or "") == iid:
                    existing = row
                    break
            if existing is None:
                row = {
                    "id": iid,
                    "kind": str(item.get("kind") or "notice"),
                    "state": str(item.get("state") or "unread"),
                    "created_at": now,
                    "updated_at": now,
                    "actor_id": str(item.get("actor_id") or ""),
                    "actor_name": str(item.get("actor_name") or ""),
                    "summary": str(item.get("summary") or "")[:400],
                    "actions": list(item.get("actions") or ["dismiss"]),
                    "payload": item.get("payload")
                    if isinstance(item.get("payload"), dict)
                    else {},
                }
                items.append(row)
            else:
                for key in (
                    "kind",
                    "state",
                    "actor_id",
                    "actor_name",
                    "summary",
                    "actions",
                    "payload",
                ):
                    if key in item and item[key] is not None:
                        existing[key] = item[key]
                existing["updated_at"] = now
                row = existing
            await self._save_unlocked(items)
            return row

    async def get(self, item_id: str) -> dict | None:
        iid = str(item_id or "").strip()
        if not iid:
            return None
        items = await self.load_items()
        for row in items:
            if isinstance(row, dict) and str(row.get("id") or "") == iid:
                return row
        return None

    async def mark(self, item_id: str, state: str, *, note: str = "") -> dict | None:
        iid = str(item_id or "").strip()
        async with self._lock:
            items = await self._load_unlocked()
            found = None
            for row in items:
                if isinstance(row, dict) and str(row.get("id") or "") == iid:
                    row["state"] = str(state)
                    row["updated_at"] = _utcnow_iso()
                    if note:
                        payload = row.get("payload")
                        if not isinstance(payload, dict):
                            payload = {}
                        payload["note"] = str(note)[:300]
                        row["payload"] = payload
                    found = row
                    break
            if found is not None:
                await self._save_unlocked(items)
            return found

    async def ingest_relationship(
        self, relationship: Any, *, event: str, before: Any = None
    ) -> dict | None:
        user = getattr(relationship, "user", None)
        uid = str(getattr(user, "id", "") or "")
        if not uid:
            return None
        name = (
            getattr(user, "display_name", None)
            or getattr(user, "name", None)
            or uid
        )
        iid = friend_item_id(uid)
        if event == "remove":
            if _is_incoming_request(relationship) or (
                before is not None and _is_incoming_request(before)
            ):
                return await self.mark(iid, "dismissed", note="request withdrawn")
            return None
        if _is_friend(relationship) and event in {"update", "add"}:
            existing = await self.get(iid)
            if existing and existing.get("state") in ACTIONABLE_STATES:
                return await self.mark(iid, "acted", note="now friends")
            return None
        if not _is_incoming_request(relationship):
            return None
        if event == "seed":
            existing = await self.get(iid)
            if existing:
                return existing
        return await self.upsert(
            {
                "id": iid,
                "kind": "friend_request",
                "state": "unread",
                "actor_id": uid,
                "actor_name": str(name),
                "summary": f"{name} sent a friend request",
                "actions": ["accept", "decline"],
                "payload": {"relationship": _rel_type_name(relationship)},
            }
        )

    async def seed_from_bot(self, bot: Any) -> int:
        """Upsert current incoming requests so offline arrivals aren't lost."""
        added = 0
        for rel in getattr(bot, "relationships", None) or []:
            row = await self.ingest_relationship(rel, event="seed")
            if row and row.get("state") in ACTIONABLE_STATES:
                added += 1
        if added:
            logger.info("Inbox seeded %s incoming friend request(s)", added)
        return added

    async def add_notice(
        self,
        *,
        kind: str,
        summary: str,
        actor_id: str = "",
        actor_name: str = "",
        actions: list[str] | None = None,
        item_id: str = "",
        payload: dict | None = None,
    ) -> dict:
        return await self.upsert(
            {
                "id": item_id or f"inb_{uuid.uuid4().hex[:8]}",
                "kind": kind,
                "state": "unread",
                "actor_id": actor_id,
                "actor_name": actor_name,
                "summary": summary,
                "actions": actions or ["dismiss"],
                "payload": payload or {},
            }
        )


async def apply_inbox_action(
    bot: Any,
    *,
    action: str,
    item_id: str = "",
    user_id: str = "",
) -> str:
    """Accept / decline / dismiss. Shared by the tool and the command queue."""
    store = getattr(bot, "inbox", None)
    if store is None:
        return "Error: inbox is not available"
    action = str(action or "").strip().lower()
    if action not in {"accept", "decline", "dismiss"}:
        return "Error: action must be accept, decline, or dismiss"

    item = None
    if item_id:
        item = await store.get(item_id)
    if item is None and user_id:
        item = await store.get(friend_item_id(user_id))
    if item is None and user_id:
        item = {
            "id": friend_item_id(user_id),
            "kind": "friend_request",
            "actor_id": str(user_id),
            "actions": ["accept", "decline"],
        }

    if item is None:
        return "Error: inbox item not found"

    allowed = {str(a).lower() for a in (item.get("actions") or [])}
    kind = str(item.get("kind") or "")
    if action == "dismiss":
        await store.mark(str(item.get("id")), "dismissed")
        return f"Dismissed {item.get('id')}"
    if action not in allowed and kind != "friend_request":
        return f"Error: {action} is not valid for this item"

    uid = str(item.get("actor_id") or user_id or "").strip()
    if not uid.isdigit():
        return "Error: no user id on this item"
    rel = None
    getter = getattr(bot, "get_relationship", None)
    if callable(getter):
        rel = getter(int(uid))
    if rel is None:
        return f"Error: no relationship with {uid} (they may have cancelled)"

    try:
        if action == "accept":
            try:
                await rel.accept()
            except discord.HTTPException as exc:
                text = str(exc).lower()
                if "stranger" in text or "confirm" in text:
                    await rel.accept(confirm_stranger_request=True)
                else:
                    raise
            await store.mark(str(item.get("id")), "acted", note="accepted")
            return f"Accepted friend request from {item.get('actor_name') or uid}"
        if action == "decline":
            await rel.delete()
            await store.mark(str(item.get("id")), "acted", note="declined")
            return f"Declined friend request from {item.get('actor_name') or uid}"
    except discord.HTTPException as exc:
        return f"Error: Discord rejected {action}: {exc}"
    except Exception as exc:
        return f"Error: {exc}"
    return f"Error: unhandled action {action}"
