"""Second-generation Maxwell plugin workbench.

This builds on the conservative human-approved editor in ``developer.py`` and
adds the pieces needed for Maxwell to author plugins from scratch instead of
only patching existing files: scaffolding, full-file replacement proposals,
static diagnostics, runtime inspection, self-tests, revision history, and
human-approved rollback proposals.

Nothing in this module bypasses the original approval boundary. Every write,
including a rollback, is still represented as a pending proposal and must be
approved in a later admin turn or with the Apply button.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from . import developer as legacy

_HISTORY_LIMIT = 40
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")


class _RevisionStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = asyncio.Lock()

    def _read(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text("utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return []
        return [dict(row) for row in data if isinstance(row, dict)] if isinstance(data, list) else []

    def _write(self, rows: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(rows[:_HISTORY_LIMIT], indent=2, ensure_ascii=False), "utf-8")
        os.replace(tmp, self.path)

    async def add(self, row: dict[str, Any]) -> None:
        async with self.lock:
            rows = self._read()
            rows.insert(0, row)
            self._write(rows)

    async def get(self, revision_id: str) -> dict[str, Any] | None:
        async with self.lock:
            for row in self._read():
                if str(row.get("id") or "") == str(revision_id):
                    return row
        return None

    async def list(self, plugin_name: str | None = None) -> list[dict[str, Any]]:
        async with self.lock:
            rows = self._read()
        if plugin_name:
            rows = [r for r in rows if str(r.get("plugin_name") or "") == str(plugin_name)]
        return rows


def _admin(bot: Any, message: Any) -> bool:
    uid = getattr(getattr(message, "author", None), "id", None)
    return legacy._is_admin(bot, uid)


def _plugin_root(plugin: str) -> Path:
    return (legacy._PLUGINS_ROOT / legacy._name(plugin)).resolve()


def _read_regular(plugin: str, rel: str) -> str | None:
    path = legacy._file(plugin, rel)
    if not path.exists():
        return None
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"not a regular plugin file: {rel}")
    text = path.read_text("utf-8")
    if len(text) > legacy._MAX_FILE_CHARS:
        raise ValueError(f"{rel} is too large")
    return text


def _safe_tree(plugin: str) -> list[tuple[str, int]]:
    root = _plugin_root(plugin)
    if not root.is_dir():
        return []
    rows: list[tuple[str, int]] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if any(part.startswith(".") for part in Path(rel).parts):
            continue
        if path.suffix.lower() not in legacy._ALLOWED:
            continue
        with contextlib.suppress(OSError):
            rows.append((rel, path.stat().st_size))
    return rows[:200]


def _tool_class_name(plugin: str) -> str:
    bits = [p for p in re.split(r"[^A-Za-z0-9]+", plugin) if p]
    base = "".join(p[:1].upper() + p[1:] for p in bits) or "Plugin"
    if base[0].isdigit():
        base = "Plugin" + base
    return base + "Tool"


def _scaffold_files(
    plugin: str,
    *,
    description: str,
    template: str,
    enabled_globally: bool,
) -> list[dict[str, str]]:
    """Generate a conservative but useful starter plugin."""

    plugin = legacy._name(plugin)
    template = str(template or "tool").strip().lower()
    if template not in {"tool", "event", "scheduled", "full"}:
        raise ValueError("template must be tool, event, scheduled, or full")
    description = " ".join(str(description or f"Maxwell plugin: {plugin}").split())[:300]
    cls = _tool_class_name(plugin)
    tool_name = re.sub(r"[^a-z0-9_]+", "_", plugin.lower()).strip("_") or "plugin"
    tool_name += "_tool"

    manifest = {
        "name": plugin,
        "description": description,
        "version": "0.1.0",
        "enabled_globally": bool(enabled_globally),
        "allowed_users": [],
        "denied_users": [],
    }

    callbacks = []
    registrations = []
    if template in {"event", "full"}:
        callbacks.append(
            "    async def on_message(message):\n"
            "        # Keep event handlers fast. Call bot tools explicitly if the plugin needs effects.\n"
            "        ctx.log.debug(\"message event id=%s\", getattr(message, \"id\", None))\n"
        )
        registrations.append('    ctx.on_event("on_message", on_message)')
    if template in {"scheduled", "full"}:
        callbacks.append(
            "    async def heartbeat():\n"
            "        # Scheduled jobs are host-tracked and are cancelled cleanly on reload.\n"
            "        ctx.log.debug(\"scheduled plugin heartbeat\")\n"
        )
        registrations.append("    ctx.every(60, heartbeat)")

    init_lines = [
        f"from .tools import {cls}",
        "",
        "",
        "def setup(bot, ctx):",
    ]
    if callbacks:
        init_lines.extend("\n".join(callbacks).rstrip().splitlines())
    init_lines.extend(registrations)
    init_lines.append(f"    return [{cls}(bot, ctx)]")
    init_lines.append("")

    tools_py = f'''from __future__ import annotations\n\nfrom typing import Any\n\nfrom tools import Tool\n\n\nclass {cls}(Tool):\n    returns_result = True\n\n    def __init__(self, bot: Any, ctx: Any):\n        self.bot = bot\n        self.ctx = ctx\n\n    def get_name(self) -> str:\n        return {tool_name!r}\n\n    def get_description(self) -> str:\n        return {description!r}\n\n    def get_parameters(self) -> dict:\n        return {{\n            "type": "object",\n            "properties": {{\n                "text": {{"type": "string", "description": "Input for this plugin"}},\n            }},\n        }}\n\n    async def execute(self, message: Any, text: str = "", **kwargs: Any) -> str:\n        # Replace this implementation with the plugin's real behavior.\n        return f"{plugin}: {{text}}" if text else "{plugin} is ready."\n'''

    readme = f"""# {plugin}\n\n{description}\n\n## Maxwell plugin contract\n\n- `setup(bot, ctx)` returns tool objects.\n- `ctx.store_path(name)` gives this plugin isolated persistent storage.\n- `ctx.on_event(name, async_callback)` registers a managed Discord event hook.\n- `ctx.every(seconds, async_callback)` registers a managed recurring job.\n- `ctx.tool(name)` resolves a built-in Maxwell tool when one is needed.\n- `ctx.is_admin(user_id)` is the supported admin check.\n- Optional module-level `teardown(bot)` is called during managed teardown.\n\nThe plugin starts disabled unless `enabled_globally` is changed or it is enabled through Maxwell's plugin controls.\n"""

    return [
        {"path": "plugin.json", "content": json.dumps(manifest, indent=2) + "\n"},
        {"path": "__init__.py", "content": "\n".join(init_lines)},
        {"path": "tools.py", "content": tools_py},
        {"path": "README.md", "content": readme},
    ]


