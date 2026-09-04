"""Background sub-agent jobs for long tasks (sites, builds, research).

The problem: a site build holds the channel's ``ReplyQueue`` turn (and one
of only two global LLM slots) for minutes, so everyone else in the room
queues behind it and the bot looks channel-locked.

The fix: the model calls ``spawn_background`` (or a user runs ``,bg``).
The live turn ends immediately with a one-line ack naming the job id, and
the real work runs detached in :func:`run_background_job` with EXTENDED
budgets (more thinking, more output, longer timeout than a live turn).
When the job finishes it mentions the requester in the origin channel with
the result data. Progress lands in a ``build: <goal>`` thread.

Additive by design: this module never monkey-patches the bot. It reuses the
bot's own seams (``_generate_response``, ``_build_openai_tools``,
``_dispatch_tool_calls``, ``_acquire_ai_slot``) and posts via
``channel.send`` directly, never through ``ReplyQueue``.
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

from tools import Tool
from utils import _safe_int, _spawn_background

logger = logging.getLogger(__name__)

# A job id is short on purpose: the model has to quote it in its ack line.
JOB_ID_BYTES = 4

# Extended-budget defaults for background jobs. Live turns stay tight;
# jobs get the big headroom. Env-overridable, control-overridable
# (bg_max_tokens / bg_timeout_seconds / bg_max_iters).
BG_MAX_TOKENS_DEFAULT_FLOOR = 32768
BG_MAX_TOKENS_HARD_CAP = 131072
BG_TIMEOUT_DEFAULT = 7200
BG_TIMEOUT_HARD_CAP = 14400
BG_ITERS_DEFAULT = 100
BG_ITERS_HARD_CAP = 200

# How many jobs may run at once, and per user. Queued extras are refused
# with a "busy" message rather than piling up behind each other.
BG_MAX_JOBS_DEFAULT = 2
BG_MAX_PER_USER_DEFAULT = 1

# Job tools never include this: a background turn that spawns another
# background turn is recursion, not progress.
_NO_RECURSE_TOOL = "spawn_background"


def resolve_job_budgets(control: Any, config: Any) -> dict[str, int]:
    """Extended thinking/output/timeout budgets for background jobs.

    Precedence per key: control override > env > default. Every value is
    clamped to its hard cap so a typo cannot book a 24h call.
    """
    control = control or {}
    live_max_tokens = (
        _safe_int(getattr(config, "OLLAMA_MAX_TOKENS", 16384) or 16384, 16384)
        if config is not None
        else 16384
    )
    default_tokens = max(live_max_tokens * 2, BG_MAX_TOKENS_DEFAULT_FLOOR)

    def _pick(control_key: str, env_key: str, default: int, cap: int) -> int:
        raw = (control or {}).get(control_key, None)
        if raw is None:
            raw = os.getenv(env_key, "")
        text = str(raw or "").strip()
        # 0/blank = unset → fall back to env, then to the default.
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
            "bg_timeout_seconds", "BG_TIMEOUT_SECONDS", BG_TIMEOUT_DEFAULT, BG_TIMEOUT_HARD_CAP
        ),
        "max_iters": _pick(
            "bg_max_iters", "BG_MAX_ITERS", BG_ITERS_DEFAULT, BG_ITERS_HARD_CAP
        ),
    }


def _short(text: Any, limit: int = 50) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()[:limit] or "project"


@dataclass
class BackgroundJob:
    id: str
    guild_id: str
    channel_id: str
    user_id: str
    goal: str
    context: str = ""
    status: str = "queued"  # queued | running | done | error | cancelled
    progress: str = ""
    result: str = ""
    thread_id: str = ""
    created_at: float = field(default_factory=time.time)
    finished_at: float = 0.0


class BackgroundJobManager:
    """Track detached jobs. Discord objects live only in _runtime (memory);
    _jobs (metadata) is what gets persisted."""

    def __init__(
        self,
        data_path: str = "data/background_jobs.json",
        *,
        max_jobs: int | None = None,
        max_per_user: int | None = None,
    ) -> None:
        self.data_path = data_path
        self.max_jobs = max(1, int(max_jobs if max_jobs is not None else os.getenv("MAXWELL_BG_JOBS", BG_MAX_JOBS_DEFAULT) or BG_MAX_JOBS_DEFAULT))
        self.max_per_user = max(1, int(max_per_user if max_per_user is not None else os.getenv("MAXWELL_BG_PER_USER", BG_MAX_PER_USER_DEFAULT) or BG_MAX_PER_USER_DEFAULT))
        self._jobs: dict[str, BackgroundJob] = {}
        self._runtime: dict[str, dict[str, Any]] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self.load()

    # ---- persistence ----------------------------------------------------
    def _save(self) -> None:
        try:
            directory = os.path.dirname(self.data_path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            payload = {
                jid: {k: v for k, v in asdict(job).items()}
                for jid, job in self._jobs.items()
            }
            tmp = f"{self.data_path}.tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump({"jobs": payload}, handle)
            os.replace(tmp, self.data_path)
        except Exception as exc:  # best effort; a job must never die on a write
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
            try:
                job = BackgroundJob(
                    id=str(jid),
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
            # A restart kills in-flight work; say so instead of lying.
            if job.status in ("queued", "running"):
                job.status = "cancelled"
                job.progress = "bot restarted while this job was running"
                job.finished_at = time.time()
            self._jobs[job.id] = job
        # History, not archive: keep the recent slice.
        if len(self._jobs) > 50:
            ordered = sorted(self._jobs.values(), key=lambda j: j.created_at)
            for stale in ordered[:-50]:
                self._jobs.pop(stale.id, None)
                self.cleanup_runtime(stale.id)

    # ---- lifecycle ------------------------------------------------------
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
        jid = secrets.token_hex(JOB_ID_BYTES)
        while jid in self._jobs:
            jid = secrets.token_hex(JOB_ID_BYTES)
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
        return self._jobs.get(str(job_id or "").strip().lower())

    def list_text(self, limit: int = 10, *, guild_id: Any = None) -> str:
        if not self._jobs:
            return "no background jobs yet."
        gid = str(guild_id or "").strip()
        jobs = [j for j in self._jobs.values() if not gid or j.guild_id == gid]
        if not jobs:
            return "no background jobs in this server yet."
        ordered = sorted(jobs, key=lambda j: j.created_at, reverse=True)[: max(1, limit)]
        lines = []
        for job in ordered:
            age = time.strftime("%H:%M", time.localtime(job.created_at)) if job.created_at else "??:??"
            lines.append(f"`{job.id}` [{job.status}] <@{job.user_id}> {_short(job.goal, 60)} ({age})")
        return "\n".join(lines)

    def cleanup_runtime(self, job_id: str) -> None:
        jid = str(job_id)
        self._runtime.pop(jid, None)
        self._tasks.pop(jid, None)

    def attach_runtime(self, job_id: str, **objects: Any) -> None:
        self._runtime[str(job_id)] = dict(objects)

    def runtime(self, job_id: str) -> dict[str, Any]:
        return self._runtime.get(str(job_id), {})

    def track_task(self, job_id: str, task: asyncio.Task) -> None:
        self._tasks[str(job_id)] = task

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

    def cancel(self, job_id: str, *, requester_id: Any = None, is_admin: bool = False) -> tuple[bool, str]:
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
            "Start a BACKGROUND job for a long task (site build, big research, "
            "multi-step work) and END this turn. The job runs detached with "
            "bigger budgets and pings the user when done, so the channel stays "
            "free. Params: goal (what to build/do, required), context (extra "
            "spec, optional). After calling, reply with send_message: ONE short "
            "ack line naming the job id — nothing else, no other tools."
        )

    async def execute(self, message: Any, goal: str | None = None, context: str | None = None, **kwargs: Any) -> str:
        if getattr(message, "_bg_job", False):
            return "ALREADY INSIDE a background job — do the work inline with normal tools, do not spawn again."
        bot = getattr(self, "bot", None)
        manager = getattr(bot, "bg_jobs", None) if bot is not None else None
        if manager is None:
            return "ERROR: background jobs are not enabled on this bot. Do the work inline."
        raw_goal = str(goal or kwargs.get("text") or kwargs.get("prompt") or "").strip()
        if not raw_goal:
            raw_goal = str(getattr(message, "content", "") or "").strip()[:500]
        if not raw_goal:
            return "ERROR: no goal given. Pass goal='...' describing what to build."
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
                    "A background job is ALREADY RUNNING for this user — do not start another. "
                    "Reply NOW with send_message: ONE short ack line and NOTHING else."
                )
            if text.startswith("ALL_BUSY:"):
                return f"COULD NOT START background job ({text[len('ALL_BUSY:'):].strip()}). Do the work inline instead."
            return f"COULD NOT START background job: {text} Do the work inline instead."
        manager.attach_runtime(job.id, message=message, channel=channel)
        try:
            task = _spawn_background(run_background_job(bot, job.id))
            manager.track_task(job.id, task)
        except RuntimeError as exc:
            manager.mark(job.id, status="error", progress=f"could not launch: {exc}")
            return f"ERROR launching background job `{job.id}`: {exc} Do the work inline."
        return (
            f"Background job `{job.id}` started for '{_short(raw_goal, 80)}'. "
            f"Reply NOW with send_message: ONE short ack line (e.g. `on it — job `{job.id}`, "
            "I'll ping you when it's done`) and NOTHING else. Do not start the work "
            "in this turn — the detached job does it."
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


def _summarize_tool_results(results: Any, limit: int = 300) -> str:
    parts = []
    for item in list(results or [])[:4]:
        text = re.sub(r"\s+", " ", str(item or "")).strip()
        if text:
            parts.append(text[:limit])
    blob = " | ".join(parts)
    return blob[:1200] if blob else "(no output)"


async def _post_thread(thread: Any, text: str) -> None:
    if thread is None or not text:
        return
    try:
        await thread.send(str(text)[:1900])
    except Exception as exc:
        logger.debug("background job thread post failed: %s", exc)


async def run_background_job(bot: Any, job_id: str) -> None:
    """Detached worker: full tool loop on extended budgets, then deliver.

    Never raises: every failure mode ends with the job marked and (when
    possible) a friendly message to the requester.
    """
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
        if channel is not None:
            with contextlib.suppress(Exception):
                await channel.send(f"<@{job.user_id}> job `{job.id}` failed — {text[:1500]}")

    if orig_message is None or channel is None:
        await _fail("lost the origin channel (restart or deleted channel).")
        return

    budgets = resolve_job_budgets(getattr(bot, "_control", {}) or {}, getattr(bot, "config", None))
    max_tokens = int(budgets["max_tokens"])
    timeout = int(budgets["timeout_seconds"])
    max_iters = int(budgets["max_iters"])

    manager.mark(job.id, status="running", progress="starting")

    # Progress thread: keeps the origin channel clean while work runs.
    thread = None
    thread_err = ""
    try:
        if hasattr(orig_message, "create_thread"):
            thread = await orig_message.create_thread(
                name=f"build: {_short(job.goal, 40)}", auto_archive_duration=60
            )
        elif hasattr(channel, "create_thread"):
            import discord  # local import: no hard dep at module load

            thread = await channel.create_thread(
                name=f"build: {_short(job.goal, 40)}",
                auto_archive_duration=60,
                type=discord.ChannelType.public_thread,
                message=orig_message,
            )
    except Exception as exc:
        thread_err = str(exc)[:200]
    if thread is not None:
        manager.mark(job.id, thread_id=str(getattr(thread, "id", "") or ""))
        await _post_thread(
            thread,
            f"Job `{job.id}` running for <@{job.user_id}> — `{_short(job.goal, 120)}`\n"
            f"Budgets: {max_tokens} tokens/call, {timeout}s timeout, {max_iters} steps. Progress lands here.",
        )
    else:
        logger.info("background job %s: no thread (%s)", job.id, thread_err or "DMs have no threads")

    # Flag the origin message so a nested spawn_background refuses (recursion
    # guard) and the job's own tools execute against the right message.
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
        base_personality = str((getattr(bot, "_control", {}) or {}).get("base_personality") or "")
    except Exception:
        pass
    tool_prompt = ""
    try:
        tool_prompt = str(bot._tool_system_prompt(platform, message=orig_message, content=job.goal) or "")
    except Exception as exc:
        logger.debug("background job %s tool prompt failed: %s", job.id, exc)

    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": (
                f"{base_personality}\n\n"
                f"You are Maxwell's BACKGROUND build agent (job `{job.id}`). The user was already "
                "told the work is running; do not narrate, just build.\n"
                f"Goal: {job.goal}\n"
                + (f"Extra context: {job.context}\n" if job.context else "")
                + "Do the whole job with tools (build, test with site_test, fix failures). "
                "Keep intermediate chatter out of the main channel — progress goes to this thread. "
                "End with a concise summary: what was built + URLs.\n\n"
                f"{tool_prompt}"
            ).strip(),
        },
        {
            "role": "user",
            "content": (
                f"Background job `{job.id}` from <@{job.user_id}>: {job.goal}"
                + (f"\nContext: {job.context}" if job.context else "")
                + "\nDo it now."
            ),
        },
    ]

    openai_tools: list[dict[str, Any]] = []
    try:
        openai_tools = list(bot._build_openai_tools(platform, message=orig_message, content=job.goal) or [])
    except Exception as exc:
        logger.warning("background job %s tool catalog failed: %s", job.id, exc)
    # No recursion: the job IS the background worker.
    openai_tools = [t for t in openai_tools if (t.get("function") or {}).get("name") != _NO_RECURSE_TOOL]
    try:
        _custom, provider_tools = bot._select_tool_protocol(openai_tools)
    except Exception:
        provider_tools = openai_tools

    final_text = ""
    succeeded = False
    deadline = time.monotonic() + float(timeout)
    try:
        for step in range(max(1, max_iters)):
            if time.monotonic() > deadline:
                manager.mark(job.id, progress=f"time budget ({timeout}s) hit at step {step}")
                final_text = final_text or "I ran out of time budget — partial work is in the thread."
                break

            remaining = max(10.0, deadline - time.monotonic())
            try:
                await bot._acquire_ai_slot(
                    timeout=float(min(remaining, 600)), priority="background", key=job.channel_id
                )
            except Exception as exc:
                if step == 0:
                    await _fail(f"still waiting on an LLM slot after 10m ({exc}).")
                    return
                logger.warning("background job %s slot wait failed at step %s: %s", job.id, step, exc)
                break

            try:
                response = await bot._generate_response(
                    messages,
                    timeout=min(timeout, remaining),
                    max_tokens=max_tokens,
                    tools=provider_tools,
                    disable_reasoning=False,
                )
                succeeded = True
            except Exception as exc:
                logger.warning("background job %s generation failed at step %s: %s", job.id, step, exc)
                final_text = final_text or f"generation failed ({type(exc).__name__}); partial work is in the thread."
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
            if not calls:
                try:
                    cleaned = await bot._dispatch_tool_calls(orig_message, response or "")
                    final_text = cleaned[0] if isinstance(cleaned, (list, tuple)) else str(cleaned or "")
                except Exception:
                    final_text = str(response or "")
                final_text = str(final_text or "").strip()
                break
            names = [_call_name(c) for c in calls]
            try:
                dispatched = await bot._dispatch_tool_calls(
                    orig_message, response, native_tool_calls=calls
                )
                if isinstance(dispatched, (list, tuple)):
                    resp_text = str(dispatched[0] or "")
                    tool_results = list(dispatched[1] or []) if len(dispatched) > 1 else []
                else:
                    resp_text, tool_results = str(dispatched or ""), []
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("background job %s dispatch failed at step %s: %s", job.id, step, exc)
                succeeded = True  # the turn itself worked; one tool errored
                messages.append({"role": "assistant", "content": str(response or "")})
                messages.append({"role": "user", "content": f"=== TOOL RESULTS ===\ntool error: {exc}"})
                continue
            manager.mark(job.id, progress=f"step {step + 1}: {', '.join([n for n in names if n][:4]) or 'thinking'}")
            await _post_thread(
                thread,
                f"step {step + 1} `{', '.join([n for n in names if n][:4]) or '…'}` — {_summarize_tool_results(tool_results)}",
            )
            # An ending-only batch (e.g. the model wrapped up via send_message)
            # is the finished answer — do not loop for more.
            named = {n for n in names if n}
            if named and named <= set(TURN_ENDING_TOOL_NAMES) and resp_text.strip():
                final_text = resp_text.strip()
                break
            try:
                followups = list(getattr(bot, "_last_native_followup_messages", None) or [])
            except Exception:
                followups = []
            if followups:
                messages.extend(followups)
            else:
                messages.append({"role": "assistant", "content": str(response or "")})
                messages.append(
                    {"role": "user", "content": "=== TOOL RESULTS ===\n" + "\n".join(tool_results)}
                )
            if not tool_results:
                final_text = resp_text.strip()
                break
            final_text = resp_text.strip()
    except asyncio.CancelledError:
        manager.mark(job.id, status="cancelled", progress="cancelled on request")
        await _post_thread(thread, f"Job `{job.id}` cancelled.")
        raise
    finally:
        manager.cleanup_runtime(job.id)

    if not succeeded:
        # Nothing ever came back — honest error, not a fake done.
        await _fail(final_text or "the model never returned anything.")
        await _post_thread(thread, "Failed before producing output.")
        return

    final_text = str(final_text or "").strip() or "Done — details are in the thread."
    manager.mark(job.id, status="done", result=final_text[:8000], progress="done")

    # Deliver: mention the requester in the ORIGIN channel with the data.
    # Direct send — never through ReplyQueue, so nothing else ever waits.
    max_chars = 4000
    try:
        max_chars = max(500, min(int((getattr(bot, "_control", {}) or {}).get("max_response_chars", 4000) or 4000), 1900 * 4))
    except Exception:
        pass
    body = final_text[:max_chars]
    thread_ref = ""
    try:
        thread_ref = getattr(thread, "jump_url", None) or getattr(thread, "mention", "") or ""
    except Exception:
        pass
    delivery = f"<@{job.user_id}> job `{job.id}` done — {body}"
    if thread_ref:
        delivery += f"\n{thread_ref}"
    try:
        splitter = getattr(bot, "_split_response", None)
        if callable(splitter):
            chunks = splitter(delivery, limit=1900)
        else:
            chunks = [delivery[i : i + 1900] for i in range(0, len(delivery), 1900)]
        for chunk in chunks:
            if chunk.strip():
                await channel.send(chunk)
    except Exception as exc:
        logger.warning("background job %s delivery failed: %s", job.id, exc)
        await _post_thread(thread, f"Done, but I could not post to the channel ({exc}):\n{body[:1500]}")
    await _post_thread(thread, f"Finished.\n{body[:1500]}")
