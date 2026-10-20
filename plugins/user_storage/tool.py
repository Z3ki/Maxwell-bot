"""The model-facing side of /storage: same tree the shell sees, no sandbox needed."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
from typing import ClassVar

from tools import Tool

from .store import MB, PUBLIC_DIR, StorageError, clean_path
from .urls import public_root, public_url, signed_url

_TEXT_PREVIEW = 20000
_WRITE_LIMIT = 10 * MB


def _inside_worker() -> bool:
    try:
        from plugins.maxwell_extras.task_workers import _inside_worker as flag
    except Exception:
        return False
    return bool(flag.get())


def _size(value: int) -> str:
    if value >= MB:
        return f"{value / MB:.1f} MB"
    if value >= 1024:
        return f"{value / 1024:.0f} KB"
    return f"{value} B"


class StorageTool(Tool):
    tool_name = "storage"
    returns_result = True
    ends_turn = False
    is_destructive = False
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["status", "list", "read", "write", "mkdir", "delete", "move", "link", "set_handle"],
            },
            "path": {"type": "string", "description": "Path inside /storage, e.g. public/game/index.html"},
            "to": {"type": "string", "description": "Destination path for move"},
            "content": {"type": "string", "description": "File body for write"},
            "encoding": {"type": "string", "enum": ["text", "base64"]},
            "handle": {"type": "string", "description": "New public address name for set_handle"},
            "hours": {"type": "integer", "minimum": 1, "maximum": 720, "description": "Private link lifetime"},
        },
        "required": ["action"],
        "additionalProperties": False,
    }

    def get_description(self) -> str:
        return (
            "The requester's persistent /storage: the same files the shell sees at /storage, kept after "
            "every sandbox wipe, restart and timeout. Only the requester's own storage is reachable. "
            "status: plan, space used, public address. list/read/write/mkdir/delete/move take paths "
            "inside /storage. Files under public/ are a website at the requester's public address "
            "(public/index.html is the front page, public/cat-3d/index.html is /cat-3d/). "
            "link: the public URL for a file in public/, or an expiring private download link for any "
            "other file; anyone holding a private link can download until it expires, so post it only "
            "where the requester wants it. set_handle: rename the public address. To attach a stored "
            "file use send_file path=/storage/<path>."
        )

    def _storage(self):
        return getattr(self.bot, "_user_storage", None)

    def limits_for(self, user_id: str) -> dict:
        """Read-only storage numbers for get_configuration. Sync; called off-loop."""
        storage = self._storage()
        if storage is None or not str(user_id).isdigit():
            return {}
        status = storage.status(str(user_id))
        return {
            "storage": {
                "quota_mb": round(status["quota_bytes"] / MB, 1),
                "used_mb": round(status["used_bytes"] / MB, 2),
                "files": status["files"],
                "max_files": status["max_files"],
                "public_address": public_root(status["handle"]) if status["quota_bytes"] > 0 else "",
            }
        }

    async def execute(self, message, action: str = "status", path: str | None = None, to: str | None = None,
                      content: str | None = None, encoding: str = "text", handle: str | None = None,
                      hours: int | None = None, **kwargs) -> str:
        storage = self._storage()
        author = getattr(message, "author", None)
        uid = str(getattr(author, "id", "") or "")
        if storage is None or not uid.isdigit():
            return "Error: persistent storage is not available on this bot"
        try:
            return await self._run(storage, message, uid, str(action or "status"), path, to, content,
                                   encoding, handle, hours)
        except StorageError as exc:
            return f"Error: {exc}"

    async def _run(self, storage, message, uid, action, path, to, content, encoding, handle, hours) -> str:
        quota = await asyncio.to_thread(storage.quota, uid)
        if quota <= 0:
            return (
                "Error: persistent storage is not enabled for this user. It comes with an "
                "operator-granted Premium plan; do not mention prices or ask them to upgrade."
            )
        if action == "status":
            return await self._status(storage, message, uid)
        if action == "list":
            entries = await asyncio.to_thread(storage.list, uid, path or "", limit=300)
            if not entries:
                return "/storage is empty" if not path else "That folder is empty"
            lines = [
                f"{'dir ' if e.is_dir else 'file'} /storage/{e.path}" + ("" if e.is_dir else f" ({_size(e.size)})")
                for e in entries
            ]
            more = "\n(listing stopped at 300 entries)" if len(entries) >= 300 else ""
            return "\n".join(lines) + more
        if action == "read":
            blob = await asyncio.to_thread(storage.read, uid, self._need(path), max_size=_WRITE_LIMIT)
            try:
                if b"\x00" in blob[:8192]:
                    raise UnicodeDecodeError("utf-8", b"", 0, 1, "NUL byte")
                text = blob.decode("utf-8")
            except UnicodeDecodeError:
                return (
                    f"/storage/{clean_path(path)} is a binary file ({_size(len(blob))}). "
                    "Attach it with send_file or share it with action=link."
                )
            if len(text) > _TEXT_PREVIEW:
                return text[:_TEXT_PREVIEW] + f"\n[truncated; file is {_size(len(blob))}]"
            return text
        if action == "write":
            if content is None:
                return "Error: content is required"
            if str(encoding or "text").lower() == "base64":
                try:
                    data = base64.b64decode(content, validate=True)
                except (binascii.Error, ValueError):
                    return "Error: content is not valid base64"
            else:
                data = str(content).encode("utf-8")
            if len(data) > _WRITE_LIMIT:
                return "Error: write at most 10 MB per call; use the shell for bigger files"
            rel = clean_path(self._need(path))
            await asyncio.to_thread(storage.write, uid, rel, data)
            return f"Saved /storage/{rel} ({_size(len(data))})" + await self._public_hint(storage, message, uid, rel)
        if action == "mkdir":
            rel = clean_path(self._need(path))
            await asyncio.to_thread(storage.mkdir, uid, rel)
            return f"Created /storage/{rel}/"
        if action == "delete":
            rel = clean_path(self._need(path))
            await asyncio.to_thread(storage.delete, uid, rel)
            return f"Deleted /storage/{rel}"
        if action == "move":
            src, dst = clean_path(self._need(path)), clean_path(self._need(to, "to"))
            await asyncio.to_thread(storage.move, uid, src, dst)
            return f"Moved /storage/{src} to /storage/{dst}" + await self._public_hint(storage, message, uid, dst)
        if action == "link":
            rel = clean_path(self._need(path))
            name = await asyncio.to_thread(storage.ensure_handle, uid, self._display(message))
            is_file = await asyncio.to_thread(storage.file_path, uid, rel) is not None
            url = public_url(name, rel)
            if url and is_file:
                return f"Public URL: {url}"
            if url and await self._is_folder(storage, uid, rel):
                return f"Public URL: {url.rstrip('/')}/"
            if not is_file:
                return "Error: no file at that path"
            ttl = max(1, min(int(hours or 168), 720)) * 3600
            url, expires = await asyncio.to_thread(signed_url, storage, uid, rel, ttl=ttl)
            if not url:
                return "Error: this bot has no public web address configured"
            return f"Private download link (expires <t:{expires}:R>): {url}"
        if action == "set_handle":
            if _inside_worker():
                return "Error: the public address is an account setting; leave it to the coordinator"
            if not handle:
                return "Error: handle is required"
            value = await asyncio.to_thread(storage.set_handle, uid, handle)
            root = public_root(value)
            return f"Public address is now {root or value}"
        return "Error: unknown action"

    @staticmethod
    def _need(value: str | None, name: str = "path") -> str:
        if not value or not str(value).strip():
            raise StorageError(f"{name} is required")
        return str(value)

    @staticmethod
    def _display(message) -> str:
        author = getattr(message, "author", None)
        return str(getattr(author, "name", "") or getattr(author, "display_name", "") or "")

    @staticmethod
    async def _is_folder(storage, uid: str, rel: str) -> bool:
        try:
            await asyncio.to_thread(storage.list, uid, rel, limit=1)
        except StorageError:
            return False
        return True

    async def _public_hint(self, storage, message, uid: str, rel: str) -> str:
        if rel != PUBLIC_DIR and not rel.startswith(PUBLIC_DIR + "/"):
            return ""
        name = await asyncio.to_thread(storage.ensure_handle, uid, self._display(message))
        url = public_url(name, rel)
        return f" — live at {url}" if url else ""

    async def _status(self, storage, message, uid: str) -> str:
        status = await asyncio.to_thread(storage.status, uid)
        if not status.get("handle"):
            status["handle"] = await asyncio.to_thread(storage.ensure_handle, uid, self._display(message))
        root = public_root(status["handle"])
        return json.dumps(
            {
                "plan": status["plan"],
                "used": _size(status["used_bytes"]),
                "quota": _size(status["quota_bytes"]),
                "files": status["files"],
                "max_files": status["max_files"],
                "public_folder": "/storage/public/",
                "public_address": root or None,
                "handle": status["handle"],
            }
        )