def _doctor(tool: "PluginWorkbenchTool", plugin: str) -> str:
    plugin = legacy._name(plugin)
    root = _plugin_root(plugin)
    errors: list[str] = []
    warnings: list[str] = []
    info: list[str] = []
    if not root.is_dir():
        return f"Plugin `{plugin}` does not exist yet. Use scaffold or propose to create it."

    manifest_path = root / "plugin.json"
    init_path = root / "__init__.py"
    if not manifest_path.is_file():
        errors.append("missing plugin.json")
        manifest = {}
    else:
        try:
            manifest = json.loads(manifest_path.read_text("utf-8"))
            if not isinstance(manifest, dict):
                raise TypeError("manifest root is not an object")
        except Exception as exc:
            manifest = {}
            errors.append(f"plugin.json invalid: {type(exc).__name__}: {exc}")

    if not init_path.is_file():
        errors.append("missing __init__.py")
    if manifest:
        if str(manifest.get("name") or "") != plugin:
            errors.append("manifest name does not match directory")
        version = str(manifest.get("version") or "")
        if not version:
            warnings.append("manifest has no version")
        elif not _SEMVER_RE.fullmatch(version):
            warnings.append(f"version {version!r} is not semantic-version shaped")
        if not str(manifest.get("description") or "").strip():
            warnings.append("manifest has no description")

    py_count = 0
    json_count = 0
    total_bytes = 0
    for rel, size in _safe_tree(plugin):
        total_bytes += size
        path = root / rel
        if rel.endswith(".py"):
            py_count += 1
            try:
                compile(path.read_text("utf-8"), f"plugins/{plugin}/{rel}", "exec")
            except Exception as exc:
                errors.append(f"{rel}: {type(exc).__name__}: {exc}")
        elif rel.endswith(".json"):
            json_count += 1
            try:
                json.loads(path.read_text("utf-8"))
            except Exception as exc:
                errors.append(f"{rel}: invalid JSON: {exc}")

    manager = getattr(tool.bot, "plugin_manager", None)
    loaded = (getattr(manager, "loaded_plugins", {}) or {}).get(plugin) if manager else None
    if loaded is None:
        warnings.append("plugin is not currently in the live loaded registry")
    else:
        tools = list((loaded.get("tools") or {}).keys()) if isinstance(loaded, dict) else []
        events = manager.plugin_events(plugin) if callable(getattr(manager, "plugin_events", None)) else []
        jobs = len((getattr(manager, "_job_specs", {}) or {}).get(plugin) or [])
        info.append(f"live tools={tools or 'none'}, events={events or 'none'}, jobs={jobs}")
        builtins = set((getattr(tool.bot, "tools", None) or {}).keys())
        collisions = sorted(set(tools) & builtins)
        if collisions:
            warnings.append("tool names also exist in the built-in registry: " + ", ".join(collisions))

    status = "PASS" if not errors else "FAIL"
    lines = [
        f"Plugin doctor `{plugin}`: {status}",
        f"Files: {len(_safe_tree(plugin))} ({py_count} Python, {json_count} JSON), {total_bytes} bytes",
    ]
    lines.extend(f"ERROR: {x}" for x in errors[:20])
    lines.extend(f"WARN: {x}" for x in warnings[:20])
    lines.extend(f"INFO: {x}" for x in info[:20])
    return "\n".join(lines)


