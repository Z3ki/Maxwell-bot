"""AutonomyEngine — Maxwell's self-directed life loop.

Runs alongside REM and on_message. Wakes every N seconds, gathers context
(DMs, channel history, memory, goals, recent events), asks the LLM what to
do, and executes actions through the existing tool system.

No approval queues. No shadow mode. Maxwell decides, Maxwell acts.

MAINTAINER NOTES:
- The old version had a self-defeating prompt ("nobody messaged you" then
  "check if anyone messaged you"). The LLM defaulted to do_nothing 75% of
  the time. Don't add that shit back.
- search_messages is disabled for autonomy — it requires a guild context
  that SyntheticMessage doesn't reliably provide. The LLM kept calling it
  with empty queries and getting errors. If you re-enable it, test with
  actual SyntheticMessage instances first.
- The context budget is PER-SECTION now, not global truncation. The old
  version truncated from the end, so channel activity (the most actionable
  data) got eaten first. Don't "simplify" back to global truncation.
- Channel cooldowns prevent spam. The bot was posting 3 messages in one
  tick to the same channel. Respect the cooldown dict.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from bot import MaxwellBot

logger = logging.getLogger(__name__)

AUTONOMY_VALID_KINDS = frozenset({
    "send_dm", "post_channel", "run_tool", "update_memory",
    "create_goal", "do_nothing",
})
MAX_ACTIONS_PER_TICK = 3  # reduced from 5 — prevents spam bursts
MAX_CONTENT_CHARS = 1900
LOG_RING_SIZE = 200

# Per-section context budgets (sum ~8000)
CTX_BUDGET_GOALS = 800
CTX_BUDGET_RECENT_EVENTS = 2000
CTX_BUDGET_CHANNEL_ACTIVITY = 2000
CTX_BUDGET_RECENT_ACTIONS = 1200
CTX_BUDGET_DM_HISTORY = 1200
CTX_BUDGET_LTM = 800
CTX_BUDGET_SHARED = 600
CTX_BUDGET_CHANNELS_MAP = 800

# Channel cooldown: don't post to same channel within N seconds
CHANNEL_COOLDOWN_SECONDS = 1800  # 30 min

# Tools that don't work with autonomous SyntheticMessage execution
AUTONOMY_DISABLED_TOOLS = frozenset({
    "search_messages",  # requires guild context, fails in DMs
    "react",            # reacting to synthetic message is pointless
    "no_response",      # not useful in autonomy context
    "typing",           # ephemeral, no value
    "forward_message",  # requires a real message_id to forward
    "edit_message",     # requires a real message_id to edit
    "delete_message",   # requires a real message_id to delete
})


# ---------------------------------------------------------------------------
# Atomic JSON helpers (same pattern as memory.py / rem.py)
# ---------------------------------------------------------------------------

def _atomic_json_write_sync(path: Path, data):
    """Atomic JSON write: temp file -> fsync -> rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fd = -1  # fdopen took ownership — don't double-close
            json.dump(data, f, indent=2, ensure_ascii=False, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        if os.path.exists(tmp):
            os.unlink(tmp)


def _load_json_safe(path: Path, default):
    """Load JSON, tolerating missing/corrupt files."""
    try:
        if not path.exists():
            return default if not callable(default) else default()
        raw = path.read_text(encoding="utf-8").strip()
        if not raw:
            return default if not callable(default) else default()
        data = json.loads(raw)
        return data
    except (json.JSONDecodeError, OSError, ValueError) as e:
        logger.warning(f"Corrupt/unreadable {path.name}, recreating defaults: {e}")
        return default if not callable(default) else default()


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _truncate(text: str, budget: int) -> str:
    """Truncate text to budget, adding ellipsis if cut."""
    budget = max(0, int(budget or 0))
    if len(text) <= budget:
        return text
    suffix = "\n... [truncated]"
    if budget <= len(suffix):
        return text[:budget]
    return text[:budget - len(suffix)] + suffix


# ---------------------------------------------------------------------------
# Synthetic message for tools that expect a discord.Message
# ---------------------------------------------------------------------------

class SyntheticMessage:
    """Minimal message-like object for tool execution outside on_message."""
    def __init__(self, channel, author, guild, content: str):
        self.channel = channel
        self.author = author
        self.guild = guild
        self.content = content
        self.id = None  # None instead of 0 — 0 is an invalid snowflake
        self.attachments = []
        self.embeds = []
        self.reference = None
        # tools access these — without them you get AttributeError
        self.mentions = []
        self.role_mentions = []
        self.channel_mentions = []
        self.type = discord.MessageType.default
        self.pinned = False
        self.tts = False
        self.flags = discord.MessageFlags()
        self.created_at = datetime.now(timezone.utc)

    async def reply(self, content=None, **kwargs):
        return await self.channel.send(content, **kwargs)

    async def add_reaction(self, emoji):
        pass  # no-op — reacting to a synthetic msg is pointless, nobody sees it

    async def remove_reaction(self, emoji, member):
        pass  # same

    async def edit(self, **kwargs):
        raise NotImplementedError("Cannot edit a SyntheticMessage")

    async def delete(self, *args, **kwargs):
        pass  # silently ignore


# ---------------------------------------------------------------------------
# AutonomyStore — JSON-backed persistence
# ---------------------------------------------------------------------------

class AutonomyStore:
    """Manages the three autonomy data files with atomic writes."""

    def __init__(self, data_dir: str):
        self.data_dir = Path(data_dir)
        self.state_file = self.data_dir / "autonomy_state.json"
        self.goals_file = self.data_dir / "autonomy_goals.json"
        self.log_file = self.data_dir / "autonomy_log.json"
        self._lock = asyncio.Lock()

    # -- state --

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
        """Read-modify-write under a single lock. fn(state) mutates in-place."""
        async with self._lock:
            state = await asyncio.to_thread(_load_json_safe, self.state_file, dict)
            if not isinstance(state, dict):
                state = {}
            fn(state)
            await asyncio.to_thread(_atomic_json_write_sync, self.state_file, state)
            return state

    # -- goals --

    async def load_goals(self) -> list[dict]:
        async with self._lock:
            data = await asyncio.to_thread(_load_json_safe, self.goals_file, dict)
            goals = data.get("goals", []) if isinstance(data, dict) else []
            return goals if isinstance(goals, list) else []

    async def save_goals(self, goals: list[dict]):
        async with self._lock:
            await asyncio.to_thread(
                _atomic_json_write_sync, self.goals_file, {"goals": goals}
            )

    MAX_GOALS = 50  # cap to prevent unbounded growth

    async def add_goal(self, description: str) -> dict:
        async with self._lock:
            data = await asyncio.to_thread(_load_json_safe, self.goals_file, dict)
            goals = data.get("goals", []) if isinstance(data, dict) else []
            if not isinstance(goals, list):
                goals = []
            if len(goals) >= self.MAX_GOALS:
                logger.warning(f"Goal limit reached ({self.MAX_GOALS}), rejecting new goal")
                return {"id": None, "description": description, "error": "goal limit reached"}
            goal = {
                "id": f"goal_{uuid.uuid4().hex[:8]}",
                "description": str(description)[:500],
                "active": True,
                "created_at": _utcnow_iso(),
                "last_acted_on": None,
            }
            goals.append(goal)
            await asyncio.to_thread(
                _atomic_json_write_sync, self.goals_file, {"goals": goals}
            )
            return goal

    async def remove_goal(self, goal_id: str) -> bool:
        async with self._lock:
            data = await asyncio.to_thread(_load_json_safe, self.goals_file, dict)
            goals = data.get("goals", []) if isinstance(data, dict) else []
            if not isinstance(goals, list):
                goals = []
            before = len(goals)
            goals = [g for g in goals if g.get("id") != goal_id]
            if len(goals) == before:
                return False
            await asyncio.to_thread(
                _atomic_json_write_sync, self.goals_file, {"goals": goals}
            )
            return True

    # -- action log (ring buffer) --

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
            # ring buffer
            entries = entries[-LOG_RING_SIZE:]
            await asyncio.to_thread(
                _atomic_json_write_sync, self.log_file, {"entries": entries}
            )

    async def clear_log(self):
        async with self._lock:
            await asyncio.to_thread(
                _atomic_json_write_sync, self.log_file, {"entries": []}
            )

    # -- error shortcut --

    async def record_error(self, error: str):
        await self.patch_state({"last_error": str(error)[:2000]})


# ---------------------------------------------------------------------------
# AutonomyEngine
# ---------------------------------------------------------------------------

class AutonomyEngine:
    """Background async loop that gives Maxwell self-directed agency."""

    def __init__(self, bot: "MaxwellBot"):
        self.bot = bot
        self.store = AutonomyStore(bot.config.DATA_DIR)
        self._running = False
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()  # prevents concurrent ticks
        self._last_thought = ""  # avoid AttributeError on early failure
        # Track channel post cooldowns: {channel_id: last_post_timestamp}
        self._channel_cooldowns: dict[str, float] = {}
        # Track posted message IDs for engagement checking: [{msg_id, channel_id, timestamp}]
        self._posted_messages: list[dict] = []
        # Validation failures from last tick (fed back into context)
        self._last_validation_failures: list[str] = []

    def _auto_channel_candidates(self) -> list[str]:
        """Stable, cooldown-aware target list for autonomous posts/tools."""
        now_ts = time.time()
        channels = []
        for raw_cid in sorted(self.bot._auto_channels or set(), key=str):
            cid = re.sub(r"[^0-9]", "", str(raw_cid))
            if not cid:
                continue
            last_post = self._channel_cooldowns.get(cid, 0)
            if now_ts - last_post >= CHANNEL_COOLDOWN_SECONDS:
                channels.append(cid)
        return channels

    def _channel_allowed(self, channel_id: str) -> bool:
        """Check if autonomy should interact with this channel.
        
        CRITICAL: must stay in sync with bot.py on_message channel guards.
        Autonomy was posting to blocked/missing-allowed channels because
        nobody remembered this check exists. Don't remove it.
        """
        control = self.bot._control or {}
        cid = str(channel_id)
        if not control.get("bot_enabled", True):
            return False
        if cid in set(control.get("blocked_channels", []) or []):
            return False
        allowed = set(control.get("allowed_channels", []) or [])
        return not allowed or cid in allowed

    def _autonomy_tool_allowed(self, name: str) -> bool:
        """Check if autonomy can use a tool, respecting dashboard controls.
        
        CRITICAL: without this, autonomy bypasses tools_enabled/disabled_tools.
        The LLM was calling shell/kilo/create_channel through autonomy even when
        the admin disabled them in the dashboard. Don't remove this gate.
        """
        if name in AUTONOMY_DISABLED_TOOLS:
            return False
        control = self.bot._control or {}
        if not control.get("tools_enabled", True):
            return False
        if name in set(control.get("disabled_tools", []) or []):
            return False
        return True

    # -- lifecycle (idempotent) --

    async def start(self):
        """Start the background loop. Safe to call multiple times."""
        if self._task is not None and not self._task.done():
            return  # already running
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("AutonomyEngine started")

    async def stop(self):
        """Graceful shutdown."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("AutonomyEngine stopped")

    # -- main loop --

    async def _loop(self):
        consecutive_failures = 0
        MAX_AUTONOMY_INTERVAL = 86400  # 24h cap — don't let a bad value sleep forever
        while self._running:
            try:
                control = self.bot._control or {}
                if control.get("autonomy_enabled", False):
                    tick_result = await self.tick()
                    if tick_result.get("error"):
                        consecutive_failures += 1
                    elif not tick_result.get("skipped"):
                        consecutive_failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                consecutive_failures += 1
                logger.error(f"AutonomyEngine tick error: {e}", exc_info=True)
                try:
                    await self.store.record_error(str(e))
                except Exception as rec_err:
                    logger.error(f"Failed to record autonomy error to store: {rec_err}")
            # read interval from bot control (source of truth)
            try:
                control = self.bot._control or {}
                interval = max(30, min(int(control.get("autonomy_interval_seconds", 300) or 300), MAX_AUTONOMY_INTERVAL))
            except (ValueError, TypeError):
                interval = 300
            # Smoother backoff: cap at 6x instead of 10x
            # 1 fail=2x, 2=4x, 3+=6x (cap). With 300s base: max 30 min, not 50.
            backoff = min(2 ** consecutive_failures, 6) if consecutive_failures > 0 else 1
            await asyncio.sleep(max(30, interval * backoff))

    # -- single tick --

    async def tick(self) -> dict:
        """One autonomy cycle. Skipped if previous tick still running."""
        if self._lock.locked():
            logger.debug("Autonomy tick skipped — previous still running")
            return {"skipped": True}
        async with self._lock:
            # BUG FIX: capture tick START time as watermark. Events recorded during
            # plan/execute have timestamps between start and end. Using end-of-tick
            # as watermark (old behavior) drops those events from the next tick.
            tick_start_iso = _utcnow_iso()
            start = time.time()
            try:
                context = await self.gather_context()
                actions = await self.plan(context)
                results = await self.execute(actions)
                duration = time.time() - start
                await self._log_tick(context, actions, results, duration, tick_start_iso)
                return {"skipped": False, "actions": len(results), "duration": duration}
            except Exception as e:
                duration = time.time() - start
                logger.error(f"Autonomy tick failed: {e}")
                await self.store.patch_state({
                    "last_tick": tick_start_iso,
                    "last_tick_duration": round(duration, 2),
                    "last_error": str(e)[:2000],
                })
                return {"skipped": False, "error": str(e), "duration": duration}

    # -----------------------------------------------------------------------
    # gather_context — ordered by decision-relevance, per-section budgets
    # -----------------------------------------------------------------------

    async def gather_context(self) -> str:
        """Collect everything Maxwell currently knows. Sections ordered by
        decision-relevance: most actionable info first, so it survives budget
        truncation. Each section has its own char budget instead of the old
        global truncation that ate channel activity first."""

        sections = []
        # Use system local time so the LLM doesn't see UTC and think it's
        # night when it's 5pm. No hardcoding offsets — let the OS decide.
        now = datetime.now().astimezone()

        # 1. Current time + mood framing
        tz_name = now.tzname() or "local time"
        sections.append(
            f"=== CURRENT TIME ===\n"
            f"{now.strftime('%A, %Y-%m-%d %H:%M')} ({tz_name})"
        )

        # 2. Active goals (most decision-relevant — what should I work on?)
        try:
            goals = await self.store.load_goals()
            active_goals = [g for g in goals if g.get("active")]
            if active_goals:
                goal_lines = [
                    f"- [{g['id']}] {g.get('description', '')} (last acted: {g.get('last_acted_on', 'never')})"
                    for g in active_goals
                ]
                sections.append(_truncate(
                    "=== ACTIVE GOALS ===\n" + "\n".join(goal_lines),
                    CTX_BUDGET_GOALS,
                ))
            else:
                sections.append("=== ACTIVE GOALS ===\n(no active goals)")
        except Exception as e:
            sections.append(f"=== ACTIVE GOALS ===\n(error: {e})")

        # 3. Recent REM events (what just happened in the server?)
        events = []
        try:
            state = await self.store.load_state()
            last_tick = state.get("last_tick")
            events = await self.bot.rem_log.drain_slice(last_tick)
            if events:
                ev_lines = []
                for ev in events[-30:]:
                    role = ev.get("role", "?")
                    content = str(ev.get("content", ""))[:200]
                    ev_lines.append(f"[{role}] {content}")
                sections.append(_truncate(
                    "=== RECENT CONVERSATIONS (since last check) ===\n" + "\n".join(ev_lines),
                    CTX_BUDGET_RECENT_EVENTS,
                ))
            else:
                sections.append("=== RECENT CONVERSATIONS ===\n(no new activity since last check)")
        except Exception as e:
            sections.append(f"=== RECENT CONVERSATIONS ===\n(error: {e})")

        # 4. Channel activity (what's happening right now?)
        channel_ids_to_check = set()
        try:
            channel_ids_to_check.update(self.bot._auto_channels or set())
        except Exception:
            pass
        try:
            for ev in (events or [])[-20:]:
                cid = ev.get("channel_id")
                if cid:
                    channel_ids_to_check.add(str(cid))
        except Exception:
            pass

        ch_lines = []
        for cid in list(channel_ids_to_check)[:10]:
            if not self._channel_allowed(cid):
                continue
            try:
                ch = self.bot.get_channel(int(cid))
                if ch is None:
                    try:
                        ch = await self.bot.fetch_channel(int(cid))
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        continue
                if ch is None:
                    continue
                messages = [m async for m in ch.history(limit=10)]
                for m in reversed(messages):
                    author_name = getattr(m.author, "display_name", None) or getattr(m.author, "name", "?")
                    content = (m.content or "")[:150]
                    if content:
                        ch_lines.append(f"[#{getattr(ch, 'name', cid)}] {author_name}: {content}")
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                continue
            except Exception:
                continue
        if ch_lines:
            sections.append(_truncate(
                "=== CHANNEL ACTIVITY ===\n" + "\n".join(ch_lines[-40:]),
                CTX_BUDGET_CHANNEL_ACTIVITY,
            ))
        else:
            sections.append("=== CHANNEL ACTIVITY ===\n(no accessible channels)")

        # 5. Recent autonomy actions + validation failures (feedback loop)
        action_feedback = []
        try:
            log_entries = await self.store.load_log()
            recent = log_entries[-10:] if log_entries else []
            if recent:
                action_lines = [
                    f"[{e.get('timestamp', '?')}] {e.get('action_kind', '?')}: "
                    f"{e.get('content_summary', '')[:100]} -> {e.get('result', '?')}"
                    for e in recent
                ]
                action_feedback.append("\n".join(action_lines))
        except Exception:
            pass

        # Include validation failures from last tick so LLM learns
        if self._last_validation_failures:
            action_feedback.append(
                "YOUR ACTIONS THAT WERE REJECTED LAST TICK (do NOT repeat these):\n"
                + "\n".join(f"- {f}" for f in self._last_validation_failures)
            )

        if action_feedback:
            sections.append(_truncate(
                "=== YOUR RECENT ACTIONS ===\n" + "\n\n".join(action_feedback),
                CTX_BUDGET_RECENT_ACTIONS,
            ))

        # 6. Engagement tracking (did anyone react to or reply to your posts?)
        engagement = await self._check_post_engagement()
        if engagement:
            sections.append(f"=== ENGAGEMENT WITH YOUR POSTS ===\n{engagement}")

        # 7. DM history
        dm_lines = []
        for channel in list(getattr(self.bot, "private_channels", []) or [])[:20]:
            try:
                messages = [m async for m in channel.history(limit=20)]
                for m in reversed(messages):
                    author_name = getattr(m.author, "display_name", None) or getattr(m.author, "name", "?")
                    content = (m.content or "")[:200]
                    if content:
                        dm_lines.append(f"[DM:{channel.id}] {author_name} ({m.author.id}): {content}")
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                continue
            except Exception:
                continue
        if dm_lines:
            sections.append(_truncate(
                "=== DM HISTORY ===\n" + "\n".join(dm_lines[-40:]),
                CTX_BUDGET_DM_HISTORY,
            ))
        else:
            sections.append("=== DM HISTORY ===\n(no accessible DMs)")

        # 8. Long-term memory
        try:
            ltm = self.bot.memory.get_long_term_memory()
            if ltm:
                ltm_text = "\n".join(str(m) for m in ltm[:30])
                sections.append(_truncate(
                    f"=== LONG-TERM MEMORY ===\n{ltm_text}",
                    CTX_BUDGET_LTM,
                ))
        except Exception as e:
            sections.append(f"=== LONG-TERM MEMORY ===\n(error: {e})")

        # 9. Available channels map (use these IDs for post_channel)
        ch_map_lines = []
        for guild in self.bot.guilds:
            for ch in guild.text_channels:
                try:
                    perms = ch.permissions_for(guild.me)
                    if perms.send_messages and self._channel_allowed(ch.id):
                        ch_map_lines.append(f"  #{ch.name} ({ch.id}) in {guild.name}")
                except Exception:
                    continue
        if ch_map_lines:
            sections.append(_truncate(
                "=== AVAILABLE CHANNELS (use these IDs for post_channel) ===\n" + "\n".join(ch_map_lines[:30]),
                CTX_BUDGET_CHANNELS_MAP,
            ))

        # 10. Shared context
        try:
            shared = await self.bot.memory.get_relevant_shared_context(
                user_id="",
                guild_id="",
                channel_id="",
                is_dm=False,
                is_admin=False,
                max_items=20,
                budget=CTX_BUDGET_SHARED,
            ) if hasattr(self.bot.memory, "get_relevant_shared_context") else []
            if shared:
                ctx_lines = [f"- [{c.get('scope', '?')}, i{c.get('importance', '?')}] {c.get('content', '')}" for c in shared[:20]]
                sections.append(_truncate(
                    "=== SHARED CONTEXT ===\n" + "\n".join(ctx_lines),
                    CTX_BUDGET_SHARED,
                ))
        except Exception:
            pass

        full = "\n\n".join(sections)
        return full

    async def _check_post_engagement(self) -> str:
        """Check if recent autonomous posts got reactions or replies."""
        if not self._posted_messages:
            return ""

        # Only check posts from the last 2 hours
        cutoff = time.time() - 7200
        self._posted_messages = [p for p in self._posted_messages if p.get("ts", 0) > cutoff]

        engagement_lines = []
        for post in self._posted_messages[-5:]:  # check last 5 posts
            try:
                channel = self.bot.get_channel(int(post["channel_id"]))
                if channel is None:
                    continue
                msg = await channel.fetch_message(post["msg_id"])
                if msg is None:
                    continue

                reactions = []
                for r in msg.reactions:
                    reactions.append(f"{r.emoji} ({r.count})")

                # Check for replies (messages that reference this post)
                reply_count = 0
                try:
                    async for reply in channel.history(limit=10, after=msg.created_at):
                        if reply.reference and reply.reference.message_id == msg.id:
                            reply_count += 1
                except Exception:
                    pass

                parts = []
                if reactions:
                    parts.append(f"reactions: {', '.join(reactions)}")
                if reply_count:
                    parts.append(f"{reply_count} replies")
                if parts:
                    ch_name = getattr(channel, "name", post["channel_id"])
                    engagement_lines.append(
                        f"Your message in #{ch_name}: {'; '.join(parts)}"
                    )
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                continue
            except Exception:
                continue

        return "\n".join(engagement_lines) if engagement_lines else ""

    # -----------------------------------------------------------------------
    # plan
    # -----------------------------------------------------------------------

    async def plan(self, context: str) -> list[dict]:
        """Ask the LLM what to do. Returns validated action list."""
        # build tool descriptions (excluding autonomy-incompatible tools)
        tool_desc_lines = []
        for name, tool in self.bot.tools.items():
            if not self._autonomy_tool_allowed(name):
                continue
            try:
                desc = tool.get_description()
                tool_desc_lines.append(f"- {name}: {desc}")
            except Exception:
                tool_desc_lines.append(f"- {name}: (description unavailable)")
        tool_descriptions = "\n".join(tool_desc_lines) if tool_desc_lines else "(no tools available)"

        # goals text
        try:
            goals = await self.store.load_goals()
            active_goals = [g for g in goals if g.get("active")]
            goals_text = "\n".join(
                f"- [{g['id']}] {g.get('description', '')}" for g in active_goals
            ) if active_goals else "(no active goals)"
        except Exception:
            goals_text = "(error loading goals)"

        # Check channel cooldowns for the prompt
        now_ts = time.time()
        cooldown_info = []
        for cid, last_post in self._channel_cooldowns.items():
            remaining = int(CHANNEL_COOLDOWN_SECONDS - (now_ts - last_post))
            if remaining > 0:
                cooldown_info.append(f"  channel {cid}: wait {remaining}s")
        cooldown_text = ""
        if cooldown_info:
            cooldown_text = (
                "\n\nCHANNEL COOLDOWNS (do NOT post to these channels yet):\n"
                + "\n".join(cooldown_info)
            )

        system_prompt = f"""You are Maxwell, doing a periodic check-in on your own. You're reviewing what's happening in your Discord server and deciding if there's something worth doing.

Here is everything you currently know:
{context}

You have access to these tools:
{tool_descriptions}

Your goals:
{goals_text}
{cooldown_text}

IMPORTANT RULES:
- The channel activity and recent conversations above are REAL data. Don't try to fetch more — it's already here.
- If conversations are happening and you have something interesting/funny/useful to say, say it.
- If someone mentioned you or talked about you, respond.
- If a conversation naturally concluded, don't re-open it unless you have a genuinely good reason.
- If nothing needs doing, that's FINE. Output do_nothing with a reason.
- NEVER call search_messages — the channel history is already provided above.
- ALWAYS use "post_channel" with a "target_channel_id" from the AVAILABLE CHANNELS list.
- Don't post to channels on cooldown.
- Don't repeat things you already posted recently (check YOUR RECENT ACTIONS).
- You can use up to {MAX_ACTIONS_PER_TICK} actions per tick.

Return ONLY valid JSON in this exact format:
{{
  "thought": "what you're thinking about the current situation",
  "actions": [
    {{
      "kind": "post_channel",
      "target_channel_id": "123456789",
      "content": "your message here",
      "reason": "why you're posting this"
    }},
    {{
      "kind": "send_dm",
      "target_user_id": "123456789",
      "content": "hey, about that thing...",
      "reason": "following up on earlier conversation"
    }},
    {{
      "kind": "run_tool",
      "tool_name": "web_search",
      "tool_args": {{"query": "something worth checking"}},
      "target_channel_id": "123456789",
      "reason": "need fresh info before posting"
    }},
    {{
      "kind": "update_memory",
      "content": "important fact to remember",
      "reason": "useful for future conversations"
    }},
    {{
      "kind": "create_goal",
      "description": "specific actionable goal",
      "reason": "why this matters"
    }},
    {{
      "kind": "do_nothing",
      "reason": "nothing needs my attention right now"
    }}
  ]
}}

Valid action kinds: send_dm, post_channel, run_tool, update_memory, create_goal, do_nothing.
Do NOT invent other action kinds — they will be rejected."""

        # call the LLM
        try:
            messages = [{"role": "system", "content": system_prompt}]
            timeout = max(30, int(
                (self.bot._control or {}).get("ai_timeout_seconds", 180) or 180
            ))
            await self.bot._acquire_ai_slot(timeout=timeout)
            try:
                raw_response = await self.bot.ai_provider.generate_response(
                    messages, timeout=timeout
                )
            finally:
                await self.bot._release_ai_slot()
        except Exception as e:
            logger.error(f"Autonomy LLM call failed: {e}")
            return [{"kind": "do_nothing", "reason": f"LLM call failed: {e}"}]

        # parse JSON from response
        logger.info(f"Autonomy LLM response ({len(raw_response or '')} chars): {(raw_response or '')[:500]}")
        actions, validation_failures = self._parse_plan(raw_response)

        # Store validation failures for next tick's feedback
        self._last_validation_failures = validation_failures

        return actions

    def _parse_plan(self, raw: str) -> tuple[list[dict], list[str]]:
        """Extract and validate the JSON plan from LLM output.
        Returns (valid_actions, validation_failures)."""
        validation_failures = []

        if not raw:
            return [{"kind": "do_nothing", "reason": "empty LLM response"}], validation_failures

        # extract JSON block — try pure JSON first, then markdown fences, then find/rfind
        text = str(raw).strip()
        json_str = None
        # 1. try pure JSON
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                json_str = text
        except (json.JSONDecodeError, ValueError):
            pass
        # 2. try markdown code fence ```json ... ```
        if json_str is None:
            m = re.search(r"```(?:json)?\s*\n?(\{[^`]*)\s*```", text, re.DOTALL)
            if m:
                json_str = m.group(1)
        # 3. fallback: collect all balanced { ... } blocks, prefer the one with "actions"
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
                                candidates.append(text[i:j + 1])
                                i = j
                                break
                i += 1
            for c in candidates:
                try:
                    obj = json.loads(c)
                    if isinstance(obj, dict) and "actions" in obj:
                        json_str = c
                        break
                except json.JSONDecodeError:
                    pass
            if json_str is None and candidates:
                json_str = candidates[0]
        if json_str is None:
            logger.warning(f"Autonomy planner returned no JSON. Raw: {text[:500]}")
            return [{"kind": "do_nothing", "reason": "no JSON in LLM response"}], validation_failures

        try:
            parsed = json.loads(json_str)
        except json.JSONDecodeError as e:
            logger.warning(f"Autonomy planner JSON parse failed: {e}. Raw: {json_str[:500]}")
            return [{"kind": "do_nothing", "reason": "invalid JSON from planner"}], validation_failures

        if not isinstance(parsed, dict):
            return [{"kind": "do_nothing", "reason": "planner returned non-object"}], validation_failures

        # save thought
        thought = str(parsed.get("thought", ""))[:2000]
        self._last_thought = thought

        raw_actions = parsed.get("actions", [])
        if not isinstance(raw_actions, list):
            return [{"kind": "do_nothing", "reason": "actions not a list"}], validation_failures

        # validate strictly
        valid = []
        for action in raw_actions[:MAX_ACTIONS_PER_TICK]:
            if not isinstance(action, dict):
                continue
            kind = str(action.get("kind", "")).strip().lower()
            # LLM keeps inventing action kind names — map common aliases
            _KIND_ALIASES = {
                "send_message": "post_channel",
                "send_msg": "post_channel",
                "message": "post_channel",
                "reply": "post_channel",
                "dm": "send_dm",
                "direct_message": "send_dm",
                "reasoning_log": "do_nothing",
                "think": "do_nothing",
                "log": "do_nothing",
            }
            original_kind = kind
            kind = _KIND_ALIASES.get(kind, kind)
            if kind not in AUTONOMY_VALID_KINDS:
                msg = f"unknown action kind '{original_kind}'"
                logger.info(f"Dropping {msg} | raw: {json.dumps(action, default=str)[:300]}")
                validation_failures.append(msg)
                continue

            if kind == "send_dm":
                uid_raw = str(action.get("target_user_id", ""))
                uid = re.sub(r"[^0-9]", "", uid_raw)
                content = str(action.get("content", "")).strip()
                if not uid or not content:
                    validation_failures.append("send_dm: missing user_id or content")
                    continue
                valid.append({
                    "kind": "send_dm",
                    "target_user_id": uid,
                    "content": content[:MAX_CONTENT_CHARS],
                    "reason": str(action.get("reason", ""))[:500],
                })

            elif kind == "post_channel":
                cid_raw = str(action.get("target_channel_id", ""))
                cid = re.sub(r"[^0-9]", "", cid_raw)
                content = str(action.get("content", "")).strip()
                if not content:
                    validation_failures.append("post_channel: empty content")
                    continue
                if not cid:
                    candidates = self._auto_channel_candidates()
                    if candidates:
                        cid = candidates[0]
                if not cid:
                    validation_failures.append("post_channel: no channel_id and no auto_channels")
                    continue
                # Check channel cooldown
                now_ts = time.time()
                last_post = self._channel_cooldowns.get(cid, 0)
                if now_ts - last_post < CHANNEL_COOLDOWN_SECONDS:
                    remaining = int(CHANNEL_COOLDOWN_SECONDS - (now_ts - last_post))
                    validation_failures.append(f"post_channel: channel {cid} on cooldown ({remaining}s remaining)")
                    continue
                valid.append({
                    "kind": "post_channel",
                    "target_channel_id": cid,
                    "content": content[:MAX_CONTENT_CHARS],
                    "reason": str(action.get("reason", ""))[:500],
                })

            elif kind == "run_tool":
                tool_name = str(action.get("tool_name", "")).strip()
                if not tool_name:
                    validation_failures.append("run_tool: missing tool_name")
                    continue
                if not self._autonomy_tool_allowed(tool_name):
                    validation_failures.append(f"run_tool: '{tool_name}' is disabled or not allowed")
                    continue
                if tool_name not in self.bot.tools:
                    validation_failures.append(f"run_tool: tool '{tool_name}' not found")
                    continue
                tool_args = action.get("tool_args", {})
                if not isinstance(tool_args, dict):
                    tool_args = {}
                safe_args = {str(k): v for k, v in tool_args.items()}
                valid.append({
                    "kind": "run_tool",
                    "tool_name": tool_name,
                    "tool_args": safe_args,
                    "reason": str(action.get("reason", ""))[:500],
                })

            elif kind == "update_memory":
                content = str(action.get("content", "")).strip()
                if not content:
                    validation_failures.append("update_memory: empty content")
                    continue
                valid.append({
                    "kind": "update_memory",
                    "content": content[:MAX_CONTENT_CHARS],
                    "reason": str(action.get("reason", ""))[:500],
                })

            elif kind == "create_goal":
                desc = str(action.get("description", "")).strip()
                if not desc:
                    validation_failures.append("create_goal: empty description")
                    continue
                valid.append({
                    "kind": "create_goal",
                    "description": desc[:500],
                    "reason": str(action.get("reason", ""))[:500],
                })

            elif kind == "do_nothing":
                valid.append({
                    "kind": "do_nothing",
                    "reason": str(action.get("reason", "no reason"))[:500],
                })

        if not valid:
            logger.warning(f"All {len(raw_actions)} actions failed validation. Raw response: {raw[:1000]}")
            valid = [{"kind": "do_nothing", "reason": "all actions failed validation"}]

        if not any(a["kind"] != "do_nothing" for a in valid):
            logger.info(f"Autonomy planner produced no actionable items. Thought: {thought[:300]}")
        return valid, validation_failures

    # -----------------------------------------------------------------------
    # execute
    # -----------------------------------------------------------------------

    async def execute(self, actions: list[dict]) -> list[dict]:
        """Execute each action. One failure doesn't kill the rest."""
        results = []
        ACTION_TIMEOUT = 30  # seconds per action
        for action in actions:
            # bail if bot disconnected mid-tick
            if self.bot.is_closed():
                logger.warning("Bot disconnected during autonomy tick, aborting remaining actions")
                break

            kind = action.get("kind", "do_nothing")
            result = {"kind": kind, "result": "success", "error": None}
            try:
                if kind == "send_dm":
                    await asyncio.wait_for(self._exec_send_dm(action, result), timeout=ACTION_TIMEOUT)
                elif kind == "post_channel":
                    await asyncio.wait_for(self._exec_post_channel(action, result), timeout=ACTION_TIMEOUT)
                elif kind == "run_tool":
                    await asyncio.wait_for(self._exec_run_tool(action, result), timeout=ACTION_TIMEOUT)
                elif kind == "update_memory":
                    await asyncio.wait_for(self._exec_update_memory(action, result), timeout=ACTION_TIMEOUT)
                elif kind == "create_goal":
                    await asyncio.wait_for(self._exec_create_goal(action, result), timeout=ACTION_TIMEOUT)
                elif kind == "do_nothing":
                    result["result"] = "skipped"
                    result["content_summary"] = action.get("reason", "no reason")
                else:
                    result["result"] = "skipped"
                    result["error"] = f"unknown kind: {kind}"
            except asyncio.TimeoutError:
                result["result"] = "error"
                result["error"] = f"action timed out after {ACTION_TIMEOUT}s"
                logger.warning(f"Autonomy action {kind} timed out after {ACTION_TIMEOUT}s")
            except Exception as e:
                result["result"] = "error"
                result["error"] = str(e)[:1000]
                logger.error(f"Autonomy action {kind} failed: {e}")
            results.append(result)

            # record in REM event log (skip do_nothing)
            if kind != "do_nothing":
                try:
                    summary = result.get("content_summary", action.get("reason", kind))
                    await self.bot.rem_log.record({
                        "ts": _utcnow_iso(),
                        "channel_id": "",
                        "guild_id": None,
                        "user_id": str(self.bot.user.id) if self.bot.user else "",
                        "user_name": self.bot.bot_name,
                        "role": "assistant",
                        "content": f"[autonomy] {kind}: {str(summary)[:300]}",
                        "auto_mode": False,
                    })
                except Exception as e:
                    logger.warning(f"Failed to record autonomy REM event: {e}")

        return results

    async def _exec_send_dm(self, action: dict, result: dict):
        user_id = action["target_user_id"]
        content = action["content"][:MAX_CONTENT_CHARS]
        result["target"] = f"user:{user_id}"
        result["content_summary"] = content[:200]

        user = self.bot.get_user(int(user_id))
        if user is None:
            try:
                user = await self.bot.fetch_user(int(user_id))
            except (discord.NotFound, discord.HTTPException, ValueError) as e:
                result["result"] = "error"
                result["error"] = f"user not found or API error: {e}"
                return
        if user is None:
            result["result"] = "error"
            result["error"] = "user not found"
            return

        dm_channel = None
        for ch in list(getattr(self.bot, "private_channels", []) or []):
            if isinstance(ch, discord.DMChannel):
                recipient = getattr(ch, "recipient", None)
                if recipient and str(recipient.id) == str(user_id):
                    dm_channel = ch
                    break
        if dm_channel is None:
            try:
                dm_channel = await user.create_dm()
            except discord.HTTPException as e:
                result["result"] = "error"
                result["error"] = f"failed to create DM channel (user may have DMs disabled): {e}"
                return

        try:
            msg = await dm_channel.send(content)
            result["tool_called"] = "send_dm"
            # Track for engagement checking
            if msg:
                self._posted_messages.append({
                    "msg_id": msg.id,
                    "channel_id": str(dm_channel.id),
                    "ts": time.time(),
                })
        except discord.Forbidden:
            result["result"] = "error"
            result["error"] = "user has DMs disabled or blocked the bot"
            return
        except discord.HTTPException as e:
            result["result"] = "error"
            result["error"] = f"Discord API error sending DM: {e}"
            return

    async def _exec_post_channel(self, action: dict, result: dict):
        channel_id = action["target_channel_id"]
        content = action["content"][:2000]
        result["target"] = f"channel:{channel_id}"
        result["content_summary"] = content[:200]

        channel = self.bot.get_channel(int(channel_id))
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(int(channel_id))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                channel = None
        if channel is None:
            result["result"] = "error"
            result["error"] = "channel not found"
            return

        try:
            msg = await channel.send(content)
            result["tool_called"] = "post_channel"
            # Update cooldown
            self._channel_cooldowns[channel_id] = time.time()
            # Track for engagement checking
            if msg:
                self._posted_messages.append({
                    "msg_id": msg.id,
                    "channel_id": channel_id,
                    "ts": time.time(),
                })
        except discord.Forbidden:
            result["result"] = "error"
            result["error"] = "bot lacks permission to send in this channel"
        except discord.HTTPException as e:
            result["result"] = "error"
            result["error"] = f"Discord API error: {e}"

    async def _exec_run_tool(self, action: dict, result: dict):
        tool_name = action["tool_name"]
        tool_args = action.get("tool_args", {})
        result["target"] = f"tool:{tool_name}"
        result["tool_called"] = tool_name
        result["tool_args"] = tool_args
        result["content_summary"] = f"{tool_name}({json.dumps(tool_args, default=str)[:150]})"

        if not self._autonomy_tool_allowed(tool_name):
            result["result"] = "error"
            result["error"] = f"tool disabled for autonomy: {tool_name}"
            return

        tool = self.bot.tools.get(tool_name)
        if tool is None:
            result["result"] = "error"
            result["error"] = f"tool not found: {tool_name}"
            return

        # resolve a channel if the action provides one
        channel = None
        target_cid = action.get("target_channel_id") or tool_args.get("channel_id")
        if target_cid:
            try:
                channel = self.bot.get_channel(int(target_cid))
                if channel is None:
                    channel = await self.bot.fetch_channel(int(target_cid))
            except (ValueError, TypeError, discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

        # if no channel and we can find a default, use the first auto_channel
        if channel is None:
            for cid in self._auto_channel_candidates():
                try:
                    channel = self.bot.get_channel(int(cid))
                    if channel is None:
                        channel = await self.bot.fetch_channel(int(cid))
                except (ValueError, TypeError, discord.NotFound, discord.Forbidden, discord.HTTPException):
                    continue
                if channel:
                    break

        if channel is None:
            result["result"] = "error"
            result["error"] = "no channel available for tool execution"
            return

        # build synthetic message
        author = self.bot.user
        guild = channel.guild if hasattr(channel, "guild") else None
        syn_msg = SyntheticMessage(
            channel=channel,
            author=author,
            guild=guild,
            content=tool_args.get("content", tool_args.get("prompt", "")),
        )

        # extract tool kwargs (exclude meta fields)
        exec_kwargs = {k: v for k, v in tool_args.items() if k not in {"channel_id"}}
        try:
            tool_result = await tool.execute(syn_msg, **exec_kwargs)
            result["result"] = "success"
            result["content_summary"] = str(tool_result)[:300] if tool_result else result["content_summary"]
        except Exception as e:
            result["result"] = "error"
            result["error"] = str(e)[:1000]

    async def _exec_update_memory(self, action: dict, result: dict):
        content = action["content"][:MAX_CONTENT_CHARS]
        result["content_summary"] = content[:200]
        result["target"] = "memory"

        try:
            await self.bot.memory.add_long_term_memory(content)
            result["tool_called"] = "add_long_term_memory"
        except Exception as e:
            result["result"] = "error"
            result["error"] = str(e)[:1000]

    async def _exec_create_goal(self, action: dict, result: dict):
        desc = action["description"][:500]
        result["content_summary"] = desc[:200]
        result["target"] = "goals"

        goal = await self.store.add_goal(desc)
        result["tool_called"] = "create_goal"
        result["goal_id"] = goal.get("id")
        if goal.get("error"):
            result["result"] = "error"
            result["error"] = goal["error"]

    # -----------------------------------------------------------------------
    # logging
    # -----------------------------------------------------------------------

    async def _log_tick(self, context: str, actions: list[dict], results: list[dict], duration: float, tick_start_iso: str | None = None):
        """Record tick results to state and action log."""
        thought = self._last_thought or ""

        # update state + bump counters in ONE locked operation (no TOCTOU race)
        total_exec = sum(1 for r in results if r.get("result") == "success")
        total_fail = sum(1 for r in results if r.get("result") == "error")

        # BUG FIX: use tick START time as watermark so events recorded during
        # plan/execute are not dropped from the next tick.
        def _update(s):
            s["last_tick"] = tick_start_iso or _utcnow_iso()
            s["last_tick_duration"] = round(duration, 2)
            s["last_error"] = None
            s["last_thought"] = thought[:2000]
            s["actions_executed_total"] = s.get("actions_executed_total", 0) + total_exec
            s["actions_failed_total"] = s.get("actions_failed_total", 0) + total_fail

        await self.store.update_state(_update)

        # log each action
        for action, result in zip(actions, results):
            entry = {
                "id": f"action_{uuid.uuid4().hex[:8]}",
                "timestamp": _utcnow_iso(),
                "thought": thought[:1000],
                "action_kind": action.get("kind", "unknown"),
                "target": result.get("target", ""),
                "content_summary": result.get("content_summary", "")[:300],
                "tool_called": result.get("tool_called", ""),
                "tool_args": result.get("tool_args", {}),
                "result": result.get("result", "unknown"),
                "error": result.get("error"),
            }
            await self.store.append_log_entry(entry)
