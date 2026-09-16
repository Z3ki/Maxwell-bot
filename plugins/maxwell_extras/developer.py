"""Human-approved live plugin editing for Maxwell.

The model may inspect and propose plugin source changes, but writing them needs
an admin click or an explicit approval in a later user turn. Changes are scoped
to plugins/<name>/, syntax-checked, written atomically per file, and followed by
a plugin-registry hot reload. No shell sandbox or coding sub-agent is involved.
"""

from __future__ import annotations

import asyncio
import contextlib
import difflib
import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

import discord

from tools import Tool

_PLUGINS_ROOT = Path(__file__).resolve().parents[1]
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_ALLOWED = {".py", ".json", ".md", ".txt"}
_MAX_FILES, _MAX_FILE_CHARS, _MAX_TOTAL_CHARS = 12, 160_000, 400_000


def _is_admin(bot: Any, uid: Any) -> bool:
    fn = getattr(bot, "_is_admin", None)
    if callable(fn):
        with contextlib.suppress(Exception):
            return bool(fn(uid))
    return False


def _name(value: Any) -> str:
    value = str(value or "").strip()
    if not _NAME_RE.fullmatch(value):
        raise ValueError("plugin_name must be a Python identifier (max 64 chars)")
    return value


def _file(plugin: str, rel: Any) -> Path:
    rel = str(rel or "").strip().replace("\\", "/")
    if not rel or rel.startswith(("/", "~", ".")):
        raise ValueError(f"invalid plugin path: {rel!r}")
    parts = Path(rel).parts
    if ".." in parts or any(p.startswith(".") for p in parts):
        raise ValueError(f"path traversal/hidden path blocked: {rel!r}")
    if Path(rel).suffix.lower() not in _ALLOWED:
        raise ValueError(f"unsupported plugin file type: {rel!r}")
    root = (_PLUGINS_ROOT / plugin).resolve()
    dest = (root / rel).resolve()
    try:
        dest.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"path escapes plugin root: {rel!r}") from exc
    return dest


def _approval(text: Any) -> bool:
    text = " ".join(str(text or "").lower().split())
    if re.search(r"\b(?:do not|don't|dont|reject|decline|cancel|nope)\b", text):
        return False
    return bool(re.search(r"\b(?:accept|approve|approved|apply|yes|go ahead|do it|ship it|looks good)\b", text))


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _as_list(value: Any, label: str) -> list:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, list):
        raise TypeError(f"{label} must be an array")
    return value


