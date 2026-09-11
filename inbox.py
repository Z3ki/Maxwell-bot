"""Maxwell's persistent notification inbox.

The inbox stores notices from supported bot-side integrations. Older releases
also mirrored Discord user relationship/friend-request events here; official
Discord bots cannot access those relationships, so that path is intentionally
gone.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from utils import _atomic_json_write_sync, _load_json_safe, _utcnow_iso

INBOX_RING_SIZE = 200
INBOX_PLANNER_BUDGET = 900
ACTIONABLE_STATES = frozenset({"unread", "read"})


def needs_decision(item: dict) -> bool:
    """Whether this row still needs an external decision.

    Retained because bot.py uses the hook when marking announced rows. The
    official-bot inbox currently contains notices only; Discord relationship
    requests are not available to bot accounts.
    """
    return False


KIND_PRIORITY = {"email": 0}
KIND_PRIORITY_DEFAULT = 1
KIND_RENDER_CAP = {"email": 6}
KIND_RENDER_CAP_DEFAULT = 12


class InboxStore:
    """JSON ring of inbox items. One lock protects each read-modify-write."""

    def __init__(self, data_dir: str):
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / "inbox.json"
        self._lock = asyncio.Lock()

    async def load_items(self) -> list[dict]:
        async with self._lock:
            return await self._load_unlocked()

    async def _load_unlocked(self, *, for_update: bool = False) -> list[dict]:
        data = await asyncio.to_thread(_load_json_safe, self.path, lambda: None)
        if data is None and not self.path.exists():
            return []
        items = data.get("items", []) if isinstance(data, dict) else []
        if for_update and (not isinstance(data, dict) or not isinstance(items, list)):
            raise ValueError("inbox.json is corrupt; refusing to overwrite it")
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
            row
            for row in rows
            if isinstance(row, dict) and row.get("state") in ACTIONABLE_STATES
        ]

    @staticmethod
    def _planner_sort_key(item: dict) -> tuple:
        kind = str(item.get("kind") or "notice")
        unread = 0 if item.get("state") == "unread" else 1
        return (unread, KIND_PRIORITY.get(kind, KIND_PRIORITY_DEFAULT))

    def planner_items(
        self, items: list[dict], *, exclude_announced: bool = False
    ) -> list[dict]:
        """Return actionable rows in prompt order.

        ``exclude_announced`` removes rows already marked read from the prompt
        tail while leaving them visible to ``inbox_list``.
        """
        rows = [
            row
            for row in self.actionable(items)
            if not exclude_announced or row.get("state") != "read"
        ]
        pending = sorted(
            rows,
            key=lambda row: str(row.get("created_at") or ""),
            reverse=True,
        )
        pending.sort(key=self._planner_sort_key)

        seen: dict[str, int] = {}
        out: list[dict] = []
        for item in pending:
            kind = str(item.get("kind") or "notice")
            cap = KIND_RENDER_CAP.get(kind, KIND_RENDER_CAP_DEFAULT)
            count = seen.get(kind, 0)
            if count >= cap:
                continue
            seen[kind] = count + 1
            out.append(item)
        return out

    @staticmethod
    def render_item(item: dict, *, summary_chars: int = 160) -> str:
        iid = str(item.get("id") or "")
        kind = str(item.get("kind") or "notice")
        acts = ",".join(str(action) for action in (item.get("actions") or [])[:4])
        summary = str(item.get("summary") or "")[:summary_chars]
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        actor = str(item.get("actor_name") or "?")
        actor_id = str(item.get("actor_id") or "")

        if kind == "email":
            who = f"{actor} <{actor_id}>" if actor_id else actor
            subject = str(payload.get("subject") or "").strip() or "(no subject)"
            body = f'{who} — "{subject}"'
            snippet = " ".join(str(payload.get("snippet") or "").split())
            if snippet:
                body += f": {snippet[:summary_chars]}"
        else:
            who = f"{actor}({actor_id})" if actor_id else actor
            body = f"{who}: {summary}"
        return f"- [{iid}] {kind} {body} [{acts}]"

    def render_planner(self, items: list[dict]) -> str:
        pending = self.planner_items(items, exclude_announced=True)
        if not pending:
            return ""
        lines = ["=== INBOX (unread / actionable — you may ignore) ==="]
        lines.extend(self.render_item(item) for item in pending[:14])
        if len(pending) > 14:
            lines.append(f"… and {len(pending) - 14} more (inbox_list to see them)")
        text = "\n".join(lines)
        if len(text) > INBOX_PLANNER_BUDGET:
            text = text[: INBOX_PLANNER_BUDGET - 20] + "\n… (inbox truncated)"
        return text

    async def upsert(self, item: dict) -> dict:
        iid = str(item.get("id") or f"inb_{uuid.uuid4().hex[:8]}")
        now = _utcnow_iso()
        async with self._lock:
            items = await self._load_unlocked(for_update=True)
            existing = next(
                (
                    row
                    for row in items
                    if isinstance(row, dict) and str(row.get("id") or "") == iid
                ),
                None,
            )
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

    async def insert_if_absent(self, item: dict) -> dict | None:
        """Insert a notice once so polling cannot resurrect a dismissed item."""
        iid = str(item.get("id") or "").strip()
        if not iid:
            raise ValueError("insert_if_absent needs an explicit id")
        now = _utcnow_iso()
        async with self._lock:
            items = await self._load_unlocked(for_update=True)
            if any(
                isinstance(row, dict) and str(row.get("id") or "") == iid
                for row in items
            ):
                return None
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
            await self._save_unlocked(items)
            return row

    async def get(self, item_id: str) -> dict | None:
        iid = str(item_id or "").strip()
        if not iid:
            return None
        for row in await self.load_items():
            if isinstance(row, dict) and str(row.get("id") or "") == iid:
                return row
        return None

    async def mark(self, item_id: str, state: str, *, note: str = "") -> dict | None:
        iid = str(item_id or "").strip()
        async with self._lock:
            items = await self._load_unlocked(for_update=True)
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
    bot,
    *,
    action: str,
    item_id: str = "",
    user_id: str = "",
) -> str:
    """Read or dismiss a notice.

    Old ``accept``/``decline`` calls get an explicit migration error instead of
    reaching user-account-only Discord relationship APIs.
    """
    store = getattr(bot, "inbox", None)
    if store is None:
        return "Error: inbox is not available"

    action = str(action or "").strip().lower()
    if action in {"accept", "decline"}:
        return (
            "Error: Discord friend-request actions were removed when Maxwell "
            "moved to an official bot account; official bots cannot accept or "
            "decline user relationships."
        )
    if action not in {"dismiss", "read"}:
        return "Error: action must be dismiss or read"

    iid = str(item_id or "").strip()
    if not iid:
        if str(user_id or "").strip():
            return (
                "Error: user_id inbox actions were removed with self-bot friend "
                "requests; use an inbox item_id."
            )
        return "Error: inbox item not found"

    item = await store.get(iid)
    if item is None:
        return "Error: inbox item not found"

    if action == "dismiss":
        await store.mark(iid, "dismissed")
        return f"Dismissed {iid}"

    await store.mark(iid, "read")
    return f"Marked {iid} read"
