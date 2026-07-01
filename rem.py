"""REM memory assimilation for Maxwell."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from memory import _atomic_json_write_sync

logger = logging.getLogger(__name__)

DEFAULT_REM_PROMPT_BODY = (
    "Run Maxwell REM memory assimilation. Review the short-term visible slice injected by the scheduler. "
    "Search existing memories before adding/editing. Consolidate durable facts, preferences, decisions, identities, and unresolved work. "
    "Delete/supersede obsolete bloat. Do not call tools in REM mode. Answer DONE with a short audit list."
)


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def rem_system_prompt(turns_remaining: int, prompt_body: str | None = None) -> str:
    body = (prompt_body or DEFAULT_REM_PROMPT_BODY).strip()
    return (
        "You are Maxwell REM, a periodic memory assimilation process — not answering live chat. "
        "Organize the last slice of visible life into durable memory. Keep it useful, specific, deduplicated, inspectable; "
        "don't compress away decisions, preferences, unresolved tasks, or identity facts.\n\n"
        f"{body}\n\n"
        f"You have {turns_remaining} REM turn(s) left. Do not call tools. Answer DONE with a short audit list."
    )


def short_term_slice_prompt(events: list[dict]) -> str:
    return (
        "Short-term visible memory slice (last interval of inputs/outputs, reasoning excluded). "
        "Preserve what matters; discard noise:\n"
        + json.dumps(events, ensure_ascii=False, sort_keys=True)
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


def _message_content(message: dict) -> str:
    return str(message.get("content") or "")


async def _provider_message(
    provider, messages: list[dict], tools: list[dict], model: str, timeout: int, max_tokens: int | None = None
) -> dict:
    if hasattr(provider, "generate_chat_completion"):
        return await provider.generate_chat_completion(
            messages, tools=tools, model=model, timeout=timeout, max_tokens=max_tokens
        )
    content = await provider.generate_response(messages, timeout=timeout, max_tokens=max_tokens)
    return {"role": "assistant", "content": content}


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
    max_tokens: int | None = None,
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
        {"role": "system", "content": "Current long-term memory snapshot:\n" + json.dumps(memory_manager.get_long_term_memory()[:200], ensure_ascii=False)},
    ]
    audit = ""
    success = False
    try:
        await store.patch_state({"running": True, "running_since": started})
        response = await _provider_message(provider, messages, [], model, timeout, max_tokens)
        audit = _message_content(response).strip() or "DONE"
        finished = utcnow_iso()
        # Use 'started' as watermark so events recorded during the run are not lost
        run = {"ts": finished, "turns_used": 0, "audit": audit[:4000], "tool_counts": {}, "events": len(events)}
        await store.patch_state({"last_rem_run_ts": started, "last_audit": audit[:4000], "running": False, "running_since": ""})
        await store.append_run(run)
        success = True
        return run
    finally:
        # BUG FIX: CancelledError is BaseException since Python 3.9, not Exception.
        # If PM2 sends SIGTERM while REM runs, the old except Exception didn't catch
        # CancelledError, leaving running: True stuck in rem_state.json forever.
        # The API then refuses new runs. Use finally to always clear the flag.
        if not success:
            try:
                await store.patch_state({"running": False, "running_since": ""})
            except Exception:
                pass
