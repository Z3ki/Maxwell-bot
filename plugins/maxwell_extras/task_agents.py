"""User-owned worker profiles, separate from optional conversation history."""

from __future__ import annotations

import asyncio
import json
from contextlib import closing, contextmanager
import re
import sqlite3
import time
from pathlib import Path
from typing import ClassVar

from privacy import owned_write_allowed, request_started_at, retention_days
from tools import Tool
from .task_workers import has_premium, WORKER_TOOLS, _FAMILIES

_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
KINDS = ("shell", "website", "research", "general", "custom")


class TaskAgentStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.root = self.path.parent.parent.parent
        with self.db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS agents (user_id TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL, instructions TEXT NOT NULL, updated_at REAL NOT NULL, PRIMARY KEY(user_id,name))"
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(agents)")}
            if "tools" not in columns:
                db.execute(
                    "ALTER TABLE agents ADD COLUMN tools TEXT NOT NULL DEFAULT '[]'"
                )
        self.path.chmod(0o600)

    @contextmanager
    def db(self):
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA secure_delete=ON")
            with db:
                yield db

    @staticmethod
    def identity(uid, name=None):
        uid = str(uid)
        if not uid.isascii() or not uid.isdecimal():
            raise ValueError("Authenticated user ID required")
        if name is not None and not _NAME.fullmatch(str(name)):
            raise ValueError(
                "Agent ID must use 1–32 lowercase letters, digits, underscores or hyphens"
            )
        return uid

    @staticmethod
    def cutoff():
        return (
            time.time()
            - retention_days("MAXWELL_AGENT_PROFILE_RETENTION_DAYS", 90) * 86400
        )

    def list(self, uid):
        uid = self.identity(uid)
        with self.db() as db:
            rows = [
                dict(row)
                for row in db.execute(
                    "SELECT name,kind,instructions,updated_at,tools FROM agents WHERE user_id=? AND updated_at>=? ORDER BY name",
                    (uid, self.cutoff()),
                )
            ]
            for row in rows:
                row["tools"] = json.loads(row["tools"]) or sorted(
                    {"shell", "create_site", "edit_site", "list_sites", "site_server", "web_search", "fetch_url"} if row["kind"] == "custom" else _FAMILIES[row["kind"]]
                )
            return rows

    def get(self, uid, name):
        self.identity(uid, name)
        return next((row for row in self.list(uid) if row["name"] == name), None)

    def save(self, uid, name, kind, instructions, *, tools=None, started_at=None):
        uid = self.identity(uid, name)
        if name is None:
            raise ValueError("Agent ID is required")
        if (
            kind not in KINDS
            or not isinstance(instructions, str)
            or not 1 <= len(instructions.strip()) <= 4000
        ):
            raise ValueError(
                "Choose shell, website, research, general or custom and 1–4000 characters of instructions"
            )
        if tools is not None and (
            not isinstance(tools, list)
            or not tools
            or any(not isinstance(t, str) or t not in _FAMILIES[kind] for t in tools)
        ):
            raise ValueError(
                "Agent tools must be a nonempty list within the selected scope"
            )
        tools = sorted(set(tools if tools is not None else _FAMILIES[kind]))
        started_at = request_started_at() if started_at is None else started_at
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if not owned_write_allowed(self.root, uid, started_at):
                raise ValueError(
                    "Request predates privacy deletion; start a new request"
                )
            db.execute("DELETE FROM agents WHERE updated_at<?", (self.cutoff(),))
            count = db.execute(
                "SELECT COUNT(*) FROM agents WHERE user_id=? AND name!=?", (uid, name)
            ).fetchone()[0]
            if count >= 10:
                raise ValueError("At most ten custom agents per user; delete one first")
            db.execute(
                "INSERT INTO agents(user_id,name,kind,instructions,updated_at,tools) VALUES(?,?,?,?,?,?) ON CONFLICT(user_id,name) DO UPDATE SET kind=excluded.kind,instructions=excluded.instructions,updated_at=excluded.updated_at,tools=excluded.tools",
                (uid, name, kind, instructions.strip(), time.time(), json.dumps(tools)),
            )

    def delete(self, uid, name=None):
        uid = self.identity(uid, name)
        with self.db() as db:
            return db.execute(
                "DELETE FROM agents WHERE user_id=?" + (" AND name=?" if name else ""),
                (uid, name) if name else (uid,),
            ).rowcount

    def prune(self):
        with self.db() as db:
            return db.execute(
                "DELETE FROM agents WHERE updated_at<?", (self.cutoff(),)
            ).rowcount


class ManageTaskAgentTool(Tool):
    tool_name = "manage_task_agent"
    returns_result = True
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "save", "delete"]},
            "name": {"type": "string", "pattern": _NAME.pattern},
            "kind": {"type": "string", "enum": list(KINDS)},
            "instructions": {"type": "string", "maxLength": 4000},
            "tools": {
                "type": "array",
                "minItems": 1,
                "maxItems": len(WORKER_TOOLS),
                "uniqueItems": True,
                "items": {"type": "string", "enum": sorted(WORKER_TOOLS)},
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    }

    def get_description(self):
        return (
            "Manage only the requester's custom task agents. list reviews their profiles; "
            "save (Premium) stores name, shell/website/research/custom scope, instructions and optional tool permissions only "
            "when asked to save/edit an agent; Maxwell can choose and update tool permissions within the caller’s available tools. delete removes a named profile. "
            "Profiles are optional saved settings, expire 90 days after editing by default, "
            "and are included in Delete my data. Never store credentials. Run a profile "
            "through delegate_task with profile=<name> and the concrete task. Profiles "
            "cannot grant extra tools, permissions, background jobs or separate credentials."
        )

    async def execute(
        self,
        message,
        action="list",
        name=None,
        kind=None,
        instructions=None,
        tools=None,
        **kwargs,
    ):
        uid = str(getattr(getattr(message, "author", None), "id", ""))
        store = getattr(self.bot, "_task_agents", None)
        if store is None:
            return "Error: custom agent storage unavailable"
        if action == "list":
            return await asyncio.to_thread(store.list, uid)
        if action == "delete" and name:
            return {"deleted": await asyncio.to_thread(store.delete, uid, name)}
        if action != "save" or not await asyncio.to_thread(has_premium, self.bot, uid):
            return "refused: saving a custom agent requires Premium and a valid action"
        created = getattr(message, "created_at", None)
        try:
            await asyncio.to_thread(
                store.save,
                uid,
                name,
                kind,
                instructions,
                tools=tools,
                started_at=created.timestamp() if created else request_started_at(),
            )
        except ValueError as exc:
            return f"Error: {exc}"
        return f"Saved custom agent {name}. It uses your selected subagent AI connection and existing permissions."
