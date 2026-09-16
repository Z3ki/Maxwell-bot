"""Background sub-agent jobs for long tasks (sites, research, images, code).

Long work runs outside the live ReplyQueue turn so one expensive task does not
lock a Discord room. Workers get larger time/output budgets, but their prompt
history is deliberately bounded: a long job should spend tokens on the next
useful action, not on repeatedly re-reading its own transcript.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import secrets
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from identity import process_name
from tools import Tool
from utils import _safe_int, _spawn_background

logger = logging.getLogger(__name__)

JOB_ID_BYTES = 4

BG_MAX_TOKENS_DEFAULT_FLOOR = 32768
BG_MAX_TOKENS_HARD_CAP = 131072
BG_TIMEOUT_DEFAULT = 7200
BG_TIMEOUT_HARD_CAP = 14400
BG_ITERS_DEFAULT = 100
BG_ITERS_HARD_CAP = 200

# Input-context guardrails are separate from output-token budgets. A generous
# output ceiling is useful for site/code tool arguments; retaining every old
# tool result is not. Keep enough recent work for continuity and summarize the
# rest into a small ledger.
BG_CONTEXT_CHARS_DEFAULT = 48000
BG_CONTEXT_CHARS_HARD_CAP = 120000
BG_CONTEXT_CHARS_MIN = 8000
BG_CONTEXT_KEEP_RECENT = 12
BG_CONTEXT_DIGEST_CHARS = 6000
_COMPACTED_LEDGER_PREFIX = (
    "=== COMPACTED PRIOR WORK ===\n"
    "Older tool chatter was compressed to save context. Treat this "
    "as a work ledger; do not redo completed work unless verification "
    "is necessary.\n"
)

# Circuit breakers. One bad tool call should be recoverable; a loop that keeps
# doing the exact same thing or throwing the same dispatch class is not useful.
BG_DISPATCH_ERROR_LIMIT = 3
BG_REPEAT_NUDGE_AT = 3
BG_REPEAT_ABORT_AT = 5
BG_STALL_NUDGE_STEPS = 10
BG_STALL_ABORT_STEPS = 30

BG_MAX_JOBS_DEFAULT = 2
BG_MAX_PER_USER_DEFAULT = 1

_NO_RECURSE_TOOL = "spawn_background"
_WORKER_HIDDEN_TOOLS = frozenset({_NO_RECURSE_TOOL, "send_message"})


def _worker_tools(openai_tools: Any) -> list[dict[str, Any]]:
    """Background-worker tool catalog: full tools minus spawner and channel post."""
    return [
        t
        for t in (openai_tools or [])
        if (t.get("function") or {}).get("name") not in _WORKER_HIDDEN_TOOLS
    ]


BG_MODEL_DEFAULT = "gemini-3.8-flash-high"


def resolve_job_model(control: Any) -> str:
    """LLM model for workers: control bg_model > env BG_MODEL > default."""
    control = control or {}
    raw = str(control.get("bg_model", "") or "").strip()
    if not raw:
        raw = str(os.getenv("BG_MODEL", "") or "").strip()
    return raw or BG_MODEL_DEFAULT


def resolve_job_budgets(control: Any, config: Any) -> dict[str, int]:
    """Extended output/timeout/iteration budgets with hard safety caps."""
    control = control or {}
    live_max_tokens = (
        _safe_int(getattr(config, "OLLAMA_MAX_TOKENS", 16384) or 16384, 16384)
        if config is not None
        else 16384
    )
    default_tokens = max(live_max_tokens * 2, BG_MAX_TOKENS_DEFAULT_FLOOR)

    def _pick(control_key: str, env_key: str, default: int, cap: int) -> int:
        raw = control.get(control_key, None)
        if raw is None:
            raw = os.getenv(env_key, "")
        text = str(raw or "").strip()
        if text in ("", "0"):
            text = str(os.getenv(env_key, "") or "").strip()
        if text in ("", "0"):
            value = default
        else:
            try:
                value = int(text)
            except (TypeError, ValueError):
                value = default
        return max(1, min(int(value), cap))

    return {
        "max_tokens": _pick(
            "bg_max_tokens", "BG_MAX_TOKENS", default_tokens, BG_MAX_TOKENS_HARD_CAP
        ),
        "timeout_seconds": _pick(
            "bg_timeout_seconds",
            "BG_TIMEOUT_SECONDS",
            BG_TIMEOUT_DEFAULT,
            BG_TIMEOUT_HARD_CAP,
        ),
        "max_iters": _pick(
            "bg_max_iters", "BG_MAX_ITERS", BG_ITERS_DEFAULT, BG_ITERS_HARD_CAP
        ),
    }


def resolve_job_context_chars(control: Any = None) -> int:
    """Maximum approximate characters retained in a worker request history.

    ``bg_context_chars`` is accepted for forward-compatible control payloads;
    deployments can use ``BG_CONTEXT_CHARS`` today without requiring a control
    schema migration.
    """
    control = control or {}
    raw = control.get("bg_context_chars", None)
    if raw in (None, "", 0, "0"):
        raw = os.getenv("BG_CONTEXT_CHARS", "")
    try:
        value = int(str(raw).strip()) if str(raw or "").strip() else BG_CONTEXT_CHARS_DEFAULT
    except (TypeError, ValueError):
        value = BG_CONTEXT_CHARS_DEFAULT
    return max(BG_CONTEXT_CHARS_MIN, min(value, BG_CONTEXT_CHARS_HARD_CAP))


def _short(text: Any, limit: int = 50) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()[:limit] or "project"


def _norm_jid(job_id: Any) -> str:
    return str(job_id or "").strip().lower()


def _message_text(message: Any) -> str:
    if not isinstance(message, dict):
        return str(message or "")
    content = message.get("content", "")
    if isinstance(content, str):
        text = content
    else:
        try:
            text = json.dumps(content, ensure_ascii=False, default=str)
        except Exception:
            text = str(content or "")
    # Tool-call metadata can dwarf an empty assistant content field, so count it
    # too when deciding whether to compact.
    for key in ("tool_calls", "function_call"):
        if message.get(key):
            try:
                text += "\n" + json.dumps(
                    message.get(key), ensure_ascii=False, default=str, sort_keys=True
                )
            except Exception:
                text += "\n" + str(message.get(key))
    return text


def _message_chars(message: Any) -> int:
    return len(_message_text(message)) + 32


def _history_digest(messages: list[dict[str, Any]], limit: int = BG_CONTEXT_DIGEST_CHARS) -> str:
    """Compact old worker history into a deterministic, non-LLM work ledger."""
    if not messages:
        return ""
    lines: list[str] = []
    # Newer dropped work is more useful. Walk backwards, then restore order.
    for message in reversed(messages[-32:]):
        role = str(message.get("role") or "event")
        text = re.sub(r"\s+", " ", _message_text(message)).strip()
        if not text:
            continue
        lines.append(f"{role}: {text[:260]}")
        if sum(len(x) + 1 for x in lines) >= limit:
            break
    lines.reverse()
    return "\n".join(lines)[-limit:]


def _safe_tail_start(messages: list[dict[str, Any]], start: int) -> int:
    """Never start a retained tail on a native ``tool`` result.

    OpenAI-style providers require that tool results follow the assistant
    message that declared their tool_call_id. Back up to that assistant entry
    when compaction would otherwise split the pair.
    """
    start = max(2, min(start, len(messages)))
    while start > 2 and start < len(messages):
        role = str((messages[start] or {}).get("role") or "")
        if role != "tool":
            break
        start -= 1
    return start


def _compact_worker_messages(
    messages: list[dict[str, Any]],
    *,
    max_chars: int = BG_CONTEXT_CHARS_DEFAULT,
    keep_recent: int = BG_CONTEXT_KEEP_RECENT,
) -> list[dict[str, Any]]:
    """Bound worker prompt growth while preserving system/goal and recent work."""
    max_chars = max(BG_CONTEXT_CHARS_MIN, int(max_chars or BG_CONTEXT_CHARS_DEFAULT))
    if len(messages) <= 2 or sum(_message_chars(m) for m in messages) <= max_chars:
        return messages

    anchors = list(messages[:2])
    recent_start = max(2, len(messages) - max(2, int(keep_recent)))
    recent_start = _safe_tail_start(messages, recent_start)
    tail = list(messages[recent_start:])

    # If the recent tail alone is too large, progressively drop whole oldest
    # entries, still respecting tool-result pairing.
    anchor_chars = sum(_message_chars(m) for m in anchors)
    tail_budget = max(BG_CONTEXT_CHARS_MIN // 2, max_chars - anchor_chars - 1200)
    while len(tail) > 2 and sum(_message_chars(m) for m in tail) > tail_budget:
        drop = 1
        # Dropping an assistant tool-call declaration but leaving its tool result
        # is invalid, so drop the result(s) with it as one transaction.
        if tail[0].get("role") == "assistant" and tail[0].get("tool_calls"):
            drop = 1
            while drop < len(tail) and tail[drop].get("role") == "tool":
                drop += 1
        elif tail[0].get("role") == "tool":
            drop = 1
            while drop < len(tail) and tail[drop].get("role") == "tool":
                drop += 1
        tail = tail[drop:]

    dropped_end = len(messages) - len(tail)
    dropped = list(messages[2:dropped_end])
    remaining = max_chars - anchor_chars - sum(_message_chars(m) for m in tail)
    digest_limit = min(
        BG_CONTEXT_DIGEST_CHARS,
        max(0, remaining - len(_COMPACTED_LEDGER_PREFIX) - 32),
    )
    digest = _history_digest(dropped, limit=digest_limit) if digest_limit else ""
    compacted = anchors
    if digest:
        compacted.append(
            {
                "role": "user",
                "content": _COMPACTED_LEDGER_PREFIX + digest,
            }
        )
    compacted.extend(tail)

    # Last-resort trim of the digest if unusually large anchor/tool metadata
    # still pushes us past the requested bound. Keep the ledger header; drop
    # the oldest digest body so the model still knows this is compacted work.
    total = sum(_message_chars(m) for m in compacted)
    if (
        total > max_chars
        and len(compacted) > 2
        and str(compacted[2].get("content") or "").startswith(
            "=== COMPACTED PRIOR WORK ==="
        )
    ):
        compacted[2]["content"] = _trim_compacted_ledger(
            str(compacted[2].get("content") or ""),
            total - max_chars,
        )
    return compacted


def _trim_compacted_ledger(content: str, over: int) -> str:
    """Drop oldest digest text without slicing off the COMPACTED PRIOR WORK header."""
    prefix = _COMPACTED_LEDGER_PREFIX
    if content.startswith(prefix):
        body = content[len(prefix) :]
    else:
        nl = content.find("\n")
        prefix = content[: nl + 1] if nl >= 0 else "=== COMPACTED PRIOR WORK ===\n"
        body = content[len(prefix) :]
    drop = min(max(0, int(over)), max(0, len(body) - 200))
    body = body[drop:]
    nl = body.find("\n")
    if 0 <= nl < 120:
        body = body[nl + 1 :]
    return prefix + body


def _call_signature(call: Any) -> str:
    """Stable signature for detecting an exact repeated tool call."""
    if not isinstance(call, dict):
        return re.sub(r"\s+", " ", str(call or "")).strip()[:1000]
    fn = call.get("function") if isinstance(call.get("function"), dict) else {}
    name = str(call.get("name") or fn.get("name") or "")
    args = call.get("arguments", fn.get("arguments", {}))
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except Exception:
            args = re.sub(r"\s+", " ", args).strip()
    try:
        arg_text = json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        arg_text = str(args)
    return f"{name}:{arg_text}"[:2000]


def _batch_signature(calls: list[Any]) -> str:
    return "|".join(_call_signature(c) for c in calls)[:6000]


@dataclass
class BackgroundJob:
    id: str
    guild_id: str
    channel_id: str
    user_id: str
    goal: str
    context: str = ""
    status: str = "queued"
    progress: str = ""
    result: str = ""
    thread_id: str = ""
    created_at: float = field(default_factory=time.time)
    finished_at: float = 0.0


class BackgroundJobManager:
    """Track detached jobs; Discord runtime objects remain memory-only."""

    def __init__(
        self,
        data_path: str = "data/background_jobs.json",
        *,
        max_jobs: int | None = None,
        max_per_user: int | None = None,
    ) -> None:
        self.data_path = data_path
        self.max_jobs = max(
            1,
            int(
                max_jobs
                if max_jobs is not None
                else os.getenv("MAXWELL_BG_JOBS", BG_MAX_JOBS_DEFAULT)
                or BG_MAX_JOBS_DEFAULT
            ),
        )
        self.max_per_user = max(
            1,
            int(
                max_per_user
                if max_per_user is not None
                else os.getenv("MAXWELL_BG_PER_USER", BG_MAX_PER_USER_DEFAULT)
                or BG_MAX_PER_USER_DEFAULT
            ),
        )
        self._jobs: dict[str, BackgroundJob] = {}
        self._runtime: dict[str, dict[str, Any]] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self.load()

    def _save(self) -> None:
        try:
            directory = os.path.dirname(self.data_path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            payload = {jid: asdict(job) for jid, job in self._jobs.items()}
            tmp = f"{self.data_path}.tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump({"jobs": payload}, handle)
            os.replace(tmp, self.data_path)
        except Exception as exc:
            logger.warning("background jobs save failed: %s", exc)

    def load(self) -> None:
        try:
            with open(self.data_path, encoding="utf-8") as handle:
                raw = json.load(handle)
        except FileNotFoundError:
            return
        except Exception as exc:
            logger.warning("background jobs load failed (%s); starting empty", exc)
            return
        items = raw.get("jobs") if isinstance(raw, dict) else None
        if not isinstance(items, dict):
            return
        for jid, data in items.items():
            if not isinstance(data, dict):
                continue
            jid = _norm_jid(jid)
            if not jid:
                continue
            try:
                job = BackgroundJob(
                    id=jid,
                    guild_id=str(data.get("guild_id") or ""),
                    channel_id=str(data.get("channel_id") or ""),
                    user_id=str(data.get("user_id") or ""),
                    goal=str(data.get("goal") or "")[:2000],
                    context=str(data.get("context") or "")[:4000],
                    status=str(data.get("status") or "cancelled"),
                    progress=str(data.get("progress") or "")[:2000],
                    result=str(data.get("result") or "")[:8000],
                    thread_id=str(data.get("thread_id") or ""),
                    created_at=float(data.get("created_at") or 0.0),
                    finished_at=float(data.get("finished_at") or 0.0),
                )
            except (TypeError, ValueError):
                continue
            if job.status in ("queued", "running"):
                job.status = "cancelled"
                job.progress = "bot restarted while this job was running"
                job.finished_at = time.time()
            self._jobs[job.id] = job
        if len(self._jobs) > 50:
            ordered = sorted(self._jobs.values(), key=lambda j: j.created_at)
            for stale in ordered[:-50]:
                self._jobs.pop(stale.id, None)
                self.cleanup_runtime(stale.id)

    def active_count(self) -> int:
        return sum(1 for j in self._jobs.values() if j.status in ("queued", "running"))

    def user_active_count(self, user_id: str) -> int:
        uid = str(user_id or "")
        return sum(
            1
            for j in self._jobs.values()
            if j.status in ("queued", "running") and j.user_id == uid
        )

    def create(
        self,
        *,
        guild_id: Any,
        channel_id: Any,
        user_id: Any,
        goal: str,
        context: str = "",
    ) -> BackgroundJob:
        goal = str(goal or "").strip()[:2000]
        if not goal:
            raise ValueError("need a goal for the background job")
        if self.user_active_count(str(user_id)) >= self.max_per_user:
            raise RuntimeError("ALREADY_RUNNING: you already have a background job running")
        if self.active_count() >= self.max_jobs:
            raise RuntimeError("ALL_BUSY: all background slots are busy — try again in a bit")
        jid = _norm_jid(secrets.token_hex(JOB_ID_BYTES))
        while not jid or jid in self._jobs:
            jid = _norm_jid(secrets.token_hex(JOB_ID_BYTES))
        job = BackgroundJob(
            id=jid,
            guild_id=str(guild_id or ""),
            channel_id=str(channel_id or ""),
            user_id=str(user_id or ""),
            goal=goal,
            context=str(context or "")[:4000],
        )
        self._jobs[jid] = job
        self._save()
        return job

    def get(self, job_id: str) -> BackgroundJob | None:
        return self._jobs.get(_norm_jid(job_id))

    def list_text(
        self, limit: int = 10, *, guild_id: Any = None, user_id: Any = None
    ) -> str:
        if not self._jobs:
            return "no background jobs yet."
        gid = str(guild_id or "").strip()
        uid = str(user_id or "").strip()
        jobs = [
            j
            for j in self._jobs.values()
            if (not gid or j.guild_id == gid) and (not uid or j.user_id == uid)
        ]
        if not jobs:
            return "no background jobs in this server yet."
        ordered = sorted(jobs, key=lambda j: j.created_at, reverse=True)[: max(1, limit)]
        lines = []
        for job in ordered:
            age = (
                time.strftime("%H:%M", time.localtime(job.created_at))
                if job.created_at
                else "??:??"
            )
            lines.append(
                f"`{job.id}` [{job.status}] <@{job.user_id}> {_short(job.goal, 60)} ({age})"
            )
        return "\n".join(lines)

    def cleanup_runtime(self, job_id: str) -> None:
        jid = _norm_jid(job_id)
        self._runtime.pop(jid, None)
        self._tasks.pop(jid, None)

    def attach_runtime(self, job_id: str, **objects: Any) -> None:
        self._runtime[_norm_jid(job_id)] = dict(objects)

    def runtime(self, job_id: str) -> dict[str, Any]:
        return self._runtime.get(_norm_jid(job_id), {})

    def track_task(self, job_id: str, task: asyncio.Task) -> None:
        jid = _norm_jid(job_id)
        self._tasks[jid] = task

        def finished(done: asyncio.Task) -> None:
            job = self.get(jid)
            if done.cancelled():
                if job is not None and job.status in ("queued", "running"):
                    self.mark(jid, status="cancelled", progress="cancelled on request")
            else:
                error = done.exception()
                if error is not None and job is not None and job.status in (
                    "queued",
                    "running",
                ):
                    self.mark(jid, status="error", progress=str(error)[:500])
            if self._tasks.get(jid) is done:
                self.cleanup_runtime(jid)

        task.add_done_callback(finished)

    def mark(self, job_id: str, **fields: Any) -> BackgroundJob | None:
        job = self.get(job_id)
        if job is None:
            return None
        for key, value in fields.items():
            if hasattr(job, key):
                setattr(job, key, value)
        if fields.get("status") in ("done", "error", "cancelled") and not job.finished_at:
            job.finished_at = time.time()
        self._save()
        return job

    def cancel(
        self, job_id: str, *, requester_id: Any = None, is_admin: bool = False
    ) -> tuple[bool, str]:
        job = self.get(job_id)
        if job is None:
            return False, "no such job."
        if job.status not in ("queued", "running"):
            return False, f"job `{job.id}` already {job.status}."
        uid = str(requester_id or "")
        if not is_admin and (not uid or uid != job.user_id):
            return False, "only the job owner (or an admin) can cancel it."
        task = self._tasks.get(job.id)
        if task is not None and not task.done():
            task.cancel()
        self.mark(job.id, status="cancelled", progress="cancelled on request")
        return True, f"job `{job.id}` cancelled."

    def stats(self) -> dict[str, Any]:
        return {
            "tracked": len(self._jobs),
            "active": self.active_count(),
            "max_jobs": self.max_jobs,
            "max_per_user": self.max_per_user,
        }


class SpawnBackgroundTool(Tool):
    """Hand a long task to a detached background job and end the live turn."""

    def get_description(self):
        return (
            "BACKGROUND job for any long/multi-step task (site, research, images, code). "
            "Pass a self-contained goal with the deliverable and important constraints; "
            "use context only for essential facts not already in the goal. Do NOT paste "
            "the chat transcript or redundant history. Returns a job id; then send_message "
            "one short ack naming that id and end the turn. Detached, bigger budgets, "
            "replies when done. Params: goal (required), context (optional concise spec)."
        )

    async def execute(
        self,
        message: Any,
        goal: str | None = None,
        context: str | None = None,
        **kwargs: Any,
    ) -> str:
        if getattr(message, "_bg_job", False):
            return (
                "ALREADY INSIDE a background job — do the work inline with normal tools, "
                "do not spawn again."
            )
        bot = getattr(self, "bot", None)
        manager = getattr(bot, "bg_jobs", None) if bot is not None else None
        if manager is None:
            return "ERROR: background jobs are not enabled on this bot. Do the work inline."
        raw_goal = str(
            goal or kwargs.get("text") or kwargs.get("prompt") or ""
        ).strip()
        if not raw_goal:
            raw_goal = str(getattr(message, "content", "") or "").strip()[:500]
        if not raw_goal:
            return "ERROR: no goal given. Pass goal='...' describing what to do."
        author = getattr(message, "author", None)
        channel = getattr(message, "channel", None)
        guild = getattr(message, "guild", None)
        try:
            job = manager.create(
                guild_id=getattr(guild, "id", "") or "",
                channel_id=getattr(channel, "id", "") or "",
                user_id=getattr(author, "id", "") or "",
                goal=raw_goal,
                context=str(context or kwargs.get("details") or "")[:4000],
            )
        except (ValueError, RuntimeError) as exc:
            text = str(exc)
            if text.startswith("ALREADY_RUNNING:"):
                return (
                    "A background job is ALREADY RUNNING for this user. "
                    "Reply NOW with send_message: ONE short ack, nothing else."
                )
            if text.startswith("ALL_BUSY:"):
                return (
                    f"COULD NOT START ({text[len('ALL_BUSY:'):].strip()}). "
                    "Do the work inline."
                )
            return f"COULD NOT START: {text} Do the work inline."
        manager.attach_runtime(job.id, message=message, channel=channel)
        try:
            task = _spawn_background(run_background_job(bot, job.id))
            manager.track_task(job.id, task)
        except RuntimeError as exc:
            manager.mark(job.id, status="error", progress=f"could not launch: {exc}")
            return f"ERROR launching `{job.id}`: {exc} Do the work inline."
        return (
            f"Background job `{job.id}` started for '{_short(raw_goal, 80)}'. "
            f"Reply NOW with send_message: ONE short ack naming `{job.id}` "
            "and NOTHING else. Do not start the work this turn."
        )


def _call_name(call: Any) -> str:
    if isinstance(call, dict):
        name = call.get("name")
        if name:
            return str(name)
        fn = call.get("function")
        if isinstance(fn, dict) and fn.get("name"):
            return str(fn["name"])
    return ""


_PROGRESS_MARKERS = (
    "site created",
    "wrote ",
    "patched ",
    "backend server live",
    "deployed",
    "restarted",
    "generated ",
    "saved ",
    "downloaded",
    "hosted ",
    "search results",
    "created ",
    "updated ",
    "deleted ",
)


def _looks_like_progress(tool_results: Any) -> bool:
    blob = " ".join(str(r or "") for r in list(tool_results or [])[:4]).lower()
    return any(marker in blob for marker in _PROGRESS_MARKERS)


def _summarize_tool_results(results: Any, limit: int = 300) -> str:
    parts = []
    for item in list(results or [])[:4]:
        text = re.sub(r"\s+", " ", str(item or "")).strip()
        if text:
            parts.append(text[:limit])
    blob = " | ".join(parts)
    return blob[:1200] if blob else "(no output)"


_URL_RE = re.compile(r"https?://[^\s)>\]]+")


def _first_url(text: Any) -> str:
    match = _URL_RE.search(str(text or ""))
    return match.group(0).rstrip(".,;:") if match else ""


_VAGUE_FINAL_RE = re.compile(
    r"^(i['’]m\s+(all\s+)?done|done|finished|all\s+done|complete)[.!…]*$",
    re.IGNORECASE,
)


def _worker_system_body(job_id: str, goal: str, context: str = "") -> str:
    """Instructions for a detached worker, optimized for convergence."""
    extra = f"Context: {context}\n" if str(context or "").strip() else ""
    return (
        f"BACKGROUND job `{job_id}`. Channel already acked — don't narrate. "
        "No channel posts (no send_message). Only your FINAL line is delivered.\n"
        f"Goal: {goal}\n"
        f"{extra}"
        "Execution rules:\n"
        "1. Treat the goal + concise context as the complete brief. Plan silently, then use "
        "the smallest sufficient tool sequence. Do not ask the user to repeat information.\n"
        "2. Advance after each result. Do not repeat a successful search, fetch, grep, or file "
        "read unless the underlying state changed or verification is genuinely required. Batch "
        "independent work when a tool supports it.\n"
        "3. Verify externally visible or destructive work once at the end; do not repeatedly "
        "re-verify unchanged state. If a tool fails, correct the call once, then choose an "
        "alternative or fail concretely instead of looping.\n"
        "4. Pick tools that match the goal. Sites: create_site / edit_site / host_file "
        "(site_server only if a backend is needed). Research: web_search / fetch_url. "
        "Images: hd_image / image_generator. Code/files: shell / host_file. Do not force "
        "a website unless the goal is a site.\n"
        "5. If this IS a site: static HTML/CSS/JS is fine. Relative API paths "
        "(`api/notes`, never `/api/...`) only when a backend is needed. Patch live files via "
        "tools. One route = one definition; don't remount the same path.\n"
        "6. Do the whole job. No placeholders. Finishing is the job; once acceptance criteria "
        "are met, stop using tools and return the result.\n"
        "Last message: one concrete result line, not 'done' or 'finished'. If you produced a "
        "real URL: `Built <title>: <url> — <one line>` with the title and URL from tools. "
        "Otherwise give the answer/artifact (path, summary, image, findings). Never invent a "
        "URL. On failure: `FAILED: <specific reason>`."
    )


def _delivery_line(final_text: Any, job_id: str) -> str:
    first = ""
    for line in str(final_text or "").splitlines():
        line = line.strip()
        if line:
            first = line
            break
    summary = re.sub(r"\s+", " ", first)[:200] or "done — details in the thread"
    url = _first_url(final_text)
    if not url and (_VAGUE_FINAL_RE.match(summary) or len(summary) < 15):
        return f"job `{job_id}` done — details in the thread."
    head = f"job `{job_id}` done — {summary}"
    if url and url not in head:
        return f"{head}\n{url}"
    return head


async def _reply_short(target_message: Any, channel: Any, text: str) -> None:
    text = str(text or "")[:1900]
    if not text.strip():
        return
    reply: Any = getattr(target_message, "reply", None)
    if callable(reply):
        try:
            try:
                await reply(text, mention_author=True)
            except TypeError:
                await reply(text)
            return
        except Exception:
            pass
    if channel is not None:
        await channel.send(text)


async def _llm_delivery_line(
    bot: Any, final_text: Any, job_id: str, job_goal: str = ""
) -> str:
    fallback = _delivery_line(final_text, job_id)
    try:
        generate = getattr(bot, "_generate_response", None)
        if not callable(generate):
            return fallback
        raw = str(final_text or "").strip()[:2000] or fallback
        goal = re.sub(r"\s+", " ", str(job_goal or "")).strip()[:200]
        prompt = (
            "One Discord line, <200 chars. Keep the real result. Include a URL only if the "
            "result has one; never invent a link. No placeholders, extra links, job-id prefix, "
            "or thread talk.\n"
            f"Result: {raw}" + (f"\nGoal: {goal}" if goal else "")
        )
        try:
            await bot._acquire_ai_slot(
                timeout=30.0, priority="background", key=f"delivery-{job_id}"
            )
            slot_held = True
        except Exception:
            slot_held = False
        name = "Bot"
        with contextlib.suppress(Exception):
            name = (
                str(process_name(bot) or getattr(bot, "bot_name", None) or "Bot").strip()
                or "Bot"
            )
        try:
            resp = await generate(
                [
                    {"role": "system", "content": f"{name}. One short reply line."},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=256,
                timeout=120,
                disable_reasoning=False,
            )
        finally:
            if slot_held:
                with contextlib.suppress(Exception):
                    await bot._release_ai_slot()
        text = str(resp or "").strip()
        first = ""
        for line in text.splitlines():
            line = line.strip()
            if line:
                first = line
                break
        first = re.sub(r"\s+", " ", first)[:400]
        if not first or len(first) < 10:
            return fallback
        url = _first_url(text) or _first_url(raw)
        if url and url not in first:
            return f"{first}\n{url}"
        return first
    except Exception:
        return fallback


async def _post_thread(thread: Any, text: str) -> None:
    if thread is None or not text:
        return
    try:
        await thread.send(str(text)[:1900])
    except Exception as exc:
        logger.debug("background job thread post failed: %s", exc)


async def run_background_job(bot: Any, job_id: str) -> None:
    """Detached worker with bounded context, circuit breakers, and final delivery."""
    manager = getattr(bot, "bg_jobs", None)
    job = manager.get(job_id) if manager is not None else None
    if job is None:
        logger.warning("background job %s vanished before start", job_id)
        return
    rt = manager.runtime(job.id)
    orig_message = rt.get("message")
    channel = rt.get("channel")
    if channel is None and orig_message is not None:
        channel = getattr(orig_message, "channel", None)
    if channel is None:
        with contextlib.suppress(Exception):
            channel = bot.get_channel(int(job.channel_id))
        if channel is None:
            with contextlib.suppress(Exception):
                channel = await bot.fetch_channel(int(job.channel_id))

    async def _fail(text: str) -> None:
        manager.mark(job.id, status="error", progress=text[:500])
        await _reply_short(
            orig_message, channel, f"job `{job.id}` failed — {text[:300]}"
        )

    if orig_message is None or channel is None:
        await _fail("lost the origin channel (restart or deleted channel).")
        return

    control = getattr(bot, "_control", {}) or {}
    budgets = resolve_job_budgets(control, getattr(bot, "config", None))
    context_chars = resolve_job_context_chars(control)
    job_model = resolve_job_model(control)
    max_tokens = int(budgets["max_tokens"])
    timeout = int(budgets["timeout_seconds"])
    max_iters = int(budgets["max_iters"])

    manager.mark(job.id, status="running", progress="starting")

    thread = None
    thread_err = ""
    try:
        if hasattr(orig_message, "create_thread"):
            thread = await orig_message.create_thread(
                name=f"job: {_short(job.goal, 40)}", auto_archive_duration=60
            )
        elif hasattr(channel, "create_thread"):
            import discord

            thread = await channel.create_thread(
                name=f"job: {_short(job.goal, 40)}",
                auto_archive_duration=60,
                type=discord.ChannelType.public_thread,
                message=orig_message,
            )
    except Exception as exc:
        thread_err = str(exc)[:200]
    if thread is not None:
        manager.mark(job.id, thread_id=str(getattr(thread, "id", "") or ""))
        store = getattr(bot, "thread_store", None)
        if store is not None:
            brief = job.goal
            extra = str(getattr(job, "context", "") or "").strip()
            if extra:
                brief = f"{brief}\n{extra}"
            try:
                await store.remember(
                    thread,
                    parent_message=orig_message,
                    context=(
                        f"Background job `{job.id}`.\n{brief}\n"
                        "Progress and the finished result belong in this thread."
                    ),
                    source="spawn_background",
                )
            except Exception:
                logger.debug(
                    "could not store background-job thread brief", exc_info=True
                )
        await _post_thread(
            thread,
            f"Job `{job.id}` running — `{_short(job.goal, 120)}`\n"
            f"Budgets: {max_tokens} output tokens/call, {context_chars} history chars, "
            f"{timeout}s timeout, {max_iters} steps. Progress lands here.",
        )
    else:
        logger.info(
            "background job %s: no thread (%s)",
            job.id,
            thread_err or "DMs have no threads",
        )

    try:
        orig_message._bg_job = True
    except Exception:
        pass

    try:
        from tool_schemas import TURN_ENDING_TOOL_NAMES
    except Exception:
        TURN_ENDING_TOOL_NAMES = frozenset({"send_message", "no_response", "sleep"})

    platform = "discord"
    try:
        platform = str(bot._message_tool_platform(orig_message) or "discord")
    except Exception:
        pass

    base_personality = ""
    try:
        get_p = getattr(bot, "_get_personality", None)
        if callable(get_p):
            base_personality = str(get_p() or "")
        else:
            from identity import fill_identity

            raw = str(control.get("base_personality") or "")
            base_personality = fill_identity(raw) if raw else ""
    except Exception:
        pass
    tool_prompt = ""
    try:
        tool_prompt = str(
            bot._tool_system_prompt(platform, message=orig_message, content=job.goal)
            or ""
        )
    except Exception as exc:
        logger.debug("background job %s tool prompt failed: %s", job.id, exc)

    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": (
                f"{base_personality}\n\n"
                f"{_worker_system_body(job.id, job.goal, job.context)}\n\n"
                f"{tool_prompt}"
            ).strip(),
        },
        {
            "role": "user",
            "content": f"<@{job.user_id}>: {job.goal}\nDo it now.",
        },
    ]

    openai_tools: list[dict[str, Any]] = []
    try:
        openai_tools = list(
            bot._build_openai_tools(platform, message=orig_message, content=job.goal)
            or []
        )
    except Exception as exc:
        logger.warning("background job %s tool catalog failed: %s", job.id, exc)
    openai_tools = _worker_tools(openai_tools)
    try:
        _custom, provider_tools = bot._select_tool_protocol(openai_tools)
    except Exception:
        provider_tools = openai_tools

    final_text = ""
    succeeded = False
    finished_cleanly = False
    last_progress_step = 0
    consecutive_dispatch_errors = 0
    last_batch_signature = ""
    repeated_batch_count = 0
    repeat_nudged = False
    deadline = time.monotonic() + float(timeout)

    try:
        for step in range(max(1, max_iters)):
            messages = _compact_worker_messages(messages, max_chars=context_chars)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                manager.mark(
                    job.id, progress=f"time budget ({timeout}s) hit at step {step}"
                )
                final_text = final_text or (
                    "I ran out of time budget — partial work is in the thread."
                )
                succeeded = False
                break

            try:
                await bot._acquire_ai_slot(
                    timeout=float(min(remaining, 600)),
                    priority="background",
                    key=job.channel_id,
                )
            except Exception as exc:
                if step == 0:
                    await _fail(f"still waiting on an LLM slot after 10m ({exc}).")
                    return
                logger.warning(
                    "background job %s slot wait failed at step %s: %s",
                    job.id,
                    step,
                    exc,
                )
                succeeded = False
                break

            try:
                response = await bot._generate_response(
                    messages,
                    timeout=min(timeout, remaining),
                    max_tokens=max_tokens,
                    tools=provider_tools,
                    disable_reasoning=False,
                    model=job_model,
                )
                succeeded = True
            except Exception as exc:
                logger.warning(
                    "background job %s generation failed at step %s: %s",
                    job.id,
                    step,
                    exc,
                )
                succeeded = False
                final_text = final_text or (
                    f"generation failed ({type(exc).__name__}); partial work is in the thread."
                )
                break
            finally:
                with contextlib.suppress(Exception):
                    await bot._release_ai_slot()

            try:
                calls = list(bot._native_calls_from(response) or [])
            except Exception:
                calls = []
            if not calls:
                try:
                    recovered, response = bot._recover_text_tool_calls(response)
                    calls = list(recovered or [])
                except Exception:
                    calls = []
            calls = [c for c in calls if _call_name(c) not in _WORKER_HIDDEN_TOOLS]
            if not calls:
                try:
                    cleaned = await bot._dispatch_tool_calls(orig_message, response or "")
                    final_text = (
                        cleaned[0]
                        if isinstance(cleaned, (list, tuple))
                        else str(cleaned or "")
                    )
                except Exception:
                    final_text = str(response or "")
                final_text = str(final_text or "").strip()
                finished_cleanly = True
                break

            batch_signature = _batch_signature(calls)
            if batch_signature and batch_signature == last_batch_signature:
                repeated_batch_count += 1
            else:
                last_batch_signature = batch_signature
                repeated_batch_count = 1
                repeat_nudged = False
            if repeated_batch_count >= BG_REPEAT_ABORT_AT:
                final_text = (
                    "repeated the same tool call without converging; stopped to avoid a loop."
                )
                succeeded = False
                await _post_thread(
                    thread,
                    f"step {step + 1}: stopped — identical tool batch repeated "
                    f"{repeated_batch_count} times.",
                )
                break
            if repeated_batch_count >= BG_REPEAT_NUDGE_AT and not repeat_nudged:
                repeat_nudged = True
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "You are repeating the exact same tool call. Do not call it again "
                            "with identical arguments. Use the result you already have, change "
                            "the approach, or finish with a concrete result."
                        ),
                    }
                )

            names = [_call_name(c) for c in calls]
            try:
                dispatched = await bot._dispatch_tool_calls(
                    orig_message, response, native_tool_calls=calls
                )
                consecutive_dispatch_errors = 0
                if isinstance(dispatched, (list, tuple)):
                    resp_text = str(dispatched[0] or "")
                    tool_results = (
                        list(dispatched[1] or []) if len(dispatched) > 1 else []
                    )
                else:
                    resp_text, tool_results = str(dispatched or ""), []
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                consecutive_dispatch_errors += 1
                logger.warning(
                    "background job %s dispatch failed at step %s (%s/%s): %s",
                    job.id,
                    step,
                    consecutive_dispatch_errors,
                    BG_DISPATCH_ERROR_LIMIT,
                    exc,
                )
                if consecutive_dispatch_errors >= BG_DISPATCH_ERROR_LIMIT:
                    final_text = (
                        f"tool dispatch failed {consecutive_dispatch_errors} times in a row "
                        f"({type(exc).__name__})."
                    )
                    succeeded = False
                    break
                messages.append({"role": "assistant", "content": str(response or "")})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "=== TOOL RESULTS ===\n"
                            f"tool error: {type(exc).__name__}: {str(exc)[:500]}\n"
                            "Correct the call once or choose a different tool; do not repeat "
                            "the same failing call."
                        ),
                    }
                )
                messages = _compact_worker_messages(messages, max_chars=context_chars)
                continue

            manager.mark(
                job.id,
                progress=(
                    f"step {step + 1}: "
                    f"{', '.join([n for n in names if n][:4]) or 'thinking'}"
                ),
            )
            if _looks_like_progress(tool_results):
                last_progress_step = step
            else:
                stalled_for = step - last_progress_step
                if stalled_for >= BG_STALL_ABORT_STEPS:
                    final_text = (
                        f"no material progress for {stalled_for} steps; stopped to avoid "
                        "burning the remaining budget."
                    )
                    succeeded = False
                    await _post_thread(
                        thread,
                        f"step {step + 1}: stopped — no material progress for "
                        f"{stalled_for} steps.",
                    )
                    break
                if stalled_for >= BG_STALL_NUDGE_STEPS:
                    last_progress_step = step
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "No material progress recently. Stop grepping/re-reading and "
                                "stop broadening scope. Use what you already know to finish the "
                                "goal with the smallest remaining tool sequence, then return one "
                                "concrete result line."
                            ),
                        }
                    )
                    await _post_thread(
                        thread,
                        f"step {step + 1}: nudge — no material progress, forcing converge.",
                    )

            await _post_thread(
                thread,
                f"step {step + 1} `{', '.join([n for n in names if n][:4]) or '…'}` — "
                f"{_summarize_tool_results(tool_results)}",
            )

            named = {n for n in names if n}
            if named and named <= set(TURN_ENDING_TOOL_NAMES) and resp_text.strip():
                final_text = resp_text.strip()
                finished_cleanly = True
                break

            try:
                followups = list(
                    getattr(bot, "_last_native_followup_messages", None) or []
                )
            except Exception:
                followups = []
            if followups:
                messages.extend(followups)
            else:
                messages.append({"role": "assistant", "content": str(response or "")})
                clipped = "\n".join(str(r or "")[:1600] for r in tool_results[:4])[:6000]
                messages.append(
                    {"role": "user", "content": "=== TOOL RESULTS ===\n" + clipped}
                )
            messages = _compact_worker_messages(messages, max_chars=context_chars)

            if not tool_results:
                final_text = resp_text.strip()
                break
            final_text = resp_text.strip()

        if not succeeded or not finished_cleanly:
            await _fail(final_text or "the model never returned a finished answer.")
            await _post_thread(thread, "Failed before producing a final answer.")
            return

        final_text = str(final_text or "").strip()
        if not final_text or final_text.upper().startswith("FAILED"):
            await _fail(final_text or "empty final answer.")
            await _post_thread(thread, "Failed before producing a final answer.")
            return

        manager.mark(job.id, progress="delivering")
        try:
            body = await _llm_delivery_line(bot, final_text, job.id, job.goal)
        except Exception:
            body = _delivery_line(final_text, job.id)
        try:
            await _reply_short(orig_message, channel, body)
        except Exception as exc:
            logger.warning("background job %s delivery failed: %s", job.id, exc)
            await _post_thread(
                thread,
                f"Done, but I could not post to the channel ({exc}):\n{body[:1500]}",
            )
        manager.mark(job.id, status="done", result=final_text[:8000], progress="done")
        await _post_thread(thread, f"Finished.\n{str(final_text or '')[:1500]}")
    except asyncio.CancelledError:
        manager.mark(job.id, status="cancelled", progress="cancelled on request")
        await _post_thread(thread, f"Job `{job.id}` cancelled.")
        raise
    finally:
        manager.cleanup_runtime(job.id)
