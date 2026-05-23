"""REM memory assimilation for Maxwell."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from memory import _atomic_json_write_sync

logger = logging.getLogger(__name__)

DEFAULT_REM_PROMPT_BODY = (
    "Run Maxwell REM memory assimilation. Review the short-term visible slice injected by the scheduler. "
    "Search existing memories before adding or editing. Consolidate durable facts, preferences, decisions, "
    "identities, and unresolved work. Do not let useful context vanish like tears in rain. Delete or supersede "
    "memory bloat when it is clearly obsolete. Do not call tools in REM mode. "
    "Answer DONE with a short audit list when complete."
)


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def rem_system_prompt(turns_remaining: int, prompt_body: str | None = None) -> str:
    body = (prompt_body or DEFAULT_REM_PROMPT_BODY).strip()
    return (
        "You are Maxwell REM, a periodic memory assimilation process. "
        "You are not answering live chat. You are organizing the last slice of visible life into durable memory.\n\n"
        "Keep memory useful, specific, "
        "deduplicated, and inspectable; do not compress away decisions, preferences, unresolved tasks, or identity facts. "
        "Do not let useful context vanish like tears in rain.\n\n"
        f"{body}\n\n"
        f"You have {turns_remaining} REM tool turn(s) left after this call. "
        "Do not call tools in REM mode. Always answer DONE with a short audit list."
    )


def short_term_slice_prompt(events: list[dict]) -> str:
    return (
        "Short-term visible memory slice follows. This is the last interval of visible inputs and outputs, "
        "with model reasoning intentionally excluded. Preserve what matters; discard noise.\n\n"
        + json.dumps(events, ensure_ascii=False, indent=2, sort_keys=True)
    )


def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, ValueError):
        return default


async def _save_json(path: Path, data):
    await asyncio.to_thread(_atomic_json_write_sync, path, data)


def _defaults_path() -> Path:
    return Path(__file__).resolve().parent / "rem_defaults.json"


def load_rem_defaults() -> dict:
    data = _load_json(_defaults_path(), {})
    if not isinstance(data, dict):
        data = {}
    return {
        "prompt": str(data.get("prompt") or DEFAULT_REM_PROMPT_BODY),
        "interval_seconds": int(data.get("interval_seconds") or 600),
        "max_turns": int(data.get("max_turns") or 3),
    }


class RemStore:
    def __init__(self, data_dir: str, run_history: int = 50):
        self.data_dir = Path(data_dir)
        self.state_file = self.data_dir / "rem_state.json"
        self.runs_file = self.data_dir / "rem_runs.json"
        self.control_file = self.data_dir / "rem_control.json"
        self.run_history = max(1, int(run_history or 50))
        self._lock = asyncio.Lock()

    async def load_state(self) -> dict:
        async with self._lock:
            state = _load_json(self.state_file, {})
            return state if isinstance(state, dict) else {}

    async def save_state(self, state: dict):
        async with self._lock:
            await _save_json(self.state_file, dict(state or {}))

    async def patch_state(self, updates: dict) -> dict:
        async with self._lock:
            state = _load_json(self.state_file, {})
            if not isinstance(state, dict):
                state = {}
            state.update(updates)
            await _save_json(self.state_file, state)
            return state

    async def load_runs(self) -> list:
        async with self._lock:
            runs = _load_json(self.runs_file, [])
            return runs if isinstance(runs, list) else []

    async def append_run(self, run: dict):
        async with self._lock:
            runs = _load_json(self.runs_file, [])
            if not isinstance(runs, list):
                runs = []
            runs.append(dict(run or {}))
            await _save_json(self.runs_file, runs[-self.run_history:])

    async def load_control(self) -> dict:
        async with self._lock:
            control = _load_json(self.control_file, {})
            return control if isinstance(control, dict) else {}

    async def save_control(self, control: dict):
        async with self._lock:
            await _save_json(self.control_file, dict(control or {}))


def ltm_tool_schemas() -> list[dict]:
    return [
        {"type": "function", "function": {"name": "ltm_list", "description": "List current long-term memory lines with ids.", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}},
        {"type": "function", "function": {"name": "ltm_search", "description": "Search long-term memory lines by simple substring/token match.", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"], "additionalProperties": False}}},
        {"type": "function", "function": {"name": "ltm_add", "description": "Add a durable long-term memory line.", "parameters": {"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"], "additionalProperties": False}}},
        {"type": "function", "function": {"name": "ltm_edit", "description": "Edit a long-term memory line by id.", "parameters": {"type": "object", "properties": {"id": {"type": "string"}, "content": {"type": "string"}}, "required": ["id", "content"], "additionalProperties": False}}},
        {"type": "function", "function": {"name": "ltm_remove", "description": "Remove a long-term memory line by id.", "parameters": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"], "additionalProperties": False}}},
    ]


def build_ltm_tools(memory_manager) -> dict[str, Callable[..., Awaitable[Any]]]:
    async def ltm_list():
        return memory_manager.get_long_term_memory()

    async def ltm_search(query: str):
        q = str(query or "").lower()
        tokens = [t for t in q.split() if t]
        out = []
        for item in memory_manager.get_long_term_memory():
            text = str(item.get("content", ""))
            hay = text.lower()
            if q in hay or (tokens and all(t in hay for t in tokens)):
                out.append(dict(item))
        return out

    async def ltm_add(content: str):
        return {"id": await memory_manager.add_long_term_memory(content)}

    async def ltm_edit(id: str, content: str):
        return {"ok": await memory_manager.edit_long_term_memory(id, content)}

    async def ltm_remove(id: str):
        return {"ok": await memory_manager.remove_long_term_memory(id)}

    return {
        "ltm_list": ltm_list,
        "ltm_search": ltm_search,
        "ltm_add": ltm_add,
        "ltm_edit": ltm_edit,
        "ltm_remove": ltm_remove,
    }


def _message_content(message: dict) -> str:
    return str(message.get("content") or "")


async def _provider_message(provider, messages: list[dict], tools: list[dict], model: str, timeout: int) -> dict:
    if hasattr(provider, "generate_chat_completion"):
        return await provider.generate_chat_completion(messages, tools=tools, model=model, timeout=timeout)
    content = await provider.generate_response(messages, timeout=timeout)
    return {"role": "assistant", "content": content}


def _tool_calls(message: dict) -> list[dict]:
    calls = message.get("tool_calls") or []
    return calls if isinstance(calls, list) else []


async def run_rem_once(
    *,
    memory_manager,
    rem_log,
    provider,
    data_dir: str,
    model: str,
    max_turns: int = 3,
    run_history: int = 50,
    prompt_body: str | None = None,
    timeout: int = 60,
) -> dict:
    store = RemStore(data_dir, run_history=run_history)
    state = await store.load_state()
    started = utcnow_iso()
    since = state.get("last_rem_run_ts")
    events = await rem_log.drain_slice(since)
    if not events:
        await store.patch_state({"last_rem_run_ts": started, "running": False, "running_since": "", "last_audit": "DONE - empty slice"})
        run = {"ts": started, "turns_used": 0, "audit": "DONE - empty slice", "tool_counts": {}, "events": 0}
        await store.append_run(run)
        return run

    messages = [
        {"role": "system", "content": rem_system_prompt(max_turns, prompt_body=prompt_body)},
        {"role": "system", "content": short_term_slice_prompt(events)},
        {"role": "system", "content": "Current long-term memory snapshot:\n" + json.dumps(memory_manager.get_long_term_memory()[:200], ensure_ascii=False, indent=2)},
    ]
    tool_counts: Counter[str] = Counter()
    audit = ""
    turns_used = 0
    try:
        await store.patch_state({"running": True, "running_since": started})
        response = await _provider_message(provider, messages, [], model, timeout)
        audit = _message_content(response).strip() or "DONE"
        finished = utcnow_iso()
        run = {"ts": finished, "turns_used": turns_used, "audit": audit[:4000], "tool_counts": dict(tool_counts), "events": len(events)}
        await store.patch_state({"last_rem_run_ts": finished, "last_audit": audit[:4000], "running": False, "running_since": ""})
        await store.append_run(run)
        return run
    except Exception:
        await store.patch_state({"running": False, "running_since": ""})
        raise