def _plan(plugin: str, edits: Any, new_files: Any, delete_files: Any) -> tuple[dict, dict, str]:
    edits = _as_list(edits, "edits")
    new_files = _as_list(new_files, "new_files")
    deletes = _as_list(delete_files, "delete_files")
    if len(edits) + len(new_files) + len(deletes) > _MAX_FILES:
        raise ValueError(f"proposal may touch at most {_MAX_FILES} files")
    if not (edits or new_files or deletes):
        raise ValueError("proposal has no file changes")

    before: dict[str, str | None] = {}
    after: dict[str, str | None] = {}

    def get(rel: str) -> str | None:
        if rel in after:
            return after[rel]
        dest = _file(plugin, rel)
        if not dest.exists():
            return None
        if not dest.is_file() or dest.is_symlink():
            raise ValueError(f"not a regular plugin file: {rel}")
        text = dest.read_text("utf-8")
        if len(text) > _MAX_FILE_CHARS:
            raise ValueError(f"{rel} is too large to edit")
        return text

    for item in edits:
        if not isinstance(item, dict):
            raise TypeError("each edit must be an object")
        rel = str(item.get("path") or "").strip()
        old, new = item.get("old"), item.get("new")
        if not isinstance(old, str) or not isinstance(new, str):
            raise TypeError(f"edit {rel!r} needs string old/new")
        cur = get(rel)
        if cur is None:
            raise ValueError(f"edit target does not exist: {rel}")
        before.setdefault(rel, cur)
        if old not in cur:
            raise ValueError(f"search text not found in {rel}")
        after[rel] = cur.replace(old, new, 1)

    for item in new_files:
        if not isinstance(item, dict):
            raise TypeError("each new file must be an object")
        rel = str(item.get("path") or "").strip()
        content = item.get("content")
        if not isinstance(content, str):
            raise TypeError(f"new file {rel!r} needs string content")
        if get(rel) is not None:
            raise ValueError(f"new file already exists: {rel}")
        before.setdefault(rel, None)
        after[rel] = content

    for raw in deletes:
        rel = str(raw or "").strip()
        cur = get(rel)
        if cur is None:
            raise ValueError(f"delete target does not exist: {rel}")
        before.setdefault(rel, cur)
        after[rel] = None

    total = 0
    for rel, text in after.items():
        total += len(text or "")
        if len(text or "") > _MAX_FILE_CHARS or total > _MAX_TOTAL_CHARS:
            raise ValueError("proposal is too large")
        if rel.endswith(".py") and text is not None:
            compile(text, f"plugins/{plugin}/{rel}", "exec")
        if rel == "plugin.json" and text is not None:
            data = json.loads(text)
            if not isinstance(data, dict) or str(data.get("name") or plugin) != plugin:
                raise ValueError("plugin.json name must match plugin_name")

    diff: list[str] = []
    for rel in sorted(set(before) | set(after)):
        old, new = before.get(rel), after.get(rel)
        diff.extend(difflib.unified_diff(
            (old or "").splitlines(), (new or "").splitlines(),
            fromfile=f"a/plugins/{plugin}/{rel}" if old is not None else "/dev/null",
            tofile=f"b/plugins/{plugin}/{rel}" if new is not None else "/dev/null",
            lineterm="",
        ))
    return before, after, "\n".join(diff)


class _Store:
    def __init__(self, path: Path):
        self.path, self.lock = Path(path), asyncio.Lock()

    def read(self) -> dict:
        try:
            data = json.loads(self.path.read_text("utf-8"))
            return data if isinstance(data, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    def write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), "utf-8")
        os.replace(tmp, self.path)

    async def get(self, pid: str) -> dict | None:
        async with self.lock:
            row = self.read().get(str(pid))
            return dict(row) if isinstance(row, dict) else None

    async def put(self, row: dict) -> None:
        async with self.lock:
            data = self.read()
            data[str(row["id"])] = row
            rows = sorted(data.values(), key=lambda r: float(r.get("created_at") or 0), reverse=True)[:100]
            self.write({str(r["id"]): r for r in rows})

    async def update(self, pid: str, **changes: Any) -> dict | None:
        async with self.lock:
            data = self.read()
            row = data.get(str(pid))
            if not isinstance(row, dict):
                return None
            row.update(changes)
            data[str(pid)] = row
            self.write(data)
            return dict(row)

    async def pending(self) -> list[dict]:
        async with self.lock:
            rows = self.read().values()
            return [dict(r) for r in rows if isinstance(r, dict) and r.get("status") == "pending"]


class _ProposalView(discord.ui.View):
    def __init__(self, tool: "PluginWorkbenchTool", pid: str):
        super().__init__(timeout=86400)
        self.tool, self.pid = tool, pid
        yes = discord.ui.Button(label="Apply plugin change", style=discord.ButtonStyle.success)
        no = discord.ui.Button(label="Reject", style=discord.ButtonStyle.secondary)
        yes.callback = self._apply  # type: ignore[method-assign]
        no.callback = self._reject  # type: ignore[method-assign]
        self.add_item(yes)
        self.add_item(no)

    async def _apply(self, interaction: discord.Interaction) -> None:
        uid = getattr(getattr(interaction, "user", None), "id", None)
        if not _is_admin(self.tool.bot, uid):
            await interaction.response.send_message("Admin approval required.", ephemeral=True)
            return
        result = await self.tool.apply(self.pid, str(uid or ""), via_button=True)
        await interaction.response.edit_message(content=f"{getattr(interaction.message, 'content', '')}\n\n{result}", view=None)

    async def _reject(self, interaction: discord.Interaction) -> None:
        uid = getattr(getattr(interaction, "user", None), "id", None)
        if not _is_admin(self.tool.bot, uid):
            await interaction.response.send_message("Admin approval required.", ephemeral=True)
            return
        await self.tool.store.update(self.pid, status="rejected", decided_at=time.time(), decided_by=str(uid or ""))
        await interaction.response.edit_message(content=f"{getattr(interaction.message, 'content', '')}\n\nProposal rejected.", view=None)


