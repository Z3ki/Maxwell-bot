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
    "Delete/supersede obsolete bloat. To persist changes, end your response with a single JSON object on its own line "
    "in this exact shape (and nothing else after it):\n"
    '{"actions": {"ltm_add": ["fact 1", "fact 2"], "ltm_remove": [<existing line numbers to drop>], '
    '"shared_add": [{"scope": "global|user:<id>|guild:<id>|channel:<id>", "content": "fact", "importance": 1-10}]}, '
    '"audit": "one-paragraph human summary of what you did and why"}\n'
    "If nothing needs persisting, emit {\"actions\": {}, \"audit\": \"no changes\"}. The audit field is required."
)


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def rem_system_prompt(turns_remaining: int, prompt_body: str | None = None) -> str:
    body = (prompt_body or DEFAULT_REM_PROMPT_BODY).strip()
    # NOTE: REM is a single-pass audit, not a multi-turn tool loop. The old
    # prompt advertised "N turns left" but the runner never looped, which
    # misled the model. Don't mention turns; just ask for one DONE audit.
    return (
        "You are Maxwell REM, a periodic memory assimilation process — not answering live chat.\n"
        "Organize the last slice of visible life into durable memory. Keep it useful, specific, deduplicated, inspectable.\n"
        "Don't compress away decisions, preferences, unresolved tasks, or identity facts.\n\n"
        f"## Task\n{body}\n\n"
        "## Output\nSingle pass. Reason briefly, then end your response with exactly one JSON object (one line) "
        "describing actions to take. The JSON's `audit` field is the only thing the dashboard shows; keep it short."
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
    # Use `or` chains to fall back to defaults when the JSON value is missing
    # OR an empty string/0/None. Each int() conversion is wrapped in a
    # try/except so a malformed rem_defaults.json (e.g. "interval_seconds":
    # "ten minutes") doesn't crash bot startup — it falls back to the
    # hardcoded default instead.
    try:
        interval_seconds = int(data.get("interval_seconds") or 600)
    except (TypeError, ValueError):
        interval_seconds = 600
    try:
        max_turns = int(data.get("max_turns") or 3)
    except (TypeError, ValueError):
        max_turns = 3
    return {
        "prompt": str(data.get("prompt") or DEFAULT_REM_PROMPT_BODY),
        "interval_seconds": interval_seconds,
        "max_turns": max_turns,
    }


class RemStore:
    def __init__(self, data_dir: str, run_history: int = 50):
        self.data_dir = Path(data_dir)
        self.state_file = self.data_dir / "rem_state.json"
        self.runs_file = self.data_dir / "rem_runs.json"
        self.control_file = self.data_dir / "rem_control.json"
        # run_history is operator-configured via env, but defense-in-depth:
        # treat bogus values (negative, "lots") as the default 50 instead of
        # letting an integer-only assertion crash bot startup.
        try:
            self.run_history = max(1, int(run_history or 50))
        except (TypeError, ValueError):
            self.run_history = 50
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
            await _save_json(self.runs_file, runs[-self.run_history :])

    async def load_control(self) -> dict:
        async with self._lock:
            control = _load_json(self.control_file, {})
            return control if isinstance(control, dict) else {}

    async def save_control(self, control: dict):
        async with self._lock:
            await _save_json(self.control_file, dict(control or {}))


def _message_content(message: dict) -> str:
    return str(message.get("content") or "")


def _extract_rem_json(raw: str) -> dict | None:
    """Pull the trailing JSON object from a REM response.

    The model is told to end with a single-line JSON object. We scan
    brace-balanced blocks from the right so a stray '{' in the prose
    doesn't get mistaken for the action payload. Returns None if no
    well-formed JSON object is found.
    """
    text = str(raw or "").strip()
    if not text or "{" not in text:
        return None
    for i in range(len(text) - 1, -1, -1):
        if text[i] != "}":
            continue
        depth = 0
        start = -1
        for j in range(i, -1, -1):
            c = text[j]
            if c == "}":
                depth += 1
            elif c == "{":
                depth -= 1
                if depth == 0:
                    start = j
                    break
        if start < 0:
            continue
        candidate = text[start : i + 1]
        try:
            return json.loads(candidate)
        except (ValueError, TypeError):
            continue
    return None


async def _apply_audit_actions(raw_audit: str, memory_manager) -> tuple[dict, str]:
    """Parse the trailing JSON and apply ltm_add / ltm_remove / shared_add.

    Returns (counts, audit_line) so the run record shows what changed.
    The apply is best-effort: a single bad fact never blocks the rest.
    """
    payload = _extract_rem_json(raw_audit)
    if not isinstance(payload, dict):
        return {"ltm_added": 0, "ltm_removed": 0, "shared_added": 0}, ""
    actions = payload.get("actions") or {}
    if not isinstance(actions, dict):
        return {"ltm_added": 0, "ltm_removed": 0, "shared_added": 0}, ""

    counts = {"ltm_added": 0, "ltm_removed": 0, "shared_added": 0}

    # LTM removes first so any renumber from adds is applied to the surviving ids.
    for raw_id in actions.get("ltm_remove") or []:
        try:
            ok = await memory_manager.remove_long_term_memory(str(raw_id))
        except Exception:
            ok = False
        if ok:
            counts["ltm_removed"] += 1

    for fact in actions.get("ltm_add") or []:
        if not isinstance(fact, str):
            continue
        fact = fact.strip()
        if not fact:
            continue
        try:
            await memory_manager.add_long_term_memory(fact)
            counts["ltm_added"] += 1
        except Exception:
            continue

    for shared in actions.get("shared_add") or []:
        if not isinstance(shared, dict):
            continue
        content = str(shared.get("content") or "").strip()
        if not content:
            continue
        entry = {
            "content": content,
            "scope": str(shared.get("scope") or "global"),
            "importance": shared.get("importance", 5),
        }
        for opt in ("source_user_id", "source_channel_id", "source_guild_id", "tags", "source_kind"):
            if opt in shared:
                entry[opt] = shared[opt]
        try:
            await memory_manager.add_shared_context(entry)
            counts["shared_added"] += 1
        except Exception:
            continue

    summary = (
        f"ltm+{counts['ltm_added']}/-{counts['ltm_removed']} "
        f"shared+{counts['shared_added']}"
    )
    return counts, summary


async def _provider_message(
    provider,
    messages: list[dict],
    tools: list[dict],
    model: str,
    timeout: int,
    max_tokens: int | None = None,
) -> dict:
    if hasattr(provider, "generate_chat_completion"):
        return await provider.generate_chat_completion(
            messages, tools=tools, model=model, timeout=timeout, max_tokens=max_tokens
        )
    content = await provider.generate_response(
        messages, timeout=timeout, max_tokens=max_tokens
    )
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
    apply_actions: bool = True,
) -> dict:
    store = RemStore(data_dir, run_history=run_history)
    state = await store.load_state()
    started = utcnow_iso()
    since = state.get("last_rem_run_ts")
    events = await rem_log.drain_slice(since)
    if not events:
        await store.patch_state(
            {
                "last_rem_run_ts": started,
                "running": False,
                "running_since": "",
                "last_audit": "DONE - empty slice",
            }
        )
        run = {
            "ts": started,
            "turns_used": 0,
            "audit": "DONE - empty slice",
            "tool_counts": {},
            "events": 0,
        }
        await store.append_run(run)
        return run

    messages = [
        {
            "role": "system",
            "content": rem_system_prompt(max_turns, prompt_body=prompt_body),
        },
        {"role": "system", "content": short_term_slice_prompt(events)},
        {
            "role": "system",
            "content": "Current long-term memory snapshot:\n"
            + json.dumps(
                (await asyncio.to_thread(memory_manager.get_long_term_memory))[:200],
                ensure_ascii=False,
            ),
        },
    ]
    audit = ""
    try:
        await store.patch_state({"running": True, "running_since": started})
        response = await _provider_message(
            provider, messages, [], model, timeout, max_tokens
        )
        raw_audit = _message_content(response).strip() or "DONE"
        audit = raw_audit[:4000]
        actions_applied = {"ltm_added": 0, "ltm_removed": 0, "shared_added": 0}
        actions_audit = ""
        if apply_actions:
            try:
                actions_applied, actions_audit = await _apply_audit_actions(
                    raw_audit, memory_manager
                )
            except Exception as e:  # never let a malformed audit kill REM
                logger.warning(f"REM action apply failed: {e}")
                actions_audit = f"action-apply-error: {e}"
        if actions_audit:
            audit = (audit + "\n\n[actions] " + actions_audit)[:4000]
        finished = utcnow_iso()
        # Use 'started' as watermark so events recorded during the run are not lost
        run = {
            "ts": finished,
            "turns_used": 0,
            "audit": audit[:4000],
            "tool_counts": actions_applied,
            "events": len(events),
        }
        await store.patch_state(
            {
                "last_rem_run_ts": started,
                "last_audit": audit[:4000],
                "running": False,
                "running_since": "",
            }
        )
        await store.append_run(run)
        return run
    finally:
        # Always clear the running flag in finally (covers CancelledError,
        # exceptions, and any partial success path). Prevents stuck
        # "running: true" that blocks the API from allowing new REM runs.
        # contextlib.suppress(Exception) does NOT cover CancelledError (a
        # BaseException since 3.8), so handle it explicitly and re-raise so a
        # mid-run cancel still clears the flag then propagates.
        try:
            await store.patch_state({"running": False, "running_since": ""})
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
