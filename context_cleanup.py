"""ContextCleanupEngine — Maxwell's shared-context janitor.

Runs alongside REM and autonomy. Periodically loads the full shared-context
store, asks the LLM (the SAME provider/model autonomy and REM use) to review it
for duplicates, contradictions, stale/garbage entries, and produces a batch of
cleanup operations (delete, edit, merge, add) that are applied through the
existing MemoryManager API.

Why this exists: the context-watcher agent fires on every flagged message and
loves to produce near-duplicate facts, half-sentences, and entries that
contradict newer ones. The on-add dedup in memory.py only catches >80% text
overlap in the same scope; semantic dupes and weird cross-scope cruft pile up
until a human has to scrub them by hand from the dashboard. This agent does the
scrub on a schedule instead.

Design notes:
- Uses MemoryManager.list_shared_context / remove_shared_context /
  update_shared_context / add_shared_context. It never writes the file
  directly — every mutation goes through the locked memory API so it stays
  consistent with the on-add sanitization (re-dedup, eviction, mtime tracking).
- Shares the autonomy/REM provider + model. If the autonomy provider isn't
  configured it transparently falls back to the main bot provider via
  bot._get_autonomy_provider().
- No approval queue. Like autonomy/REM, it just runs. The audit log + last
  actions are surfaced in the dashboard so a human can see what it did and
  undo deletions if needed.
- One cleanup pass = one LLM call over the current snapshot. If the store is
  huge it caps the reviewed slice so we don't blow the context window.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from control_defaults import DEFAULT_CONTROL  # noqa: E402
from utils import _atomic_json_write_sync  # noqa: E402

logger = logging.getLogger(__name__)

# Cap the snapshot sent to the LLM. The store can hold 1000 entries; sending all
# of them would overflow most context windows and cost a fortune. 200 is plenty
# to find obvious dupes and weird stuff — on-add dedup already catches exact
# matches, so this pass mostly hunts semantic dupes and garbage.
MAX_REVIEW_ENTRIES = 200
# Hard cap on operations the LLM may request in one pass. Stops a hallucinating
# model from nuking the whole store in a single tick.
MAX_OPS_PER_PASS = 60
MAX_CONTENT_CHARS = 1200
LOG_RING_SIZE = 50
MAX_ACTIONS_PER_PASS = MAX_OPS_PER_PASS

VALID_OP_KINDS = frozenset({"delete", "edit", "merge", "add"})
VALID_VISIBILITIES = frozenset({"shared", "private", "admin_only", "public_hint"})


def _safe_int(val, default=0):
    """Parse int safely, returning default on failure."""
    try:
        return int(val) if val is not None else default
    except (TypeError, ValueError):
        return default


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json_safe(path: Path, default):
    try:
        if not path.exists():
            return default() if callable(default) else default
        raw = path.read_text(encoding="utf-8").strip()
        if not raw:
            return default() if callable(default) else default
        data = json.loads(raw)
        return data
    except (json.JSONDecodeError, OSError, ValueError) as e:
        # Fail closed: do NOT overwrite the on-disk file with {} on a transient
        # read error. Overwriting wiped state/watermarks in production (a corrupt
        # read of autonomy_goals/context_cleanup_state reset everything and made
        # the next pass reprocess the entire slice). Leave the file intact so a
        # human can recover it; the engine runs off in-memory defaults this cycle.
        logger.warning(
            f"Corrupt/unreadable {path.name}, using defaults (file left intact): {e}"
        )
        return default() if callable(default) else default


def _truncate(text: str, budget: int) -> str:
    budget = max(0, _safe_int(budget, 0))
    if len(text) <= budget:
        return text
    suffix = "\n... [truncated]"
    if budget <= len(suffix):
        return text[:budget]
    return text[: budget - len(suffix)] + suffix


def _strip_id(value: Any) -> str:
    return re.sub(r"[^A-Za-z0-9_\-]", "", str(value or ""))[:64]


def _coerce_scope(value: Any, fallback: str = "global") -> str:
    scope = str(value or "").strip().lower()[:80]
    return scope or fallback


def _coerce_visibility(value: Any, fallback: str = "shared") -> str:
    vis = str(value or "").strip().lower()
    return vis if vis in VALID_VISIBILITIES else fallback


def _coerce_importance(value: Any, fallback: int = 5) -> int:
    try:
        return max(1, min(int(value), 10))
    except (TypeError, ValueError):
        return fallback


class ContextCleanupStore:
    """JSON-backed state + audit log for the cleanup agent (atomic writes)."""

    def __init__(self, data_dir: str):
        self.data_dir = Path(data_dir)
        self.state_file = self.data_dir / "context_cleanup_state.json"
        self.log_file = self.data_dir / "context_cleanup_log.json"
        self.control_file = self.data_dir / "context_cleanup_control.json"
        self._lock = asyncio.Lock()

    async def load_state(self) -> dict:
        async with self._lock:
            data = await asyncio.to_thread(_load_json_safe, self.state_file, dict)
            return data if isinstance(data, dict) else {}

    async def save_state(self, state: dict):
        async with self._lock:
            await asyncio.to_thread(_atomic_json_write_sync, self.state_file, state)

    async def patch_state(self, updates: dict) -> dict:
        async with self._lock:
            state = await asyncio.to_thread(_load_json_safe, self.state_file, dict)
            if not isinstance(state, dict):
                state = {}
            state.update(updates)
            await asyncio.to_thread(_atomic_json_write_sync, self.state_file, state)
            return state

    async def update_state(self, fn) -> dict:
        async with self._lock:
            state = await asyncio.to_thread(_load_json_safe, self.state_file, dict)
            if not isinstance(state, dict):
                state = {}
            fn(state)
            await asyncio.to_thread(_atomic_json_write_sync, self.state_file, state)
            return state

    async def load_control(self) -> dict:
        async with self._lock:
            control = await asyncio.to_thread(_load_json_safe, self.control_file, dict)
            return control if isinstance(control, dict) else {}

    async def save_control(self, control: dict):
        async with self._lock:
            await asyncio.to_thread(
                _atomic_json_write_sync, self.control_file, dict(control or {})
            )

    async def load_log(self) -> list[dict]:
        async with self._lock:
            data = await asyncio.to_thread(_load_json_safe, self.log_file, dict)
            entries = data.get("entries", []) if isinstance(data, dict) else []
            return entries if isinstance(entries, list) else []

    async def append_log_entry(self, entry: dict):
        async with self._lock:
            data = await asyncio.to_thread(_load_json_safe, self.log_file, dict)
            entries = data.get("entries", []) if isinstance(data, dict) else []
            if not isinstance(entries, list):
                entries = []
            entries.append(entry)
            entries = entries[-LOG_RING_SIZE:]
            await asyncio.to_thread(
                _atomic_json_write_sync, self.log_file, {"entries": entries}
            )

    async def clear_log(self):
        async with self._lock:
            await asyncio.to_thread(
                _atomic_json_write_sync, self.log_file, {"entries": []}
            )

    async def record_error(self, error: str):
        await self.patch_state({"last_error": str(error)[:2000]})


def _entry_digest(entry: dict) -> str:
    """Compact one-line representation for the LLM snapshot."""
    eid = str(entry.get("id", "?"))
    scope = entry.get("scope", "global")
    vis = entry.get("visibility", "shared")
    imp = entry.get("importance", 5)
    content = str(entry.get("content", "")).replace("\n", " ")[:200]
    return f"[{eid}] scope={scope} vis={vis} i{imp}: {content}"


class ContextCleanupEngine:
    """Background loop that periodically reviews and cleans shared context.

    Runs two passes per tick: shared_context (scoped facts) and long_term_memory
    (the flat LTM list that Intel/news writes to hourly). The LTM pass exists
    because Intel appends 7-8 dated facts every hour and nothing else
    maintained that list — without an LTM pass, the only cap is MAX_LTM_LINES
    and it just rolls over the oldest entries.
    """

    def __init__(self, bot: Any):
        self.bot = bot
        self.store = ContextCleanupStore(getattr(bot.config, "DATA_DIR", "data"))
        self.enabled = self._default_enabled()
        self.interval_seconds = self._default_interval()
        self.ltm_enabled = self._default_ltm_enabled()
        self._running_flag = False
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._last_audit = ""

    def _default_enabled(self) -> bool:
        return bool(
            (getattr(self.bot, "_control", None) or {}).get(
                "context_cleanup_enabled",
                DEFAULT_CONTROL.get("context_cleanup_enabled", True),
            )
        )

    def _default_interval(self) -> int:
        try:
            return max(
                300,
                int(
                    (getattr(self.bot, "_control", None) or {}).get(
                        "context_cleanup_interval_seconds",
                        DEFAULT_CONTROL.get("context_cleanup_interval_seconds", 1800),
                    )
                    or 1800
                ),
            )
        except (TypeError, ValueError):
            return 1800

    def _default_ltm_enabled(self) -> bool:
        return bool(
            (getattr(self.bot, "_control", None) or {}).get(
                "context_cleanup_ltm_enabled",
                DEFAULT_CONTROL.get("context_cleanup_ltm_enabled", True),
            )
        )

    # -- lifecycle --

    async def start(self):
        """Start the background loop. Safe to call multiple times."""
        # Guard FIRST, before any await, to avoid a double-start leaking a
        # second _loop that stop() can't cancel.
        if self._task is not None and not self._task.done():
            return
        await self.load_control()
        # Clear a stale on-disk "running" flag left by a previous process that
        # died mid-pass; otherwise status() reports running=True after a crash.
        try:
            state = await self.store.load_state()
            if state.get("running"):
                await self.store.patch_state({"running": False, "running_since": ""})
        except Exception as e:
            logger.debug(f"ContextCleanup stale-running clear failed: {e}")
        self._task = asyncio.create_task(self._loop())
        logger.info("ContextCleanupEngine started")

    async def stop(self):
        if self._task:
            await self._cancel_task(self._task)
        self._task = None
        logger.info("ContextCleanupEngine stopped")

    @staticmethod
    async def _cancel_task(task: asyncio.Task):
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def load_control(self):
        try:
            control = await self.store.load_control()
            self.enabled = bool(control.get("enabled", self._default_enabled()))
            self.interval_seconds = max(
                300,
                int(control.get("interval_seconds", self._default_interval()) or 1800),
            )
            self.ltm_enabled = bool(
                control.get("ltm_enabled", self._default_ltm_enabled())
            )
        except Exception as e:
            logger.warning(f"ContextCleanup load_control failed: {e}")

    async def save_control(self):
        await self.store.save_control(
            {
                "enabled": self.enabled,
                "interval_seconds": self.interval_seconds,
                "ltm_enabled": self.ltm_enabled,
            }
        )

    # -- main loop --

    async def _loop(self):
        MAX_INTERVAL = 86400
        consecutive_failures = 0
        while True:
            try:
                await self.load_control()
                if self.enabled:
                    result = await self.run_once()
                    if result.get("error"):
                        consecutive_failures += 1
                    elif not result.get("skipped"):
                        consecutive_failures = 0
                else:
                    # Reset backoff while disabled so re-enabling after a run
                    # of failures doesn't delay the first run by up to 6x.
                    consecutive_failures = 0
            except asyncio.CancelledError as _exc:
                raise
            except Exception as e:
                consecutive_failures += 1
                logger.error(f"ContextCleanup loop error: {e}", exc_info=True)
                with contextlib.suppress(Exception):
                    await self.store.record_error(str(e))
            interval = max(60, min(self.interval_seconds, MAX_INTERVAL))
            # Cap the exponent first (avoid huge 2**N); max 6x backoff.
            backoff = (
                min(1 << min(consecutive_failures, 3), 6)
                if consecutive_failures > 0
                else 1
            )
            await asyncio.sleep(max(60, interval * backoff))

    # -- single pass --

    async def run_once(self) -> dict:
        """One cleanup pass over shared_context AND long_term_memory.

        Skipped if a previous pass is still running. Both sub-passes share
        the same lock and the same audit/log entry; their per-store
        counters are reported in the result.
        """
        if self._lock.locked():
            logger.debug("ContextCleanup pass skipped — previous still running")
            return {"skipped": True}
        acquired = False
        try:
            await asyncio.wait_for(self._lock.acquire(), timeout=600)
            acquired = True
        except asyncio.TimeoutError as _exc:
            logger.error(
                "ContextCleanup lock timed out — previous pass hung for >10m, forcing release"
            )
            return {"skipped": False, "error": "lock timeout"}
        try:
            self._running_flag = True
            await self.store.patch_state(
                {"running": True, "running_since": _utcnow_iso()}
            )
            started = _utcnow_iso()
            start = time.time()
            try:
                memory = cast(Any, getattr(self.bot, "memory", None))
                if memory is None:
                    raise RuntimeError("memory manager unavailable")

                sc_audit = ""
                sc_applied = 0
                sc_skipped = 0
                ltm_audit = ""
                ltm_applied = 0
                ltm_skipped = 0
                ltm_skipped_disabled = False

                # --- shared_context pass ---
                if hasattr(memory, "list_shared_context"):
                    entries = await memory.list_shared_context(limit=MAX_REVIEW_ENTRIES)
                    if not entries:
                        sc_audit = "shared_context: empty"
                    else:
                        sc_plan, sc_audit = await self.plan(entries)
                        sc_applied, sc_skipped = await self.apply(sc_plan)

                # --- long_term_memory pass ---
                if self.ltm_enabled and hasattr(memory, "get_long_term_memory"):
                    ltm_entries = await asyncio.to_thread(memory.get_long_term_memory)
                    if not ltm_entries:
                        ltm_audit = "ltm: empty"
                    else:
                        ltm_plan, ltm_audit = await self.plan_ltm(ltm_entries)
                        ltm_applied, ltm_skipped = await self.apply_ltm(ltm_plan)
                elif not self.ltm_enabled:
                    ltm_audit = "ltm: skipped (disabled)"
                    ltm_skipped_disabled = True

                duration = time.time() - start
                combined_audit = (
                    f"shared_context: {sc_audit} | ltm: {ltm_audit}"
                ).strip(" |")
                total_applied = sc_applied + ltm_applied
                total_skipped = sc_skipped + ltm_skipped
                await self._finish_pass(
                    started,
                    duration,
                    combined_audit,
                    total_applied,
                    total_skipped,
                    None,
                    sc_applied=sc_applied,
                    sc_skipped=sc_skipped,
                    ltm_applied=ltm_applied,
                    ltm_skipped=ltm_skipped,
                    ltm_disabled=ltm_skipped_disabled,
                )
                return {
                    "skipped": False,
                    "ops": total_applied,
                    "skipped_ops": total_skipped,
                    "sc_ops": sc_applied,
                    "ltm_ops": ltm_applied,
                    "audit": combined_audit,
                    "duration": duration,
                }
            except Exception as e:
                logger.error(f"ContextCleanup pass failed: {e}")
                duration = time.time() - start
                await self._finish_pass(started, duration, f"ERROR: {e}", 0, 0, str(e))
                return {"skipped": False, "error": str(e), "duration": duration}
            finally:
                self._running_flag = False
        finally:
            if acquired:
                self._lock.release()

    async def _finish_pass(
        self,
        started_iso: str,
        duration: float,
        audit: str,
        applied: int,
        skipped: int,
        error: str | None,
        sc_applied: int = 0,
        sc_skipped: int = 0,
        ltm_applied: int = 0,
        ltm_skipped: int = 0,
        ltm_disabled: bool = False,
    ):
        self._last_audit = str(audit)[:4000]

        def _update(s):
            s["last_run"] = started_iso
            s["last_duration"] = round(duration, 2)
            s["last_audit"] = str(audit)[:4000]
            s["ops_applied_total"] = _safe_int(s.get("ops_applied_total", 0)) + applied
            s["ops_skipped_total"] = _safe_int(s.get("ops_skipped_total", 0)) + skipped
            s["sc_ops_applied_total"] = (
                _safe_int(s.get("sc_ops_applied_total", 0)) + sc_applied
            )
            s["ltm_ops_applied_total"] = (
                _safe_int(s.get("ltm_ops_applied_total", 0)) + ltm_applied
            )
            s["ltm_disabled"] = bool(ltm_disabled)
            s["passes_total"] = _safe_int(s.get("passes_total", 0)) + 1
            s["last_error"] = error

        await self.store.update_state(_update)
        await self.store.append_log_entry(
            {
                "id": f"pass_{uuid.uuid4().hex[:8]}",
                "timestamp": _utcnow_iso(),
                "duration": round(duration, 2),
                "ops_applied": applied,
                "ops_skipped": skipped,
                "sc_ops": sc_applied,
                "ltm_ops": ltm_applied,
                "ltm_disabled": bool(ltm_disabled),
                "audit": str(audit)[:2000],
                "error": error,
            }
        )
        with contextlib.suppress(Exception):
            await self.store.patch_state({"running": False, "running_since": ""})

    # -- planning --

    async def plan(self, entries: list[dict]) -> tuple[list[dict], str]:
        """Ask the LLM for a batch of cleanup ops over the current snapshot."""
        snapshot = "\n".join(_entry_digest(e) for e in entries[:MAX_REVIEW_ENTRIES])
        snapshot = _truncate(snapshot, 16000)

        system_prompt = (
            "You are Maxwell's shared-context janitor. You review a list of stored facts "
            "and produce cleanup operations: delete duplicates/garbage, edit messy entries, "
            "merge near-duplicates into one clean entry, or add a missing consolidated fact.\n\n"
            "RULES:\n"
            "- Only DELETE an entry if it is an exact/near duplicate of another, obviously "
            "garbage (truncated, nonsensical, leaked prompt text, raw timestamps), or "
            "superseded by a newer entry with the same scope.\n"
            "- EDIT to clean up phrasing, fix scope/visibility/importance, or remove junk "
            "appended to an otherwise useful fact. Never change the meaning.\n"
            "- MERGE two or more near-duplicate entries into one: provide keep_id + the new "
            "content; the others are deleted automatically. Use sparingly.\n"
            "- ADD only if a genuinely new consolidated fact is missing. Do not re-add "
            "something you're also deleting.\n"
            "- Preserve identity, preference, and operational facts. Never delete secrets "
            "handling — just leave them.\n"
            f"- At most {MAX_OPS_PER_PASS} operations. Prefer edits over deletes when unsure.\n\n"
            "Return ONLY strict JSON:\n"
            "{\n"
            '  "audit": "short summary of what you cleaned and why",\n'
            '  "ops": [\n'
            '    {"kind":"delete","id":"<entry id>","reason":"..."},\n'
            '    {"kind":"edit","id":"<entry id>","content":"...","importance":1-10,'
            '"scope":"global|user:<id>|guild:<id>|channel:<id>|dm:<id>","visibility":'
            '"shared|private|admin_only|public_hint","reason":"..."},\n'
            '    {"kind":"merge","keep_id":"<id>","delete_ids":["<id>","<id>"],'
            '"content":"...","importance":1-10,"reason":"..."},\n'
            '    {"kind":"add","content":"...","scope":"...","importance":1-10,'
            '"visibility":"...","reason":"..."}\n'
            "  ]\n"
            "}\n"
            "Valid kinds: delete, edit, merge, add. Do not invent others."
        )

        user_prompt = (
            f"Current shared-context snapshot ({len(entries)} entries, max {MAX_REVIEW_ENTRIES}):\n"
            f"{snapshot}\n\nReturn the cleanup plan JSON."
        )

        try:
            provider = await self.bot._get_autonomy_provider()
            # We only ever call generate_response; drop the misleading
            # generate_chat_completion check (it was never dispatched to). If the
            # resolved provider lacks generate_response, fall back to the main
            # ai_provider, which has both methods.
            if provider is None or not callable(
                getattr(provider, "generate_response", None)
            ):
                provider = getattr(self.bot, "ai_provider", None)
            if provider is None:
                return [], "DONE - no provider available"
            # Provider-unavailable soft skip: don't burn an AI slot or count this
            # as a failure — _get_autonomy_provider re-probes init next tick.
            if getattr(provider, "available", None) == False:  # noqa: E712
                logger.info("ContextCleanup: provider not available, soft skip")
                return [], "DONE - provider not available"
            model = (
                str(
                    (getattr(self.bot, "_control", None) or {}).get(
                        "autonomy_model", ""
                    )
                    or ""
                )
                or None
            )
            timeout = max(
                30,
                min(
                    int(
                        (getattr(self.bot, "_control", None) or {}).get(
                            "ai_timeout_seconds", 120
                        )
                        or 120
                    ),
                    600,
                ),
            )
            await self.bot._acquire_ai_slot(timeout=timeout)
            try:
                raw = await provider.generate_response(
                    [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    timeout=timeout,
                    model=model,
                    max_tokens=4096,
                )
            finally:
                await self.bot._release_ai_slot()
        except Exception as e:
            logger.error(f"ContextCleanup LLM call failed: {e}")
            return [], f"ERROR - LLM call failed: {e}"

        ops, audit = self._parse_plan(raw, entries)
        return ops, audit

    def _parse_plan(self, raw: str, entries: list[dict]) -> tuple[list[dict], str]:
        """Extract and validate the cleanup plan from LLM output."""
        if not raw:
            return [], "DONE - empty LLM response"
        text = str(raw).strip()

        json_str: str | None = None
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                json_str = text
        except (json.JSONDecodeError, ValueError):
            pass
        if json_str is None:
            m = re.search(r"```(?:json)?\s*\n?(\{[^`]*)\s*```", text, re.DOTALL)
            if m:
                json_str = m.group(1)
        if json_str is None:
            candidates = []
            i = 0
            while i < len(text):
                if text[i] == "{":
                    depth = 0
                    for j in range(i, len(text)):
                        if text[j] == "{":
                            depth += 1
                        elif text[j] == "}":
                            depth -= 1
                            if depth == 0:
                                candidates.append(text[i : j + 1])
                                i = j
                                break
                i += 1
            for c in candidates:
                try:
                    obj = json.loads(c)
                    if isinstance(obj, dict) and "ops" in obj:
                        json_str = c
                        break
                except json.JSONDecodeError as _exc:
                    pass
            if json_str is None and candidates:
                json_str = candidates[0]
        if json_str is None:
            logger.warning(f"ContextCleanup no JSON. Raw: {text[:500]}")
            return [], "DONE - no JSON in LLM response"

        try:
            parsed = json.loads(json_str)
        except json.JSONDecodeError as e:
            logger.warning(
                f"ContextCleanup JSON parse failed: {e}. Raw: {json_str[:500]}"
            )
            return [], "DONE - invalid JSON from planner"
        if not isinstance(parsed, dict):
            return [], "DONE - planner returned non-object"

        audit = str(parsed.get("audit", ""))[:2000]
        raw_ops = parsed.get("ops", [])
        if not isinstance(raw_ops, list):
            return [], audit or "DONE"

        known_ids = {str(e.get("id", "")) for e in entries if e.get("id")}
        valid: list[dict] = []
        for op in raw_ops[:MAX_OPS_PER_PASS]:
            if not isinstance(op, dict):
                continue
            kind = str(op.get("kind", "")).strip().lower()
            if kind not in VALID_OP_KINDS:
                continue
            reason = str(op.get("reason", ""))[:300]
            if kind == "delete":
                eid = _strip_id(op.get("id"))
                if eid and eid in known_ids:
                    valid.append({"kind": "delete", "id": eid, "reason": reason})
            elif kind == "edit":
                eid = _strip_id(op.get("id"))
                if not eid or eid not in known_ids:
                    continue
                updates: dict[str, Any] = {"reason": reason}
                if op.get("content"):
                    content = str(op.get("content")).strip()[:MAX_CONTENT_CHARS]
                    if content:
                        updates["content"] = content
                if op.get("importance") is not None:
                    updates["importance"] = _coerce_importance(op.get("importance"))
                if op.get("scope"):
                    updates["scope"] = _coerce_scope(op.get("scope"))
                if op.get("visibility"):
                    updates["visibility"] = _coerce_visibility(op.get("visibility"))
                if len(updates) > 1:
                    valid.append({"kind": "edit", "id": eid, "updates": updates})
            elif kind == "merge":
                keep_id = _strip_id(op.get("keep_id"))
                delete_ids_raw = op.get("delete_ids", [])
                if not isinstance(delete_ids_raw, list):
                    delete_ids_raw = []
                delete_ids = [d for d in (_strip_id(x) for x in delete_ids_raw) if d]
                delete_ids = [d for d in delete_ids if d in known_ids and d != keep_id]
                content = str(op.get("content", "")).strip()[:MAX_CONTENT_CHARS]
                if (
                    not keep_id
                    or keep_id not in known_ids
                    or not delete_ids
                    or not content
                ):
                    continue
                valid.append(
                    {
                        "kind": "merge",
                        "keep_id": keep_id,
                        "delete_ids": delete_ids,
                        "content": content,
                        "importance": _coerce_importance(op.get("importance"), 5),
                        "reason": reason,
                    }
                )
            elif kind == "add":
                content = str(op.get("content", "")).strip()[:MAX_CONTENT_CHARS]
                if not content:
                    continue
                valid.append(
                    {
                        "kind": "add",
                        "content": content,
                        "scope": _coerce_scope(op.get("scope"), "global"),
                        "visibility": _coerce_visibility(
                            op.get("visibility"), "shared"
                        ),
                        "importance": _coerce_importance(op.get("importance"), 5),
                        "reason": reason,
                    }
                )
        return valid, audit or "DONE"

    # -- applying --

    async def apply(self, plan: list[dict]) -> tuple[int, int]:
        """Apply cleanup ops through the memory manager. Returns (applied, skipped)."""
        memory = cast(Any, getattr(self.bot, "memory", None))
        if memory is None:
            return 0, len(plan)
        applied = 0
        skipped = 0
        for op in plan[:MAX_OPS_PER_PASS]:
            kind = op.get("kind")
            try:
                if kind == "delete":
                    ok = await memory.remove_shared_context(op["id"])
                elif kind == "edit":
                    ok = await memory.update_shared_context(op["id"], op["updates"])
                elif kind == "merge":
                    ok = await memory.update_shared_context(
                        op["keep_id"],
                        {
                            "content": op["content"],
                            "importance": op["importance"],
                        },
                    )
                    if ok:
                        for did in op["delete_ids"]:
                            with contextlib.suppress(Exception):
                                await memory.remove_shared_context(did)
                elif kind == "add":
                    new_id = await memory.add_shared_context(
                        {
                            "scope": op["scope"],
                            "visibility": op["visibility"],
                            "importance": op["importance"],
                            "content": op["content"],
                            "tags": ["cleanup"],
                        }
                    )
                    ok = bool(new_id)
                else:
                    ok = False
                if ok:
                    applied += 1
                else:
                    skipped += 1
            except Exception as e:
                logger.warning(f"ContextCleanup op {kind} failed: {e}")
                skipped += 1
        return applied, skipped

    # -- LTM pass (long_term_memory) --

    def _ltm_digest(self, entry: dict) -> str:
        eid = str(entry.get("id", "?"))
        content = str(entry.get("content", "")).replace("\n", " ")[:200]
        return f"[{eid}] {content}"

    async def plan_ltm(self, entries: list[dict]) -> tuple[list[dict], str]:
        """Ask the LLM to clean the long_term_memory store.

        LTM is a flat list of dated facts (Intel/news mostly). Cleanup is
        delete/edit/merge; "add" is intentionally not allowed here because
        nothing else writes to LTM except Intel and the bot itself, and
        cleanup shouldn't grow the list.
        """
        if not entries:
            return [], "ltm: empty"
        # Cap snapshot size. LTM can be 1000+; send the freshest slice that
        # fits the budget — Intel's recent facts are at the tail.
        budget = 14000
        tail = entries[-MAX_REVIEW_ENTRIES:]
        snapshot = "\n".join(self._ltm_digest(e) for e in tail)
        snapshot = _truncate(snapshot, budget)
        system_prompt = (
            "You are Maxwell's long-term-memory janitor. You review a list of "
            "stored facts (mostly news/intel entries the bot accumulated) and "
            "produce cleanup operations: delete duplicates/garbage, edit messy "
            "entries, or merge near-duplicates into one clean entry.\n\n"
            "RULES:\n"
            "- Only DELETE an entry if it is an exact/near duplicate of another, "
            "obviously garbage (truncated, leaked prompt text, raw timestamps), "
            "or superseded by a newer entry on the same topic.\n"
            "- EDIT to clean phrasing, fix typos, normalize dates, or strip "
            "leading '[2026-01-01]' style prefixes that are not useful. Never "
            "change the meaning.\n"
            "- MERGE two or more near-duplicate entries into one: keep the "
            "newest/widest-scoped one, provide a single clean merged content, "
            "and list the others in delete_ids.\n"
            f"- At most {MAX_OPS_PER_PASS} operations. Prefer edits over deletes.\n\n"
            "Return ONLY strict JSON:\n"
            "{\n"
            '  "audit": "short summary of what you cleaned and why",\n'
            '  "ops": [\n'
            '    {"kind":"delete","id":"<entry id>","reason":"..."},\n'
            '    {"kind":"edit","id":"<entry id>","content":"...","reason":"..."},\n'
            '    {"kind":"merge","keep_id":"<id>","delete_ids":["<id>","<id>"],'
            '"content":"...","reason":"..."}\n'
            "  ]\n"
            "}\n"
            "Valid kinds: delete, edit, merge. Do not add entries — Intel "
            "and the bot itself manage additions; cleanup only removes/merges."
        )
        user_prompt = (
            f"Long-term-memory snapshot ({len(entries)} entries total, showing "
            f"newest {len(tail)}):\n{snapshot}\n\nReturn the cleanup plan JSON."
        )
        try:
            provider = await self.bot._get_autonomy_provider()
            if provider is None or not callable(
                getattr(provider, "generate_response", None)
            ):
                provider = getattr(self.bot, "ai_provider", None)
            if provider is None:
                return [], "ltm: no provider available"
            if getattr(provider, "available", None) == False:  # noqa: E712
                logger.info("ContextCleanup: provider not available for LTM, soft skip")
                return [], "ltm: provider not available"
            model = (
                str(
                    (getattr(self.bot, "_control", None) or {}).get(
                        "autonomy_model", ""
                    )
                    or ""
                )
                or None
            )
            timeout = max(
                30,
                min(
                    int(
                        (getattr(self.bot, "_control", None) or {}).get(
                            "ai_timeout_seconds", 120
                        )
                        or 120
                    ),
                    600,
                ),
            )
            await self.bot._acquire_ai_slot(timeout=timeout)
            try:
                raw = await provider.generate_response(
                    [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    timeout=timeout,
                    model=model,
                    max_tokens=4096,
                )
            finally:
                await self.bot._release_ai_slot()
        except Exception as e:
            logger.error(f"ContextCleanup LTM LLM call failed: {e}")
            return [], f"ltm ERROR: {e}"

        ops, audit = self._parse_ltm_plan(raw, entries)
        return ops, audit

    def _parse_ltm_plan(self, raw: str, entries: list[dict]) -> tuple[list[dict], str]:
        if not raw:
            return [], "ltm: empty LLM response"
        text = str(raw).strip()
        json_str: str | None = None
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                json_str = text
        except (json.JSONDecodeError, ValueError):
            pass
        if json_str is None:
            m = re.search(r"```(?:json)?\s*\n?(\{[^`]*)\s*```", text, re.DOTALL)
            if m:
                json_str = m.group(1)
        if json_str is None:
            candidates = []
            i = 0
            while i < len(text):
                if text[i] == "{":
                    depth = 0
                    for j in range(i, len(text)):
                        if text[j] == "{":
                            depth += 1
                        elif text[j] == "}":
                            depth -= 1
                            if depth == 0:
                                candidates.append(text[i : j + 1])
                                i = j
                                break
                i += 1
            for c in candidates:
                try:
                    obj = json.loads(c)
                    if isinstance(obj, dict) and "ops" in obj:
                        json_str = c
                        break
                except json.JSONDecodeError as _exc:
                    pass
            if json_str is None and candidates:
                json_str = candidates[0]
        if json_str is None:
            logger.warning(f"ContextCleanup LTM no JSON. Raw: {text[:500]}")
            return [], "ltm: no JSON in LLM response"
        try:
            parsed = json.loads(json_str)
        except json.JSONDecodeError as e:
            logger.warning(
                f"ContextCleanup LTM JSON parse failed: {e}. Raw: {json_str[:500]}"
            )
            return [], "ltm: invalid JSON from planner"
        if not isinstance(parsed, dict):
            return [], "ltm: planner returned non-object"

        audit = str(parsed.get("audit", ""))[:2000]
        raw_ops = parsed.get("ops", [])
        if not isinstance(raw_ops, list):
            return [], audit or "ltm: DONE"

        known_ids = {str(e.get("id", "")) for e in entries if e.get("id") is not None}
        valid: list[dict] = []
        for op in raw_ops[:MAX_OPS_PER_PASS]:
            if not isinstance(op, dict):
                continue
            kind = str(op.get("kind", "")).strip().lower()
            reason = str(op.get("reason", ""))[:300]
            if kind == "delete":
                eid = str(op.get("id", "")).strip()
                if eid and eid in known_ids:
                    valid.append({"kind": "delete", "id": eid, "reason": reason})
            elif kind == "edit":
                eid = str(op.get("id", "")).strip()
                if not eid or eid not in known_ids:
                    continue
                content = str(op.get("content", "")).strip()[:MAX_CONTENT_CHARS]
                if not content:
                    continue
                valid.append(
                    {"kind": "edit", "id": eid, "content": content, "reason": reason}
                )
            elif kind == "merge":
                keep_id = str(op.get("keep_id", "")).strip()
                delete_ids_raw = op.get("delete_ids", [])
                if not isinstance(delete_ids_raw, list):
                    delete_ids_raw = []
                delete_ids = [d for d in (str(x).strip() for x in delete_ids_raw) if d]
                delete_ids = [d for d in delete_ids if d in known_ids and d != keep_id]
                content = str(op.get("content", "")).strip()[:MAX_CONTENT_CHARS]
                if (
                    not keep_id
                    or keep_id not in known_ids
                    or not delete_ids
                    or not content
                ):
                    continue
                valid.append(
                    {
                        "kind": "merge",
                        "keep_id": keep_id,
                        "delete_ids": delete_ids,
                        "content": content,
                        "reason": reason,
                    }
                )
        return valid, audit or "ltm: DONE"

    async def apply_ltm(self, plan: list[dict]) -> tuple[int, int]:
        memory = cast(Any, getattr(self.bot, "memory", None))
        if memory is None:
            return 0, len(plan)
        # Flatten the plan into a single batch of edits + deletes and apply it
        # in ONE locked pass. Applying ops one-by-one used to renumber every
        # LTM entry (to positional 1..N) after each save, so the 2nd+ ops
        # targeted the wrong ids and silently corrupted memory. The batch
        # method resolves all ids against the freshly-reloaded list and
        # renumbers exactly once at the end.
        edit_map: dict[str, str] = {}
        delete_ids: set[str] = set()
        applied = 0
        skipped = 0
        for op in plan[:MAX_OPS_PER_PASS]:
            kind = op.get("kind")
            try:
                if kind == "delete":
                    did = str(op.get("id"))
                    if did and did not in delete_ids:
                        delete_ids.add(did)
                        applied += 1
                    else:
                        skipped += 1
                elif kind == "edit":
                    mid = str(op.get("id"))
                    content = op.get("content")
                    if mid and content is not None:
                        edit_map[mid] = content
                        applied += 1
                    else:
                        skipped += 1
                elif kind == "merge":
                    keep_id = str(op.get("keep_id"))
                    content = op.get("content")
                    if keep_id and content is not None:
                        edit_map[keep_id] = content
                        applied += 1
                    else:
                        skipped += 1
                    for did in op.get("delete_ids", []) or []:
                        did = str(did)
                        if did and did != keep_id:
                            delete_ids.add(did)
                else:
                    skipped += 1
            except Exception as e:
                logger.warning(f"ContextCleanup LTM op {kind} parse failed: {e}")
                skipped += 1
        try:
            await memory.apply_ltm_batch(edits=edit_map, deletes=delete_ids)
        except Exception as e:
            logger.warning(f"ContextCleanup LTM batch apply failed: {e}")
            return 0, applied + skipped
        return applied, skipped

    # -- status for API --

    async def status(self) -> dict:
        state = await self.store.load_state()
        log = await self.store.load_log()
        return {
            "enabled": self.enabled,
            "ltm_enabled": self.ltm_enabled,
            "interval_seconds": self.interval_seconds,
            "running": self._running_flag or bool(state.get("running")),
            "last_run": state.get("last_run", ""),
            "last_duration": state.get("last_duration"),
            "last_audit": str(state.get("last_audit") or self._last_audit or "")[:4000],
            "last_error": state.get("last_error"),
            "ops_applied_total": state.get("ops_applied_total", 0),
            "ops_skipped_total": state.get("ops_skipped_total", 0),
            "sc_ops_applied_total": state.get("sc_ops_applied_total", 0),
            "ltm_ops_applied_total": state.get("ltm_ops_applied_total", 0),
            "ltm_disabled": bool(state.get("ltm_disabled", False)),
            "passes_total": state.get("passes_total", 0),
            "log": log[-20:],
        }