class PluginWorkbenchTool(Tool):
    returns_result = True

    def __init__(self, bot: Any, ctx: Any):
        self.bot = bot
        self.store = _Store(ctx.store_path("plugin_proposals.json"))

    def get_name(self) -> str:
        return "plugin_workbench"

    def get_description(self) -> str:
        return (
            "Admin-only live plugin development. Inspect actual plugin files with list/read, "
            "then propose edits/new files/deletions. A proposal only writes after an admin "
            "clicks Apply or explicitly approves it in a later user turn. Apply edits the real "
            "plugins/ tree, validates it, and hot-reloads the registry; no shell sandbox or "
            "coding sub-agent. Use this when an admin gives Maxwell a plugin idea or asks him "
            "to fix a plugin. Never call apply in the same turn as propose."
        )

    def get_parameters(self) -> dict:
        obj = {"type": "object", "properties": {"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}}, "required": ["path", "old", "new"]}
        new_obj = {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}
        return {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["list", "read", "propose", "show", "apply", "reject"]},
            "plugin_name": {"type": "string"}, "path": {"type": "string"},
            "summary": {"type": "string"}, "proposal_id": {"type": "string"},
            "edits": {"type": "array", "items": obj},
            "new_files": {"type": "array", "items": new_obj},
            "delete_files": {"type": "array", "items": {"type": "string"}},
        }, "required": ["action"]}

    async def execute(self, message: Any, action: str, plugin_name: str | None = None, path: str | None = None, summary: str | None = None, proposal_id: str | None = None, edits: Any = None, new_files: Any = None, delete_files: Any = None, **kwargs: Any) -> str:
        uid = getattr(getattr(message, "author", None), "id", None)
        if not _is_admin(self.bot, uid):
            return "Error: plugin_workbench is admin-only."
        action = str(action or "").lower().strip()
        if action == "list":
            names = sorted(p.name for p in _PLUGINS_ROOT.iterdir() if p.is_dir() and (p / "plugin.json").is_file())
            pending = await self.store.pending()
            suffix = f"\nPending proposals: {', '.join(str(p['id']) for p in pending[:10])}" if pending else ""
            return "Installed plugins: " + (", ".join(names) if names else "none") + suffix
        if action == "read":
            try:
                dest = _file(_name(plugin_name), path)
            except Exception as exc:
                return f"Error: {exc}"
            if not dest.is_file() or dest.is_symlink():
                return "Error: plugin file not found."
            text = dest.read_text("utf-8")
            return text[:28_000] + (f"\n...[truncated {len(text)-28_000} chars]" if len(text) > 28_000 else "")
        if action in {"show", "apply", "reject"}:
            row = await self.store.get(str(proposal_id or ""))
            if not row:
                return "Error: proposal not found."
            if action == "show":
                diff = str(row.get("diff") or "")[:12_000]
                return f"Plugin proposal {row['id']} [{row['status']}]\nPlugin: {row['plugin_name']}\nSummary: {row['summary']}\n\n```diff\n{diff}\n```"
            if action == "reject":
                await self.store.update(str(proposal_id), status="rejected", decided_at=time.time(), decided_by=str(uid))
                return "Proposal rejected."
            if _mid := str(getattr(message, "id", "") or ""):
                if _mid == str(row.get("created_message_id") or ""):
                    return "Error: approval must come from a later user turn or the Apply button."
            if not _approval(getattr(message, "content", "")):
                return "Error: the current admin message must explicitly approve this proposal (for example: 'accept it' or 'ship it')."
            return await self.apply(str(proposal_id), str(uid), via_button=False)
        if action != "propose":
            return "Error: unknown action."

        try:
            plugin = _name(plugin_name)
            before, _after, diff = _plan(plugin, edits, new_files, delete_files)
        except Exception as exc:
            return f"Error: invalid plugin proposal: {type(exc).__name__}: {exc}"
        pid = uuid.uuid4().hex[:10]
        row = {
            "id": pid, "plugin_name": plugin, "summary": str(summary or "Plugin change")[:500],
            "status": "pending", "created_at": time.time(), "created_by": str(uid),
            "created_message_id": str(getattr(message, "id", "") or ""),
            "before_hashes": {rel: (_sha(text) if text is not None else None) for rel, text in before.items()},
            "edits": _as_list(edits, "edits"), "new_files": _as_list(new_files, "new_files"),
            "delete_files": _as_list(delete_files, "delete_files"), "diff": diff[:40_000],
        }
        await self.store.put(row)
        send = getattr(getattr(message, "channel", None), "send", None)
        if callable(send):
            with contextlib.suppress(Exception):
                await send(f"🧩 **Plugin proposal `{pid}`** — `{plugin}`\n{row['summary']}\nNothing has been written yet.", view=_ProposalView(self, pid))
        return f"Plugin proposal {pid} [pending]\nPlugin: {plugin}\nSummary: {row['summary']}\nWaiting for explicit admin approval."

    async def apply(self, pid: str, approver: str, *, via_button: bool) -> str:
        row = await self.store.get(pid)
        if not row or row.get("status") != "pending":
            return "Error: proposal is missing or no longer pending."
        plugin = str(row.get("plugin_name") or "")
        try:
            before, after, _ = _plan(plugin, row.get("edits"), row.get("new_files"), row.get("delete_files"))
        except Exception as exc:
            return f"Error: proposal no longer applies cleanly: {exc}"
        expected = row.get("before_hashes") or {}
        for rel, text in before.items():
            if (_sha(text) if text is not None else None) != expected.get(rel):
                return f"Error: {rel} changed since proposal creation; make a fresh proposal."

        root = (_PLUGINS_ROOT / plugin).resolve()
        try:
            for rel, text in after.items():
                dest = _file(plugin, rel)
                if text is None:
                    with contextlib.suppress(FileNotFoundError):
                        dest.unlink()
                else:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    tmp = dest.with_name(dest.name + ".maxwell-tmp")
                    tmp.write_text(text, "utf-8")
                    os.replace(tmp, dest)
            if not (root / "plugin.json").is_file() or not (root / "__init__.py").is_file():
                raise RuntimeError("plugin must contain plugin.json and __init__.py")
            manifest = json.loads((root / "plugin.json").read_text("utf-8"))
            if str(manifest.get("name") or "") != plugin:
                raise RuntimeError("plugin.json name does not match plugin directory")
            manager = getattr(self.bot, "plugin_manager", None)
            if manager is None or not callable(getattr(manager, "reload_plugins", None)):
                raise RuntimeError("plugin manager is unavailable")
            reload_result = manager.reload_plugins()
            if plugin not in (getattr(manager, "loaded_plugins", {}) or {}):
                raise RuntimeError(f"plugin failed to load after edit ({reload_result})")
        except Exception as exc:
            for rel, text in before.items():
                dest = _file(plugin, rel)
                if text is None:
                    with contextlib.suppress(FileNotFoundError):
                        dest.unlink()
                else:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_text(text, "utf-8")
            manager = getattr(self.bot, "plugin_manager", None)
            if manager is not None and callable(getattr(manager, "reload_plugins", None)):
                with contextlib.suppress(Exception):
                    manager.reload_plugins()
            await self.store.update(pid, last_error=f"{type(exc).__name__}: {exc}")
            return f"Error: plugin change rolled back: {type(exc).__name__}: {exc}"

        await self.store.update(pid, status="applied", decided_at=time.time(), decided_by=approver, approval="button" if via_button else "later_turn")
        return f"Applied plugin proposal {pid} to `{plugin}` and hot-reloaded the plugin registry."