class PluginWorkbenchTool(legacy.PluginWorkbenchTool):
    """Authoring-capable workbench while preserving legacy approval semantics."""

    def __init__(self, bot: Any, ctx: Any):
        super().__init__(bot, ctx)
        self.revisions = _RevisionStore(ctx.store_path("plugin_revisions.json"))

    def get_description(self) -> str:
        return (
            "Admin-only plugin IDE for Maxwell. It can inspect plugin trees/files, show a "
            "development guide, scaffold brand-new plugins, statically validate/doctor them, "
            "run optional self-tests, propose search/replace changes or complete file rewrites, "
            "and keep revision history with rollback proposals. Every filesystem change still "
            "requires explicit admin approval in a later turn or the Apply button; never apply "
            "in the same turn as a proposal. Use scaffold/replace/propose when an admin asks "
            "Maxwell to CREATE a plugin, not only when fixing an existing one."
        )

    def get_parameters(self) -> dict:
        schema = super().get_parameters()
        props = schema["properties"]
        props["action"]["enum"] = [
            "list",
            "tree",
            "guide",
            "read",
            "doctor",
            "selftest",
            "scaffold",
            "replace",
            "propose",
            "show",
            "history",
            "rollback",
            "apply",
            "reject",
        ]
        props["template"] = {
            "type": "string",
            "enum": ["tool", "event", "scheduled", "full"],
        }
        props["description"] = {"type": "string"}
        props["enabled_globally"] = {"type": "boolean"}
        props["revision_id"] = {"type": "string"}
        props["files"] = {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        }
        return schema

    async def execute(
        self,
        message: Any,
        action: str,
        plugin_name: str | None = None,
        path: str | None = None,
        summary: str | None = None,
        proposal_id: str | None = None,
        edits: Any = None,
        new_files: Any = None,
        delete_files: Any = None,
        files: Any = None,
        template: str | None = None,
        description: str | None = None,
        enabled_globally: bool = False,
        revision_id: str | None = None,
        **kwargs: Any,
    ) -> str:
        action = str(action or "").strip().lower()
        if action in {"list", "read", "propose", "show", "apply", "reject"}:
            return await super().execute(
                message,
                action=action,
                plugin_name=plugin_name,
                path=path,
                summary=summary,
                proposal_id=proposal_id,
                edits=edits,
                new_files=new_files,
                delete_files=delete_files,
                **kwargs,
            )
        if not _admin(self.bot, message):
            return "Error: plugin_workbench is admin-only."

        try:
            plugin = legacy._name(plugin_name) if plugin_name else ""
        except Exception as exc:
            return f"Error: {exc}"

        if action == "guide":
            return (
                "Maxwell plugin guide:\n"
                "- Required for managed creation: plugin.json + __init__.py.\n"
                "- setup(bot, ctx) returns a list of Tool objects; legacy setup(bot) still works.\n"
                "- Tool methods: get_name(), get_description(), get_parameters(), async execute().\n"
                "- Set returns_result=True when the model needs to read the tool output.\n"
                "- ctx.store_path(name) -> isolated persistent data path.\n"
                "- ctx.on_event(event, async_callback) -> managed Discord event subscription.\n"
                "- ctx.every(seconds, async_callback, run_immediately=False) -> managed job (>=5s).\n"
                "- ctx.tool(name) -> built-in Maxwell tool; ctx.is_admin(id) -> safe admin check.\n"
                "- Optional teardown(bot) is called by managed teardown.\n"
                "- Prefer scaffold for a new baseline, then replace/propose custom source.\n"
                "- All writes are pending proposals until a later explicit admin approval."
            )

        if action == "tree":
            rows = _safe_tree(plugin)
            if not rows:
                return f"Plugin `{plugin}` has no readable plugin files or does not exist."
            return "\n".join(f"{rel} ({size} bytes)" for rel, size in rows)

        if action == "doctor":
            return _doctor(self, plugin)

        if action == "selftest":
            manager = getattr(self.bot, "plugin_manager", None)
            data = (getattr(manager, "loaded_plugins", {}) or {}).get(plugin) if manager else None
            module = (data or {}).get("module") if isinstance(data, dict) else None
            hook = getattr(module, "selftest", None)
            if not callable(hook):
                return f"Plugin `{plugin}` has no selftest(bot, ctx) hook."
            ctx = (data or {}).get("context") if isinstance(data, dict) else None
            try:
                result = hook(self.bot, ctx)
                if inspect.isawaitable(result):
                    result = await asyncio.wait_for(result, timeout=10.0)
            except asyncio.TimeoutError:
                return "Error: plugin selftest timed out after 10 seconds."
            except Exception as exc:
                return f"Error: plugin selftest failed: {type(exc).__name__}: {exc}"
            return f"Plugin `{plugin}` selftest: {result if result is not None else 'PASS'}"

        if action == "scaffold":
            root = _plugin_root(plugin)
            if root.exists() and any(root.iterdir()):
                return "Error: plugin directory already contains files; use replace/propose instead."
            try:
                generated = _scaffold_files(
                    plugin,
                    description=str(description or summary or f"Maxwell plugin: {plugin}"),
                    template=str(template or "tool"),
                    enabled_globally=bool(enabled_globally),
                )
            except Exception as exc:
                return f"Error: could not scaffold plugin: {exc}"
            return await super().execute(
                message,
                action="propose",
                plugin_name=plugin,
                summary=summary or f"Create `{plugin}` plugin ({template or 'tool'} template)",
                new_files=generated,
            )

        if action == "replace":
            try:
                rows = legacy._as_list(files, "files")
            except Exception as exc:
                return f"Error: {exc}"
            if not rows:
                return "Error: replace needs at least one file."
            replace_edits: list[dict[str, str]] = []
            creates: list[dict[str, str]] = []
            try:
                for item in rows:
                    if not isinstance(item, dict):
                        raise TypeError("each file must be an object")
                    rel = str(item.get("path") or "").strip()
                    content = item.get("content")
                    if not isinstance(content, str):
                        raise TypeError(f"file {rel!r} needs string content")
                    current = _read_regular(plugin, rel)
                    if current is None:
                        creates.append({"path": rel, "content": content})
                    else:
                        replace_edits.append({"path": rel, "old": current, "new": content})
            except Exception as exc:
                return f"Error: invalid replacement: {type(exc).__name__}: {exc}"
            return await super().execute(
                message,
                action="propose",
                plugin_name=plugin,
                summary=summary or f"Replace {len(rows)} file(s) in `{plugin}`",
                edits=replace_edits,
                new_files=creates,
                delete_files=delete_files,
            )

        if action == "history":
            rows = await self.revisions.list(plugin or None)
            if not rows:
                return "No applied plugin revisions recorded yet."
            out = []
            for row in rows[:20]:
                out.append(
                    f"{row.get('id')} | {row.get('plugin_name')} | "
                    f"{row.get('summary') or 'change'} | {row.get('applied_at')}"
                )
            return "Applied plugin revisions:\n" + "\n".join(out)

        if action == "rollback":
            rid = str(revision_id or proposal_id or "").strip()
            row = await self.revisions.get(rid)
            if not row:
                return "Error: revision not found."
            plugin = str(row.get("plugin_name") or "")
            before = row.get("before") or {}
            if not isinstance(before, dict):
                return "Error: revision snapshot is invalid."
            restore_edits: list[dict[str, str]] = []
            restore_new: list[dict[str, str]] = []
            restore_delete: list[str] = []
            try:
                for rel, wanted in before.items():
                    current = _read_regular(plugin, rel)
                    if wanted is None:
                        if current is not None:
                            restore_delete.append(rel)
                    elif current is None:
                        restore_new.append({"path": rel, "content": str(wanted)})
                    elif current != str(wanted):
                        restore_edits.append({"path": rel, "old": current, "new": str(wanted)})
            except Exception as exc:
                return f"Error: rollback could not inspect current files: {exc}"
            if not (restore_edits or restore_new or restore_delete):
                return "Revision is already in the requested pre-change state."
            return await super().execute(
                message,
                action="propose",
                plugin_name=plugin,
                summary=f"Rollback revision {rid}: {row.get('summary') or 'plugin change'}",
                edits=restore_edits,
                new_files=restore_new,
                delete_files=restore_delete,
            )

        return "Error: unknown action."

    async def apply(self, pid: str, approver: str, *, via_button: bool) -> str:
        row = await self.store.get(pid)
        before: dict[str, str | None] = {}
        if row and row.get("status") == "pending":
            plugin = str(row.get("plugin_name") or "")
            with contextlib.suppress(Exception):
                before, _after, _diff = legacy._plan(
                    plugin,
                    row.get("edits"),
                    row.get("new_files"),
                    row.get("delete_files"),
                )
        result = await super().apply(pid, approver, via_button=via_button)
        if not result.startswith("Applied plugin proposal ") or not row:
            return result

        plugin = str(row.get("plugin_name") or "")
        after: dict[str, str | None] = {}
        for rel in before:
            with contextlib.suppress(Exception):
                after[rel] = _read_regular(plugin, rel)
        await self.revisions.add(
            {
                "id": pid,
                "plugin_name": plugin,
                "summary": str(row.get("summary") or "Plugin change"),
                "applied_at": time.time(),
                "approver": str(approver),
                "approval": "button" if via_button else "later_turn",
                "before": before,
                "after": after,
            }
        )
        return result + " Revision snapshot recorded for rollback."


__all__ = ["PluginWorkbenchTool"]
