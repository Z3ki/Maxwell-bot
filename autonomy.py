"""AutonomyEngine — Maxwell's self-directed life loop.

Runs alongside REM and on_message. Wakes every N seconds, gathers context
(DMs, channel history, memory, goals, recent events), asks the LLM what to
do, and executes actions through the existing tool system.

No approval queues. No shadow mode. Maxwell decides, Maxwell acts.

MAINTAINER NOTES:
- The old version had a self-defeating prompt ("nobody messaged you" then
  "check if anyone messaged you"). The LLM defaulted to do_nothing 75% of
  the time. Don't add that shit back.
- Autonomy now exposes every dashboard-enabled tool. If a tool needs a real
  Discord message, SyntheticMessage has to point at target_message_id. Yes,
  this is more annoying. The user explicitly asked for all tools.
- The context budget is PER-SECTION now, not global truncation. The old
  version truncated from the end, so channel activity (the most actionable
  data) got eaten first. Don't "simplify" back to global truncation.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import discord

from control_defaults import (
    DEFAULT_CONTROL,
)  # noqa: E402
from utils import (  # noqa: E402
    _atomic_json_write_sync,
    _coerce_utc_datetime as _coerce_utc_dt_shared,
    _discord_display_name as _discord_display_name_shared,
    _discord_id as _discord_id_shared,
    render_discord_context_text as _render_discord_context_text,
)

logger = logging.getLogger(__name__)

# Regex constants, _discord_display_name, _discord_id, _coerce_utc_datetime,
# and _render_discord_context_text are now imported from utils.py


def _user_ref(obj: Any, bot_user: Any = None) -> str:
    uid = _discord_id(obj)
    if bot_user is not None and uid == str(getattr(bot_user, "id", "")):
        return f"you/Maxwell({uid})"
    return f"{_discord_display_name(obj)}({uid})"


def _visible_message_content(message: Any, content: str | None = None) -> str:
    text = _render_discord_context_text(message, content)
    parts = [text] if text else []
    for attachment in list(getattr(message, "attachments", []) or [])[:5]:
        content_type = getattr(attachment, "content_type", "") or ""
        if content_type.startswith("image/"):
            kind = "image"
        elif content_type.startswith("audio/"):
            kind = "audio"
        elif content_type.startswith("video/"):
            kind = "video"
        else:
            kind = "file"
        name = getattr(attachment, "filename", "")
        parts.append(f"[{kind}: {name}]" if name else f"[{kind}]")
    if getattr(message, "embeds", None):
        parts.append("[embed]")
    return " ".join(p for p in parts if p).strip()


def _message_relation_tags(
    message: Any, *, bot_user: Any = None, reply: Any = None
) -> list[str]:
    tags: list[str] = []
    addressed: list[str] = []

    if getattr(getattr(message, "author", None), "bot", False):
        tags.append("speaker_kind=bot")
    else:
        tags.append("speaker_kind=human")

    if reply is not None and hasattr(reply, "author"):
        ref = _user_ref(reply.author, bot_user)
        tags.append(f"reply_to={ref}")
        addressed.append(f"reply_to:{ref}")

    mentions = list(getattr(message, "mentions", []) or [])[:10]
    if mentions:
        mention_refs = [_user_ref(user, bot_user) for user in mentions]
        tags.append("mentions=[" + ", ".join(mention_refs) + "]")
        addressed.extend(f"mention:{ref}" for ref in mention_refs)
        if bot_user is not None and any(
            str(getattr(user, "id", "")) == str(getattr(bot_user, "id", ""))
            for user in mentions
        ):
            tags.append("mentions_you")

    tags.append(
        "addressed_to=[" + "; ".join(addressed) + "]"
        if addressed
        else "addressed_to=channel"
    )
    return tags


def _format_memory_context_line(msg: dict, *, bot_user: Any = None, now=None) -> str:
    stamp = _context_time(msg.get("timestamp"), now=now)
    prefix = f"[{stamp}] " if stamp else ""
    author = str(msg.get("author", "?"))
    author_id = str(msg.get("author_id") or "")
    bot_id = str(getattr(bot_user, "id", "")) if bot_user is not None else ""
    bot_name = str(
        getattr(bot_user, "display_name", None) or getattr(bot_user, "name", "") or ""
    )

    if msg.get("is_tool"):
        return f"{prefix}[Tool] {str(msg.get('content', ''))[:600]}"

    if (bot_id and author_id == bot_id) or (
        not author_id and bot_name and author == bot_name
    ):
        label = f"You/Maxwell({author_id})" if author_id else "You/Maxwell"
    else:
        label = f"{author}({author_id})" if author_id else author
        if msg.get("author_is_bot"):
            label += " [bot]"

    relation_bits = []
    if msg.get("reply_to_author"):
        reply_label = str(msg.get("reply_to_author"))
        reply_id = str(msg.get("reply_to_author_id") or "")
        if msg.get("reply_to_self"):
            reply_label = "you/Maxwell"
        relation_bits.append(
            f"reply_to={reply_label}({reply_id})" if reply_id else f"reply_to={reply_label}"
        )
    mentions = msg.get("mentions") if isinstance(msg.get("mentions"), list) else []
    mention_bits = [
        f"@{item.get('name', 'unknown')}({item.get('id', 'unknown')})"
        for item in mentions[:10]
        if isinstance(item, dict)
    ]
    if mention_bits:
        relation_bits.append("mentions=" + ",".join(mention_bits))
    relation = f" [{'; '.join(relation_bits)}]" if relation_bits else ""
    return f"{prefix}{label}{relation}: {str(msg.get('content', ''))[:600]}"


def _conversation_label(bot: Any, channel_id: str) -> str:
    """Human-readable channel/DM label for autonomy context."""
    cid = re.sub(r"[^0-9]", "", str(channel_id or ""))
    if not cid:
        return str(channel_id or "unknown")
    channel = None
    with contextlib.suppress(Exception):
        channel = bot.get_channel(int(cid))
    if channel is None:
        for private in list(getattr(bot, "private_channels", []) or []):
            if str(getattr(private, "id", "")) == cid:
                channel = private
                break
    if isinstance(channel, discord.DMChannel):
        recipient = getattr(channel, "recipient", None)
        if recipient is not None:
            return f"DM with {_user_ref(recipient, getattr(bot, 'user', None))} channel={cid}"
        return f"DM channel={cid}"
    if channel is not None:
        name = getattr(channel, "name", None) or cid
        guild = getattr(channel, "guild", None)
        guild_name = getattr(guild, "name", None)
        if guild_name:
            return f"#{name}({cid}) in {guild_name}"
        return f"#{name}({cid})"
    return f"channel={cid}"


# _render_discord_context_text imported from utils.py


AUTONOMY_VALID_KINDS = frozenset(
    {
        "send_dm",
        "post_channel",
        "run_tool",
        "update_memory",
        "create_goal",
        "do_nothing",
    }
)
MAX_ACTIONS_PER_TICK = 3  # reduced from 5 — prevents spam bursts
MAX_CONTENT_CHARS = 1900
LOG_RING_SIZE = 200

# Tools that post a visible message to a channel. autonomy's run_tool path
# builds a SyntheticMessage against the target channel, so these must be
# treated like post_channel for the unprompted-post rate-limit.
AUTONOMY_POST_TOOLS = frozenset({
    "send_message", "send_file", "send_meme", "send_media", "tts",
})

# Per-section context budgets (sum ~8800, bumped for enriched channel map)
CTX_BUDGET_GOALS = 800
CTX_BUDGET_RECENT_EVENTS = 2000
CTX_BUDGET_CHANNEL_ACTIVITY = 2800
CTX_BUDGET_CHANNEL_MEMORY = 2200
CTX_BUDGET_RECENT_ACTIONS = 1200
CTX_BUDGET_DM_HISTORY = 1200
CTX_BUDGET_LTM = 800
CTX_BUDGET_SHARED = 600
CTX_BUDGET_CHANNELS_MAP = 1600  # bumped from 800 — enriched with topic/recency

# Hard safety: these tools are NEVER available to autonomy even if dashboard
# enables them. Prevents autonomy/LLM from server-admin, shell, site creation,
# or other high-risk actions. (Dashboard disabled_tools still apply too.)
AUTONOMY_DISABLED_TOOLS = frozenset({
    "shell",
    "create_site",
    "sub_agent",
    "list_admin_servers",
    "create_category",
    "create_channel",
    "edit_channel",
    "delete_channel",
    "change_avatar",
    "set_nickname",
    "forward_message",
    "create_invite",
})


# ---------------------------------------------------------------------------
# Atomic JSON helpers (same pattern as memory.py / rem.py)
# ---------------------------------------------------------------------------


# _atomic_json_write_sync imported from utils.py


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
        try:
            path.write_text("{}", encoding="utf-8")
        except Exception:
            pass
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
    return text[: budget - len(suffix)] + suffix


def _truncate_keep_tail(text: str, budget: int) -> str:
    """Keep newest lines when context gets too fat. Front truncation betrayed us."""
    budget = max(0, int(budget or 0))
    if len(text) <= budget:
        return text
    prefix = "[older context truncated] ...\n"
    if budget <= len(prefix):
        return text[-budget:]
    return prefix + text[-max(0, budget - len(prefix)) :]


# _coerce_utc_datetime, _discord_display_name, _discord_id imported from utils.py.
# The utils imports are aliased to *_shared to avoid clashing with bot.py's
# local copies; rebind them to the bare names the helper functions below use.
# Without these, _user_ref() raises NameError, which the channel-activity loop
# silently swallows via `except Exception: continue` — so autonomy sees NO live
# channel activity. Keep these aliases in sync with the utils import block.
_coerce_utc_datetime = _coerce_utc_dt_shared  # local alias for backward compat
_discord_display_name = _discord_display_name_shared
_discord_id = _discord_id_shared


def _relative_time(dt, *, now: datetime | None = None) -> str:
    """Human-readable relative time like '2m ago', '3h ago', 'just now'."""
    dt = _coerce_utc_datetime(dt)
    if dt is None:
        return "?"
    try:
        now = _coerce_utc_datetime(now) or datetime.now(timezone.utc)
        age_s = int((now - dt).total_seconds())
        if age_s < 0:
            return "just now"
        if age_s < 60:
            return f"{age_s}s ago"
        if age_s < 3600:
            return f"{age_s // 60}m ago"
        if age_s < 86400:
            return f"{age_s // 3600}h ago"
        return f"{age_s // 86400}d ago"
    except Exception:
        return "?"


def _context_time(value, *, now: datetime | None = None) -> str:
    dt = _coerce_utc_datetime(value)
    if dt is None:
        return "?"
    return f"{_relative_time(dt, now=now)} / {dt.astimezone().strftime('%a %Y-%m-%d %H:%M')} local"


def _action_feedback_line(entry: dict, *, now: datetime | None = None) -> str:
    when = _context_time(entry.get("timestamp"), now=now)
    kind = str(entry.get("action_kind") or "unknown")
    result = str(entry.get("result") or "?")
    target = str(entry.get("target") or "")
    summary = str(entry.get("content_summary") or "").replace("\n", " ")[:180]
    if kind == "do_nothing":
        # Do not feed old do_nothing prose back into the model. It loves to quote
        # stale "5 minutes ago" guesses like they're fresh facts. Ask me how I know.
        return f"[{when}] did nothing -> {result}"
    if kind in {"post_channel", "send_dm"}:
        where = f" to {target}" if target else ""
        return f"[{when}] {kind}{where}: {summary} -> {result}"
    if kind == "run_tool":
        tool = entry.get("tool_called") or target
        return f"[{when}] ran {tool}: {summary} -> {result}"
    return f"[{when}] {kind}: {summary} -> {result}"


# ---------------------------------------------------------------------------
# Synthetic message for tools that expect a discord.Message
# ---------------------------------------------------------------------------


class SyntheticMessage:
    """Minimal message-like object for tool execution outside on_message."""

    def __init__(self, channel, author, guild, content: str, target_message=None):
        self.channel = channel
        self.author = author
        self.guild = guild
        self.content = content
        self._target_message = target_message
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
        if self._target_message is not None and hasattr(self._target_message, "reply"):
            return await self._target_message.reply(content, **kwargs)
        return await self.channel.send(content, **kwargs)

    async def add_reaction(self, emoji):
        if self._target_message is not None and hasattr(
            self._target_message, "add_reaction"
        ):
            return await self._target_message.add_reaction(emoji)
        raise discord.NotFound(response=None, message="target message not found")

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
    MAX_GOAL_DESC_CHARS = 2000

    async def add_goal(self, description: str) -> dict:
        async with self._lock:
            data = await asyncio.to_thread(_load_json_safe, self.goals_file, dict)
            goals = data.get("goals", []) if isinstance(data, dict) else []
            if not isinstance(goals, list):
                goals = []
            if len(goals) >= self.MAX_GOALS:
                logger.warning(
                    f"Goal limit reached ({self.MAX_GOALS}), rejecting new goal"
                )
                return {
                    "id": None,
                    "description": description,
                    "error": "goal limit reached",
                }
            goal = {
                "id": f"goal_{uuid.uuid4().hex[:8]}",
                "description": str(description)[: self.MAX_GOAL_DESC_CHARS],
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

    def __init__(self, bot: Any):
        self.bot = bot
        self.store = AutonomyStore(bot.config.DATA_DIR)
        self._running = False
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()  # prevents concurrent ticks
        self._last_thought = ""  # avoid AttributeError on early failure
        # Track posted message IDs for engagement checking: [{msg_id, channel_id, timestamp}]
        self._posted_messages: list[dict] = []
        # Validation failures from last tick (fed back into context)
        self._last_validation_failures: list[str] = []

    def _auto_channel_candidates(self) -> list[str]:
        """Stable target list for autonomous posts/tools."""
        channels = []
        for raw_cid in sorted(self.bot._auto_channels or set(), key=str):
            cid = re.sub(r"[^0-9]", "", str(raw_cid))
            if cid:
                channels.append(cid)
        return channels

    def _channel_allowed(self, channel_id: str) -> bool:
        """Check if autonomy should interact with this channel.

        CRITICAL: must stay in sync with bot.py on_message channel guards.
        Autonomy was posting to blocked/missing-allowed channels because
        nobody remembered this check exists. Don't remove it.
        """
        control = getattr(self.bot, "_control", None) or {}
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
        Hard safety denials from AUTONOMY_DISABLED_TOOLS are enforced first.
        """
        if name in AUTONOMY_DISABLED_TOOLS:
            return False
        control = getattr(self.bot, "_control", None) or {}
        if not control.get("tools_enabled", True):
            return False
        return name not in set(control.get("disabled_tools", []) or [])

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
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        logger.info("AutonomyEngine stopped")

    # -- main loop --

    async def _loop(self):
        consecutive_failures = 0
        MAX_AUTONOMY_INTERVAL = 86400  # 24h cap — don't let a bad value sleep forever
        while self._running:
            try:
                control = getattr(self.bot, "_control", None) or {}
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
                control = getattr(self.bot, "_control", None) or {}
                interval = max(
                    30,
                    min(
                        int(control.get("autonomy_interval_seconds", 300) or 300),
                        MAX_AUTONOMY_INTERVAL,
                    ),
                )
            except (ValueError, TypeError):
                interval = 300
            # Smoother backoff: cap at 6x instead of 10x
            # 1 fail=2x, 2=4x, 3+=6x (cap). With 300s base: max 30 min, not 50.
            backoff = min(2**consecutive_failures, 6) if consecutive_failures > 0 else 1
            base_sleep = max(30, interval * backoff)
            # Randomize the tick so autonomy wakes at irregular, lifelike
            # intervals instead of a metronomic fixed cadence. Jitter ranges
            # from half to 1.5x the configured interval — e.g. a 300s base
            # becomes anywhere from ~2.5m to ~7.5m. Keeps the min 30s floor.
            sleep_for = int(base_sleep * random.uniform(0.5, 1.5))
            await asyncio.sleep(max(30, sleep_for))

    # -- single tick --

    async def tick(self) -> dict:
        """One autonomy cycle. Skipped if previous tick still running."""
        if self._lock.locked():
            logger.debug("Autonomy tick skipped — previous still running")
            return {"skipped": True}
        acquired = False
        try:
            await asyncio.wait_for(self._lock.acquire(), timeout=600)
            acquired = True
        except asyncio.TimeoutError:
            logger.error("Autonomy tick lock timed out — previous tick hung for >10m, forcing release")
            return {"skipped": False, "error": "lock timeout"}
        try:
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
                await self._log_tick(
                    context, actions, results, duration, tick_start_iso
                )
                return {"skipped": False, "actions": len(results), "duration": duration}
            except Exception as e:
                duration = time.time() - start
                logger.error(f"Autonomy tick failed: {e}")
                await self.store.patch_state(
                    {
                        "last_tick": tick_start_iso,
                        "last_tick_duration": round(duration, 2),
                        "last_error": str(e)[:2000],
                    }
                )
                return {"skipped": False, "error": str(e), "duration": duration}
        finally:
            if acquired:
                self._lock.release()

    async def _resolve_reference(
        self, message: Any, cache: dict[tuple[str, str], Any]
    ) -> Any | None:
        ref_obj = getattr(message, "reference", None)
        if ref_obj is None:
            return None

        resolved = getattr(ref_obj, "resolved", None)
        if resolved is not None and hasattr(resolved, "author"):
            return resolved

        msg_id = getattr(ref_obj, "message_id", None)
        channel = cast(Any, getattr(message, "channel", None))
        channel_id = str(getattr(channel, "id", ""))
        if not msg_id or not channel_id or not hasattr(channel, "fetch_message"):
            return None

        key = (channel_id, str(msg_id))
        if key in cache:
            return cache[key]
        if len(cache) >= 25:
            # Reply lookups are nice, Discord rate limits are not. Twenty-five is
            # plenty for one autonomy tick unless the server is doing reply soup.
            return None

        try:
            resolved = await channel.fetch_message(int(msg_id))
        except (
            discord.NotFound,
            discord.Forbidden,
            discord.HTTPException,
            ValueError,
            TypeError,
        ):
            return None
        except Exception:
            return None

        if resolved is not None and hasattr(resolved, "author"):
            cache[key] = resolved
            with contextlib.suppress(Exception):
                ref_obj.resolved = resolved
            return resolved
        return None

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
            f"=== CURRENT TIME ===\n{now.strftime('%A, %Y-%m-%d %H:%M')} ({tz_name})"
        )

        # Normal on_message replies and autonomy ticks run independently. Tell the
        # planner about live/recent normal replies so it does not treat the same
        # conversation as unattended and send a second autonomous message.
        try:
            reply_lines = []
            active_replying = sorted(
                str(cid) for cid in (getattr(self.bot, "_replying_channels", None) or set())
            )
            for cid in active_replying[:12]:
                reply_lines.append(
                    f"currently replying normally in {_conversation_label(self.bot, cid)}"
                )
            last_replies = getattr(self.bot, "_last_bot_reply", None) or {}
            if last_replies:
                now_ts = time.time()
                for cid, ts in sorted(
                    last_replies.items(), key=lambda item: item[1], reverse=True
                )[:12]:
                    age = int(max(0, now_ts - float(ts or 0)))
                    if age <= 3600:
                        reply_lines.append(
                            f"normal reply already sent {age}s ago in {_conversation_label(self.bot, str(cid))}"
                        )
            if reply_lines:
                sections.append(
                    "=== NORMAL REPLY STATUS ===\n"
                    + "\n".join(reply_lines)
                    + "\nInterpret this as Maxwell already handling or having just handled those conversations. Usually choose do_nothing there unless a clearly new human message arrives after that reply."
                )
        except Exception as e:
            sections.append(f"=== NORMAL REPLY STATUS ===\n(error: {e})")

        # 2. Active goals (most decision-relevant — what should I work on?)
        try:
            goals = await self.store.load_goals()
            active_goals = [g for g in goals if g.get("active")]
            if active_goals:
                goal_lines = [
                    f"- [{g['id']}] {g.get('description', '')} (last acted: {g.get('last_acted_on', 'never')})"
                    for g in active_goals
                ]
                sections.append(
                    _truncate(
                        "=== ACTIVE GOALS ===\n" + "\n".join(goal_lines),
                        CTX_BUDGET_GOALS,
                    )
                )
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
                    content = str(ev.get("content", "")).replace("\n", " ")[:260]
                    ts = ev.get("ts", "")
                    when = "?"
                    if ts:
                        with contextlib.suppress(Exception):
                            ev_dt = _coerce_utc_datetime(ts)
                            when = _context_time(ev_dt) if ev_dt else "?"
                    cid = str(ev.get("channel_id") or "?")
                    ch_name = cid
                    with contextlib.suppress(Exception):
                        ch_obj = self.bot.get_channel(int(cid)) if cid != "?" else None
                        ch_name = (
                            getattr(ch_obj, "name", cid) if ch_obj is not None else cid
                        )
                    uid = str(ev.get("user_id") or "?")
                    uname = str(ev.get("user_name") or "?")
                    role = str(ev.get("role") or "?")
                    speaker_kind = (
                        "you/Maxwell"
                        if self.bot.user and uid == str(self.bot.user.id)
                        else role
                    )

                    tags = []
                    if ev.get("message_id"):
                        tags.append(f"msg={ev.get('message_id')}")

                    addressed = []
                    if ev.get("reply_to_author_id"):
                        reply_name = str(ev.get("reply_to_author") or "unknown")
                        reply_id = str(ev.get("reply_to_author_id") or "")
                        reply_ref = (
                            f"you/Maxwell({reply_id})"
                            if ev.get("reply_to_self")
                            else f"{reply_name}({reply_id})"
                        )
                        tags.append(f"reply_to={reply_ref}")
                        addressed.append(f"reply_to:{reply_ref}")
                    mentions = []
                    for row in list(ev.get("mentions") or [])[:10]:
                        if not isinstance(row, dict):
                            continue
                        mid = str(row.get("id") or "")
                        if not mid:
                            continue
                        mname = str(row.get("name") or mid)
                        mref = (
                            f"you/Maxwell({mid})"
                            if self.bot.user and mid == str(self.bot.user.id)
                            else f"{mname}({mid})"
                        )
                        mentions.append(mref)
                    if mentions:
                        tags.append("mentions=[" + ", ".join(mentions) + "]")
                        addressed.extend(f"mention:{ref}" for ref in mentions)
                        if self.bot.user and any(
                            ref.endswith(f"({self.bot.user.id})") for ref in mentions
                        ):
                            tags.append("mentions_you")
                    tags.append(
                        "addressed_to=[" + "; ".join(addressed) + "]"
                        if addressed
                        else "addressed_to=channel"
                    )
                    tag_text = " ".join(tags)
                    ev_lines.append(
                        f'time={when} channel=#{ch_name}({cid}) speaker={uname}({uid}, {speaker_kind}) {tag_text} content="{content}"'
                    )
                sections.append(
                    _truncate(
                        "=== RECENT CONVERSATIONS (since last check) ===\n"
                        + "\n".join(ev_lines),
                        CTX_BUDGET_RECENT_EVENTS,
                    )
                )
            else:
                sections.append(
                    "=== RECENT CONVERSATIONS ===\n(no new activity since last check)"
                )
        except Exception as e:
            sections.append(f"=== RECENT CONVERSATIONS ===\n(error: {e})")

        # 4. Channel activity (what's happening right now?)
        channel_ids_to_check = []
        seen_channel_ids = set()

        def add_channel_id(raw_cid):
            cid = re.sub(r"[^0-9]", "", str(raw_cid or ""))
            if cid and cid not in seen_channel_ids:
                seen_channel_ids.add(cid)
                channel_ids_to_check.append(cid)

        # New event channels first. If somebody pinged/replied, this is the room
        # where context matters. Sets made this random before; random context is
        # how you get bot improv jazz.
        with contextlib.suppress(Exception):
            for ev in reversed(events or []):
                add_channel_id(ev.get("channel_id"))
        with contextlib.suppress(Exception):
            for cid in self._auto_channel_candidates():
                add_channel_id(cid)

        ch_lines = []
        ref_cache: dict[tuple[str, str], Any] = {}
        for cid in channel_ids_to_check[:10]:
            if not self._channel_allowed(cid):
                continue
            try:
                ch = cast(Any, self.bot.get_channel(int(cid)))
                if ch is None:
                    try:
                        ch = cast(Any, await self.bot.fetch_channel(int(cid)))
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        continue
                if ch is None or not hasattr(ch, "history"):
                    continue
                messages = [m async for m in ch.history(limit=12)]
                for m in reversed(messages):
                    content = _visible_message_content(m, m.content or "")[:260]
                    if not content:
                        continue
                    age = _context_time(getattr(m, "created_at", None))
                    reply = await self._resolve_reference(m, ref_cache)
                    tags = _message_relation_tags(
                        m, bot_user=self.bot.user, reply=reply
                    )
                    tag_text = " ".join(tags)
                    msg_id = str(getattr(m, "id", "?"))
                    author = _user_ref(getattr(m, "author", None), self.bot.user)
                    ch_lines.append(
                        f'time={age} channel=#{getattr(ch, "name", cid)}({cid}) msg={msg_id} speaker={author} {tag_text} content="{content}"'
                    )
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                continue
            except Exception:
                continue
        if ch_lines:
            sections.append(
                _truncate(
                    "=== CHANNEL ACTIVITY ===\n" + "\n".join(ch_lines[-40:]),
                    CTX_BUDGET_CHANNEL_ACTIVITY,
                )
            )
        else:
            sections.append("=== CHANNEL ACTIVITY ===\n(no accessible channels)")

        # Auto-invoke the youtube tool for YouTube links seen in recent
        # channel activity, so the planner has transcript/frames context —
        # same capability as the normal reply path. Mirrors bot.py's
        # pre_tool_results injection.
        yt_context = await self._gather_youtube_context(ch_lines)
        if yt_context:
            sections.append(
                _truncate(
                    "=== YOUTUBE CONTEXT (auto-fetched for links above) ===\n"
                    + yt_context,
                    CTX_BUDGET_CHANNEL_ACTIVITY,
                )
            )

        # 5. The same short-term channel memory normal Maxwell sees.
        # This is the glue that stops autonomy from acting like some weird second
        # intern who skimmed the logs but missed the actual relationship history.
        try:
            memory = cast(Any, getattr(self.bot, "memory", None))
            mem_lines = []
            memory_now = datetime.now(timezone.utc)
            memory_budget = max(
                1000,
                min(
                    int(
                        (getattr(self.bot, "_control", None) or {}).get(
                            "memory_context_budget", CTX_BUDGET_CHANNEL_MEMORY
                        )
                        or CTX_BUDGET_CHANNEL_MEMORY
                    ),
                    20000,
                ),
            )
            if memory and hasattr(memory, "get_channel_memory"):
                for cid in reversed(channel_ids_to_check[:8]):
                    if not self._channel_allowed(cid):
                        continue
                    rows = await memory.get_channel_memory(cid)
                    if not rows:
                        continue
                    ch_name = cid
                    with contextlib.suppress(Exception):
                        ch_obj = self.bot.get_channel(int(cid))
                        ch_name = getattr(ch_obj, "name", cid) if ch_obj else cid
                    history_count = max(
                        1,
                        min(
                            int(
                                (getattr(self.bot, "_control", None) or {}).get(
                                    "memory_history_messages", 40
                                )
                                or 40
                            ),
                            100,
                        ),
                    )
                    tool_limit = max(
                        0,
                        min(
                            int(
                                (getattr(self.bot, "_control", None) or {}).get(
                                    "tool_history_messages", 3
                                )
                                or 0
                            ),
                            20,
                        ),
                    )
                    recent_rows = rows[-history_count:]
                    recent_ids = {id(row) for row in recent_rows}
                    tool_rows = (
                        [row for row in rows if isinstance(row, dict) and row.get("is_tool") and id(row) not in recent_ids][
                            -tool_limit:
                        ]
                        if tool_limit
                        else []
                    )
                    channel_rows = tool_rows + list(recent_rows)
                    channel_lines = []
                    used = 0
                    for msg in reversed(channel_rows):
                        if not isinstance(msg, dict):
                            continue
                        line = _format_memory_context_line(
                            msg, bot_user=self.bot.user, now=memory_now
                        )
                        if channel_lines and used + len(line) > memory_budget:
                            break
                        channel_lines.append(line)
                        used += len(line)
                    if channel_lines:
                        mem_lines.append(f"# {ch_name}({cid})")
                        mem_lines.extend(reversed(channel_lines))
            if mem_lines:
                sections.append(
                    _truncate_keep_tail(
                        "=== RECENT CONTEXT MEMORY (same continuity normal Maxwell sees; background only) ===\n"
                        + "\n".join(mem_lines[-80:]),
                        memory_budget,
                    )
                )
        except Exception as e:
            sections.append(f"=== RECENT CONTEXT MEMORY ===\n(error: {e})")

        # 6. Recent autonomy actions + validation failures (feedback loop)
        action_feedback = []
        try:
            log_entries = await self.store.load_log()
            recent = log_entries[-10:] if log_entries else []
            if recent:
                action_now = datetime.now(timezone.utc)
                action_lines = [
                    _action_feedback_line(e, now=action_now) for e in recent
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
            sections.append(
                _truncate(
                    "=== YOUR RECENT ACTIONS ===\n" + "\n\n".join(action_feedback),
                    CTX_BUDGET_RECENT_ACTIONS,
                )
            )

        # 6. Engagement tracking (did anyone react to or reply to your posts?)
        try:
            engagement = await self._check_post_engagement()
            if engagement:
                sections.append(f"=== ENGAGEMENT WITH YOUR POSTS ===\n{engagement}")
        except Exception as e:
            sections.append(f"=== ENGAGEMENT WITH YOUR POSTS ===\n(error: {e})")

        # 7. DM history
        dm_blocks = []
        for channel in list(getattr(self.bot, "private_channels", []) or [])[:20]:
            try:
                recipient = getattr(channel, "recipient", None)
                recipient_ref = (
                    _user_ref(recipient, self.bot.user)
                    if recipient
                    else f"channel({channel.id})"
                )
                lines = [f"DM with {recipient_ref} channel={channel.id}"]
                messages = [m async for m in channel.history(limit=20)]
                for m in reversed(messages):
                    content = _visible_message_content(m, m.content or "")[:260]
                    if not content:
                        continue
                    age = _context_time(getattr(m, "created_at", None))
                    author_is_self = bool(
                        self.bot.user
                        and getattr(m.author, "id", None) == self.bot.user.id
                    )
                    direction = (
                        f"from=you/Maxwell({getattr(self.bot.user, 'id', '?')}) to={recipient_ref}"
                        if author_is_self
                        else f"from={_user_ref(m.author, self.bot.user)} to=you/Maxwell({getattr(self.bot.user, 'id', '?')})"
                    )
                    lines.append(
                        f'time={age} msg={getattr(m, "id", "?")} {direction} content="{content}"'
                    )
                if len(lines) > 1:
                    dm_blocks.append("\n".join(lines))
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                continue
            except Exception:
                continue
        if dm_blocks:
            sections.append(
                _truncate(
                    "=== DM HISTORY ===\n" + "\n\n".join(dm_blocks[-20:]),
                    CTX_BUDGET_DM_HISTORY,
                )
            )
        else:
            sections.append("=== DM HISTORY ===\n(no accessible DMs)")

        # 8. Long-term memory
        try:
            memory = cast(Any, getattr(self.bot, "memory", None))
            ltm = memory.get_long_term_memory() if memory else []
            if ltm:
                ltm_text = "\n".join(str(m) for m in ltm[:30])
                sections.append(
                    _truncate(
                        f"=== LONG-TERM MEMORY ===\n{ltm_text}",
                        CTX_BUDGET_LTM,
                    )
                )
        except Exception as e:
            sections.append(f"=== LONG-TERM MEMORY ===\n(error: {e})")

        # 9. Available channels map — enriched with activity and topic
        # so the LLM can pick the right channel without cross-referencing CHANNEL ACTIVITY.
        now_ts = time.time()
        ch_map_lines = []
        for guild in self.bot.guilds:
            for ch in guild.text_channels:
                try:
                    if guild.me is None:
                        continue
                    perms = ch.permissions_for(guild.me)
                    if not perms.send_messages or not self._channel_allowed(str(ch.id)):
                        continue
                    cid = str(ch.id)
                    # build status tags
                    tags = []
                    if cid in (self.bot._auto_channels or set()):
                        tags.append("auto")
                    topic = getattr(ch, "topic", None) or ""
                    topic_snippet = topic[:80].replace("\n", " ") if topic else ""
                    # grab last message time for recency
                    last_msg_ago = ""
                    try:
                        last_msg = [m async for m in ch.history(limit=1)]
                        if last_msg:
                            age_s = int(now_ts - last_msg[0].created_at.timestamp())
                            if age_s < 60:
                                last_msg_ago = "just now"
                            elif age_s < 3600:
                                last_msg_ago = f"{age_s // 60}m ago"
                            elif age_s < 86400:
                                last_msg_ago = f"{age_s // 3600}h ago"
                            else:
                                last_msg_ago = f"{age_s // 86400}d ago"
                    except Exception:
                        pass
                    tag_str = f" [{', '.join(tags)}]" if tags else ""
                    topic_str = f' — "{topic_snippet}"' if topic_snippet else ""
                    recency_str = f" (last msg: {last_msg_ago})" if last_msg_ago else ""
                    ch_map_lines.append(
                        f"  {cid}: #{ch.name}{tag_str}{recency_str}{topic_str}"
                    )
                except Exception:
                    continue
        if ch_map_lines:
            sections.append(
                _truncate(
                    "=== AVAILABLE CHANNELS (use the numeric ID on the left for post_channel target_channel_id) ===\n"
                    + "\n".join(ch_map_lines[:30]),
                    CTX_BUDGET_CHANNELS_MAP,
                )
            )

        # 10. Shared context
        try:
            memory = cast(Any, getattr(self.bot, "memory", None))
            shared = (
                await memory.get_relevant_shared_context(
                    user_id="",
                    guild_id="",
                    channel_id="",
                    is_dm=False,
                    is_admin=False,
                    max_items=20,
                    budget=CTX_BUDGET_SHARED,
                )
                if memory and hasattr(memory, "get_relevant_shared_context")
                else []
            )
            if shared:
                ctx_lines = [
                    f"- [{c.get('scope', '?')}, i{c.get('importance', '?')}] {c.get('content', '')}"
                    for c in shared[:20]
                ]
                sections.append(
                    _truncate(
                        "=== SHARED CONTEXT ===\n" + "\n".join(ctx_lines),
                        CTX_BUDGET_SHARED,
                    )
                )
        except Exception:
            pass

        full = "\n\n".join(sections)
        return full

    async def _gather_youtube_context(self, ch_lines: list[str]) -> str:
        """Auto-invoke the youtube tool for YouTube links in recent channel
        activity, mirroring the normal reply path. Returns transcript/frame
        text the planner can use directly."""
        control = getattr(self.bot, "_control", None) or {}
        if not control.get("tools_enabled", True):
            return ""
        if "youtube" in set(control.get("disabled_tools", []) or []):
            return ""
        yt_tool = self.bot.tools.get("youtube")
        if yt_tool is None:
            return ""
        yt_re = re.compile(
            r"https?://(?:www\.)?(?:youtube\.com|youtu\.be|youtube-nocookie\.com)/[^\s<>\"']+",
            re.IGNORECASE,
        )
        urls: list[str] = []
        for line in ch_lines:
            for m in yt_re.finditer(line):
                url = m.group(0).rstrip(".,)]")
                if url not in urls:
                    urls.append(url)
        if not urls:
            return ""
        blocks: list[str] = []
        for url in urls[:3]:
            try:
                # SyntheticMessage lets the youtube tool resolve a channel if
                # it needs one (it generally doesn't for transcript fetch).
                syn = SyntheticMessage(
                    channel=None,
                    author=SimpleNamespace(
                        id="autonomy",
                        display_name=getattr(self.bot.user, "display_name", "Maxwell"),
                        name=getattr(self.bot.user, "name", "Maxwell"),
                        bot=True,
                    ),
                    guild=None,
                    content=url,
                )
                result = await yt_tool.execute(syn, url=url)
                if result:
                    # Strip frame image blobs — autonomy is text-only planning.
                    result = re.sub(
                        r"__IMAGE_B64__.*?__END_IMAGE_B64__",
                        "[frame available]",
                        result,
                        flags=re.DOTALL,
                    )
                    blocks.append(f"URL {url}:\n{result[:1500]}")
            except Exception as e:
                logger.warning(f"Autonomy youtube auto-invoke failed for {url}: {e}")
        return "\n\n".join(blocks)

    async def _check_post_engagement(self) -> str:
        """Check if recent autonomous posts got reactions or replies."""
        if not self._posted_messages:
            return ""

        # Only check posts from the last 2 hours
        cutoff = time.time() - 7200
        self._posted_messages = [
            p for p in self._posted_messages if p.get("ts", 0) > cutoff
        ]

        engagement_lines = []
        for post in self._posted_messages[-5:]:  # check last 5 posts
            try:
                channel = cast(Any, self.bot.get_channel(int(post["channel_id"])))
                if channel is None or not hasattr(channel, "fetch_message"):
                    continue
                msg = await channel.fetch_message(post["msg_id"])
                if msg is None:
                    continue

                reactions = []
                for r in msg.reactions:
                    reactions.append(f"{r.emoji} ({r.count})")

                # Check for replies (messages that reference this post)
                reply_snippets = []
                with contextlib.suppress(Exception):
                    if hasattr(channel, "history"):
                        async for reply in channel.history(
                            limit=10, after=msg.created_at
                        ):
                            if reply.reference and reply.reference.message_id == msg.id:
                                author = getattr(
                                    reply.author, "display_name", None
                                ) or getattr(reply.author, "name", "?")
                                content = _render_discord_context_text(
                                    reply, reply.content or ""
                                )[:160]
                                reply_snippets.append(
                                    f"{author}({reply.author.id}): {content or '[media/reaction-only]'}"
                                )

                parts = []
                if reactions:
                    parts.append(f"reactions: {', '.join(reactions)}")
                if reply_snippets:
                    shown = "; ".join(reply_snippets[:2])
                    more = (
                        f" (+{len(reply_snippets) - 2} more)"
                        if len(reply_snippets) > 2
                        else ""
                    )
                    parts.append(f"replies: {shown}{more}")
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

    async def _find_channel_for_message_id(self, message_id: str) -> str | None:
        """Locate the channel id that holds a given message_id by scanning
        short-term channel memory. This is the fallback that makes react /
        edit / delete / forward work when the LLM only passed target_message_id
        (e.g. a forum post's starter message) without the matching thread
        channel id — and when the resolved channel fetch missed."""
        message_id = str(message_id or "").strip()
        if not message_id:
            return None
        memory = cast(Any, getattr(self.bot, "memory", None))
        if memory is None or not hasattr(memory, "memory"):
            return None
        try:
            store = getattr(memory, "memory", {}) or {}
        except Exception:
            return None
        for cid, msgs in store.items():
            if not isinstance(msgs, list):
                continue
            for row in msgs:
                if isinstance(row, dict) and str(row.get("message_id") or "") == message_id:
                    return str(cid)
        return None

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
        tool_descriptions = (
            "\n".join(tool_desc_lines) if tool_desc_lines else "(no tools available)"
        )

        # goals text
        try:
            goals = await self.store.load_goals()
            active_goals = [g for g in goals if g.get("active")]
            goals_text = (
                "\n".join(
                    f"- [{g['id']}] (last acted: {g.get('last_acted_on') or 'never'}) {g.get('description', '')}"
                    for g in active_goals
                )
                if active_goals
                else "(no active goals)"
            )
        except Exception:
            goals_text = "(error loading goals)"

        # Pull the real personality so autonomy posts sound like the same bot
        base_personality = str(
            (getattr(self.bot, "_control", None) or {}).get(
                "base_personality", DEFAULT_CONTROL.get("base_personality", "")
            )
        )
        # Inject age dynamically — use bot's _get_personality if available
        if hasattr(self.bot, "_get_personality"):
            base_personality = self.bot._get_personality()

        system_prompt = f"""You are Maxwell, doing a quick background check-in on your Discord server. Silence is normal — you are NOT obligated to speak every tick.

PERSONALITY (when you DO post, match this voice):
{base_personality}

CURRENT CONTEXT:
{context}

TOOLS:
{tool_descriptions}

GOALS (these are your ongoing objectives — pursue them proactively and flexibly, not just when someone pings you. When you act on a goal, update last_acted_on by re-creating the goal after acting):
{goals_text}

DECISION RULES:
- Default to do_nothing when there is genuinely nothing worth doing — but don't default to do_nothing just to be safe. If a goal or the live context gives you a real opening, act on it.
- Act when ANY of these is true: (1) someone mentioned/replied to/asked you (check mentions, reply_to, addressed_to), (2) a goal needs a concrete step and now is a reasonable moment, (3) you have a genuinely natural, in-character addition to a live conversation, (4) a goal you created earlier applies to the current situation even with no new human message aimed at you.
- Goals are yours to push on your own initiative — a goal about following up on someone's project means you may post_channel or send_dm when that person is around, without waiting to be asked.
- If NORMAL REPLY STATUS says Maxwell is currently replying normally in a conversation, treat that conversation as already being handled. Do not also post/DM into it from autonomy; choose do_nothing unless a separate, clearly new situation elsewhere needs action.
- If NORMAL REPLY STATUS says a normal reply was already sent recently, do not send an autonomous follow-up into the SAME conversation just because it appears in DM HISTORY or CHANNEL ACTIVITY. Only act if a newer human message after that reply creates a fresh reason, OR a goal applies to a different conversation/person.
- Don't: restate visible context, reopen concluded conversations, DM without a concrete reason, or say "just checking in". Talk like a person in the channel — never reference being a "background loop" or "check-in".
- Don't pile on in the SAME channel you just replied in. But acting in a DIFFERENT channel, or on a DIFFERENT goal/person, in the same tick is fine and encouraged when the situation warrants it.
- A reaction (react tool) is a cheap, low-noise way to engage with a message that doesn't need a full reply — use it freely when it fits the vibe.
- Voice: short, casual, lowercase-natural — exactly like Maxwell in normal chat.

DATA RULES:
- Channel activity / recent conversations are REAL, structured lines: channel=#name(ID), msg=ID, speaker=Name(user_id), reply_to=, mentions=[], addressed_to=, content="...". Don't fetch more.
- Discord is multi-user: each user_id is a distinct person; never cross-attribute.
- target_channel_id must be the numeric ID from channel= or AVAILABLE CHANNELS, never a name.
- To thread a reply, include reply_to_message_id with the target msg ID.
- For react/edit/delete/forward, pass target_message_id + target_channel_id from CHANNEL ACTIVITY. In forum channels, each post is its own thread — the starter message id and the thread channel id are the SAME snowflake, so use that id as BOTH target_message_id and target_channel_id. If you only have target_message_id, autonomy will search memory to find its channel, but passing target_channel_id is more reliable.
- Don't repeat recent posts (check YOUR RECENT ACTIONS; timestamps are recalculated this tick).
- Prefer 0-1 actions; up to {MAX_ACTIONS_PER_TICK} only with clear reason for each.

Return ONLY valid JSON:
{{
  "thought": "your read on the situation",
  "actions": [
    {{"kind": "post_channel", "target_channel_id": "ID", "reply_to_message_id": "ID", "content": "...", "reason": "..."}},
    {{"kind": "send_dm", "target_user_id": "ID", "content": "...", "reason": "..."}},
    {{"kind": "run_tool", "tool_name": "react", "tool_args": {{"emoji": "...", "target_message_id": "ID"}}, "target_channel_id": "ID", "reason": "..."}},
    {{"kind": "update_memory", "content": "...", "reason": "..."}},
    {{"kind": "create_goal", "description": "...", "reason": "..."}},
    {{"kind": "do_nothing", "reason": "..."}}
  ]
}}

Valid kinds: send_dm, post_channel, run_tool, update_memory, create_goal, do_nothing. Do NOT invent others."""

        # call the LLM
        try:
            messages = [{"role": "system", "content": system_prompt}]
            # Cap the timeout like the REM path (bot.py _run_rem_once_guarded)
            # so a misconfigured ai_timeout_seconds can't hang a tick for hours.
            timeout = max(
                30,
                min(
                    int(
                        (getattr(self.bot, "_control", None) or {}).get(
                            "ai_timeout_seconds", 180
                        )
                        or 180
                    ),
                    600,
                ),
            )
            # Provider-unavailable soft skip: if the autonomy provider isn't
            # ready (init failed / endpoint down), don't burn an AI slot or count
            # this as a tick failure — just do_nothing. _get_autonomy_provider
            # awaits init, so a transient failure self-heals on the next tick.
            ai_provider = cast(Any, getattr(self.bot, "_get_autonomy_provider", None))
            if callable(ai_provider):
                ai_provider = await ai_provider()
            else:
                ai_provider = cast(Any, getattr(self.bot, "ai_provider", None))
            if not callable(getattr(ai_provider, "generate_response", None)):
                ai_provider = cast(Any, getattr(self.bot, "ai_provider", None))
            if ai_provider is not None and getattr(ai_provider, "available", None) is False:
                logger.info("Autonomy planner: provider not available, soft skip")
                return [{"kind": "do_nothing", "reason": "provider not available"}]
            await self.bot._acquire_ai_slot(timeout=timeout)
            try:
                # Pass the configured autonomy model as override so even the main
                # provider runs a different model if autonomy_model is set.
                control = getattr(self.bot, "_control", None) or {}
                autonomy_model = str(control.get("autonomy_model", "") or "")
                # Honor autonomy_disable_reasoning per-call so it takes effect even
                # when reusing the main provider (no autonomy_base_url). The
                # provider lets a per-call False override the endpoint default.
                autonomy_disable_reasoning = bool(
                    control.get("autonomy_disable_reasoning", True)
                )
                raw_response = await ai_provider.generate_response(
                    messages,
                    timeout=timeout,
                    model=autonomy_model or None,
                    # Autonomy only generates a short JSON plan; cap max_tokens so
                    # we don't blow past an autonomy model's output limit (e.g.
                    # minimax-m3 caps at 131072) and waste quota/tokens.
                    max_tokens=8192,
                    disable_reasoning=autonomy_disable_reasoning,
                )
            finally:
                await self.bot._release_ai_slot()
        except Exception as e:
            # Re-raise so tick() reports an error and _loop engages exponential
            # backoff. The provider already retried internally (retry_attempts);
            # if it still fails, hammering every interval with backoff=1 is worse
            # than backing off. The provider-unavailable soft skip above returns
            # normally and does NOT reach here.
            logger.error(f"Autonomy LLM call failed: {e}")
            raise

        # parse JSON from response
        logger.info(
            f"Autonomy LLM response ({len(raw_response or '')} chars): {(raw_response or '')[:500]}"
        )
        actions, validation_failures = self._parse_plan(raw_response)

        # Store validation failures for next tick's feedback
        self._last_validation_failures = validation_failures

        return actions

    def _parse_plan(self, raw: str) -> tuple[list[dict], list[str]]:
        """Extract and validate the JSON plan from LLM output.
        Returns (valid_actions, validation_failures)."""
        validation_failures = []

        if not raw:
            return [
                {"kind": "do_nothing", "reason": "empty LLM response"}
            ], validation_failures

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
                                candidates.append(text[i : j + 1])
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
            return [
                {"kind": "do_nothing", "reason": "no JSON in LLM response"}
            ], validation_failures

        try:
            parsed = json.loads(json_str)
        except json.JSONDecodeError as e:
            logger.warning(
                f"Autonomy planner JSON parse failed: {e}. Raw: {json_str[:500]}"
            )
            return [
                {"kind": "do_nothing", "reason": "invalid JSON from planner"}
            ], validation_failures

        if not isinstance(parsed, dict):
            return [
                {"kind": "do_nothing", "reason": "planner returned non-object"}
            ], validation_failures

        # save thought
        thought = str(parsed.get("thought", ""))[:2000]
        self._last_thought = thought

        raw_actions = parsed.get("actions", [])
        if not isinstance(raw_actions, list):
            return [
                {"kind": "do_nothing", "reason": "actions not a list"}
            ], validation_failures

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
                logger.info(
                    f"Dropping {msg} | raw: {json.dumps(action, default=str)[:300]}"
                )
                validation_failures.append(msg)
                continue

            if kind == "send_dm":
                uid_raw = str(action.get("target_user_id", ""))
                uid = re.sub(r"[^0-9]", "", uid_raw)
                content = str(action.get("content", "")).strip()
                if not uid or not content:
                    validation_failures.append("send_dm: missing user_id or content")
                    continue
                valid.append(
                    {
                        "kind": "send_dm",
                        "target_user_id": uid,
                        "content": content[:MAX_CONTENT_CHARS],
                        "reason": str(action.get("reason", ""))[:500],
                    }
                )

            elif kind == "post_channel":
                cid_raw = str(action.get("target_channel_id", ""))
                cid = re.sub(r"[^0-9]", "", cid_raw)
                content = str(action.get("content", "")).strip()
                if not content:
                    validation_failures.append("post_channel: empty content")
                    continue
                if not cid:
                    validation_failures.append(
                        "post_channel: missing explicit numeric target_channel_id"
                    )
                    continue
                reply_to_raw = str(action.get("reply_to_message_id", ""))
                reply_to = re.sub(r"[^0-9]", "", reply_to_raw)
                parsed_action = {
                    "kind": "post_channel",
                    "target_channel_id": cid,
                    "content": content[:MAX_CONTENT_CHARS],
                    "reason": str(action.get("reason", ""))[:500],
                }
                if reply_to:
                    parsed_action["reply_to_message_id"] = reply_to
                valid.append(parsed_action)

            elif kind == "run_tool":
                tool_name = str(action.get("tool_name", "")).strip()
                if not tool_name:
                    validation_failures.append("run_tool: missing tool_name")
                    continue
                if not self._autonomy_tool_allowed(tool_name):
                    validation_failures.append(
                        f"run_tool: '{tool_name}' is disabled or not allowed"
                    )
                    continue
                if tool_name not in self.bot.tools:
                    validation_failures.append(
                        f"run_tool: tool '{tool_name}' not found"
                    )
                    continue
                tool_args = action.get("tool_args", {})
                if not isinstance(tool_args, dict):
                    tool_args = {}
                safe_args = {str(k): v for k, v in tool_args.items()}
                parsed_action = {
                    "kind": "run_tool",
                    "tool_name": tool_name,
                    "tool_args": safe_args,
                    "reason": str(action.get("reason", ""))[:500],
                }
                target_cid_raw = str(action.get("target_channel_id", ""))
                target_cid = re.sub(r"[^0-9]", "", target_cid_raw)
                if target_cid_raw.strip() and not target_cid:
                    validation_failures.append(
                        "run_tool: target_channel_id must be numeric"
                    )
                    continue
                if target_cid:
                    parsed_action["target_channel_id"] = target_cid
                valid.append(parsed_action)

            elif kind == "update_memory":
                content = str(action.get("content", "")).strip()
                if not content:
                    validation_failures.append("update_memory: empty content")
                    continue
                valid.append(
                    {
                        "kind": "update_memory",
                        "content": content[:MAX_CONTENT_CHARS],
                        "reason": str(action.get("reason", ""))[:500],
                    }
                )

            elif kind == "create_goal":
                desc = str(action.get("description", "")).strip()
                if not desc:
                    validation_failures.append("create_goal: empty description")
                    continue
                valid.append(
                    {
                        "kind": "create_goal",
                        "description": desc[:500],
                        "reason": str(action.get("reason", ""))[:500],
                    }
                )

            elif kind == "do_nothing":
                valid.append(
                    {
                        "kind": "do_nothing",
                        "reason": str(action.get("reason", "no reason"))[:500],
                    }
                )

        if not valid:
            logger.warning(
                f"All {len(raw_actions)} actions failed validation. Raw response: {raw[:1000]}"
            )
            valid = [{"kind": "do_nothing", "reason": "all actions failed validation"}]

        if not any(a["kind"] != "do_nothing" for a in valid):
            logger.info(
                f"Autonomy planner produced no actionable items. Thought: {thought[:300]}"
            )
        return valid, validation_failures

    # -----------------------------------------------------------------------
    # execute
    # -----------------------------------------------------------------------

    async def execute(self, actions: list[dict]) -> list[dict]:
        """Execute each action. One failure doesn't kill the rest."""
        results = []
        ACTION_TIMEOUT = 30  # seconds per action

        # Prevent multiple posts to the *same* channel within a single autonomy tick/plan.
        # This was a bypass of cooldowns noted in reviews: validation happened before any
        # side effects, so the LLM could return several post_channel for one cid and all would run.
        planned_post_channels: set[str] = set()

        for action in actions:
            # bail if bot disconnected mid-tick
            if self.bot.is_closed():
                logger.warning(
                    "Bot disconnected during autonomy tick, aborting remaining actions"
                )
                break

            kind = action.get("kind", "do_nothing")
            result = {"kind": kind, "result": "success", "error": None}

            # Determine the target channel for any post-style action so we can
            # gate it against live main-bot activity. Applies to post_channel
            # and message-sending run_tool (AUTONOMY_POST_TOOLS).
            post_cid = None
            if kind == "post_channel":
                post_cid = str(action.get("target_channel_id") or "") or None
            elif kind == "run_tool" and str(action.get("tool_name", "")) in AUTONOMY_POST_TOOLS:
                ta = action.get("tool_args") or {}
                post_cid = (
                    str(action.get("target_channel_id")
                        or ta.get("target_channel_id")
                        or ta.get("channel_id")
                        or "")
                    or None
                )
            if post_cid:
                # HARD GATE: never post into a channel the main bot is currently
                # mid-reply in. _replying_channels is held for the whole
                # _handle_message lifetime (generation + tool-call loop), so
                # without this autonomy posts over a reply that's still being
                # built and the bot visibly talks over itself. Not configurable —
                # this is a correctness fix, not a taste preference.
                if post_cid in (getattr(self.bot, "_replying_channels", None) or set()):
                    logger.info(
                        f"Autonomy skip post to {post_cid}: main bot currently replying there"
                    )
                    result = {
                        "kind": kind,
                        "result": "skipped",
                        "error": None,
                        "content_summary": "main bot currently replying in channel",
                    }
                    results.append(result)
                    continue

                # Same-tick dedup: don't allow the plan to post twice to one channel in one go.
                if post_cid in planned_post_channels:
                    logger.info(f"Autonomy skip duplicate post to {post_cid} in same tick/plan")
                    result = {
                        "kind": kind,
                        "result": "skipped",
                        "error": None,
                        "content_summary": "duplicate post_channel for same channel in this tick",
                    }
                    results.append(result)
                    continue
                planned_post_channels.add(post_cid)
                # Soft guard: skip autonomy post if the bot replied in-channel
                # within the configured window (0 = off, never block). Catches
                # the case where the main reply already finished but is so
                # recent that piling on would still look like spam.
                last_reply = getattr(self.bot, "_last_bot_reply", {}).get(post_cid, 0.0)
                block_window = int(
                    (getattr(self.bot, "_control", None) or {}).get(
                        "autonomy_recent_reply_block_seconds", 0
                    )
                    or 0
                )
                if block_window > 0 and last_reply and time.time() - last_reply < block_window:
                    logger.info(
                        f"Autonomy skip post to {post_cid}: bot replied there "
                        f"{int(time.time() - last_reply)}s ago (block_window={block_window}s)"
                    )
                    result = {
                        "kind": kind,
                        "result": "skipped",
                        "error": None,
                        "content_summary": f"bot recently replied in channel ({block_window}s window)",
                    }
                    results.append(result)
                    continue

            try:
                if kind == "send_dm":
                    await asyncio.wait_for(
                        self._exec_send_dm(action, result), timeout=ACTION_TIMEOUT
                    )
                elif kind == "post_channel":
                    await asyncio.wait_for(
                        self._exec_post_channel(action, result), timeout=ACTION_TIMEOUT
                    )
                elif kind == "run_tool":
                    await asyncio.wait_for(
                        self._exec_run_tool(action, result), timeout=ACTION_TIMEOUT
                    )
                elif kind == "update_memory":
                    await asyncio.wait_for(
                        self._exec_update_memory(action, result), timeout=ACTION_TIMEOUT
                    )
                elif kind == "create_goal":
                    await asyncio.wait_for(
                        self._exec_create_goal(action, result), timeout=ACTION_TIMEOUT
                    )
                elif kind == "do_nothing":
                    result["result"] = "skipped"
                    result["content_summary"] = action.get("reason", "no reason")
                else:
                    result["result"] = "skipped"
                    result["error"] = f"unknown kind: {kind}"
            except asyncio.TimeoutError:
                result["result"] = "error"
                result["error"] = f"action timed out after {ACTION_TIMEOUT}s"
                logger.warning(
                    f"Autonomy action {kind} timed out after {ACTION_TIMEOUT}s"
                )
            except Exception as e:
                result["result"] = "error"
                result["error"] = str(e)[:1000]
                logger.error(f"Autonomy action {kind} failed: {e}")
            results.append(result)

            # record in REM event log (skip do_nothing)
            if kind != "do_nothing":
                try:
                    summary = result.get("content_summary", action.get("reason", kind))
                    rem_log = cast(Any, getattr(self.bot, "rem_log", None))
                    if rem_log is None:
                        continue
                    channel_id = str(result.get("channel_id") or "")
                    guild_id = result.get("guild_id")
                    await rem_log.record(
                        {
                            "ts": _utcnow_iso(),
                            "channel_id": channel_id,
                            "guild_id": str(guild_id) if guild_id else None,
                            "user_id": str(self.bot.user.id) if self.bot.user else "",
                            "user_name": self.bot.bot_name,
                            "role": "assistant",
                            "content": f"[autonomy] {kind}: {str(summary)[:300]}",
                            "auto_mode": bool(
                                channel_id
                                and channel_id
                                in (getattr(self.bot, "_auto_channels", None) or set())
                            ),
                        }
                    )
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
                result["error"] = (
                    f"failed to create DM channel (user may have DMs disabled): {e}"
                )
                return

        try:
            msg = await dm_channel.send(content)
            result["tool_called"] = "send_dm"
            result["channel_id"] = str(getattr(dm_channel, "id", ""))
            # Track for engagement checking
            if msg:
                self._posted_messages.append(
                    {
                        "msg_id": msg.id,
                        "channel_id": str(dm_channel.id),
                        "ts": time.time(),
                    }
                )
                await self._remember_visible_self_message(
                    dm_channel, msg, content, reason=action.get("reason", "")
                )
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
        content = action["content"][:MAX_CONTENT_CHARS]
        reply_to_message_id = action.get("reply_to_message_id")
        result["target"] = f"channel:{channel_id}"
        result["channel_id"] = channel_id
        result["content_summary"] = content[:200]
        if reply_to_message_id:
            result["reply_to_message_id"] = str(reply_to_message_id)

        if not self._channel_allowed(channel_id):
            result["result"] = "error"
            result["error"] = "channel not allowed for autonomy"
            return

        channel = cast(Any, self.bot.get_channel(int(channel_id)))
        if channel is None:
            try:
                channel = cast(Any, await self.bot.fetch_channel(int(channel_id)))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                channel = None
        if channel is None:
            result["result"] = "error"
            result["error"] = "channel not found"
            return

        result["guild_id"] = str(getattr(getattr(channel, "guild", None), "id", ""))

        try:
            if not hasattr(channel, "send"):
                result["result"] = "error"
                result["error"] = "channel cannot receive messages"
                return

            msg = None
            ref = None
            memory_reply = None
            if reply_to_message_id and hasattr(channel, "fetch_message"):
                try:
                    ref = await channel.fetch_message(int(reply_to_message_id))
                    if ref is not None and hasattr(ref, "reply"):
                        msg = await ref.reply(content, mention_author=True)
                        result["sent_as_reply"] = True
                        memory_reply = ref
                except (
                    discord.NotFound,
                    discord.Forbidden,
                    discord.HTTPException,
                    ValueError,
                    TypeError,
                ):
                    logger.warning(
                        f"Autonomy post_channel: couldn't reply to message {reply_to_message_id} in {channel_id}; falling back to channel.send"
                    )

            if msg is None:
                msg = await channel.send(content)
                result["sent_as_reply"] = False

            result["tool_called"] = "post_channel"
            # Track for engagement checking
            if msg:
                self._posted_messages.append(
                    {
                        "msg_id": msg.id,
                        "channel_id": channel_id,
                        "ts": time.time(),
                    }
                )
                await self._remember_visible_self_message(
                    channel, msg, content, reply=memory_reply, reason=action.get("reason", "")
                )
        except discord.Forbidden:
            result["result"] = "error"
            result["error"] = "bot lacks permission to send in this channel"
        except discord.HTTPException as e:
            result["result"] = "error"
            result["error"] = f"Discord API error: {e}"

    async def _remember_visible_self_message(
        self,
        channel: Any,
        sent_message: Any,
        content: str,
        *,
        reply: Any = None,
        reason: str = "",
    ):
        # Always record autonomy's own posts into channel memory (not gated on
        # store_memory) so the normal reply path keeps context after an
        # autonomous post. Dedup by message_id in memory.add_to_channel_memory
        # keeps the later on_message self-echo from duplicating the entry.
        memory = cast(Any, getattr(self.bot, "memory", None))
        if memory is None or not hasattr(memory, "add_to_channel_memory"):
            return
        bot_user = getattr(self.bot, "user", None)
        channel_id = str(getattr(channel, "id", ""))
        if not channel_id:
            return

        author_name = (
            getattr(bot_user, "display_name", None)
            or getattr(bot_user, "name", None)
            or getattr(self.bot, "bot_name", "Maxwell")
        )
        item = {
            "author": author_name,
            "author_id": str(getattr(bot_user, "id", "")),
            "author_is_bot": True,
            "content": _render_discord_context_text(sent_message, content),
            "message_id": str(getattr(sent_message, "id", "")),
            "timestamp": (
                getattr(sent_message, "created_at", None) or datetime.now(timezone.utc)
            ).isoformat(),
            "autonomy": True,
            "autonomy_reason": str(reason)[:500],
        }
        if reply is not None and hasattr(reply, "author"):
            item.update(
                {
                    "reply_to_message_id": str(getattr(reply, "id", "")),
                    "reply_to_author": getattr(
                        reply.author,
                        "display_name",
                        str(getattr(reply.author, "id", "unknown")),
                    ),
                    "reply_to_author_id": str(getattr(reply.author, "id", "")),
                    "reply_to_self": bool(
                        bot_user and getattr(reply.author, "id", None) == bot_user.id
                    ),
                }
            )

        try:
            await memory.add_to_channel_memory(channel_id, item)
        except Exception as e:
            logger.warning(f"Failed to record autonomy self-message memory: {e}")

    async def _exec_run_tool(self, action: dict, result: dict):
        tool_name = action["tool_name"]
        tool_args = action.get("tool_args", {})
        result["target"] = f"tool:{tool_name}"
        result["tool_called"] = tool_name
        result["tool_args"] = tool_args
        result["content_summary"] = (
            f"{tool_name}({json.dumps(tool_args, default=str)[:150]})"
        )

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
        explicit_target = bool(
            action.get("target_channel_id")
            or tool_args.get("target_channel_id")
            or tool_args.get("source_channel_id")
        )
        target_cid = (
            action.get("target_channel_id")
            or tool_args.get("target_channel_id")
            or tool_args.get("source_channel_id")
            or tool_args.get("channel_id")
        )
        if target_cid:
            # LLM sometimes passes channel names like "general" instead of IDs.
            # int() throws ValueError, we'd silently fall back to auto_channel,
            # and the message goes to the wrong place. Validate upfront.
            clean_cid = re.sub(r"[^0-9]", "", str(target_cid))
            if not clean_cid:
                logger.warning(
                    f"Autonomy run_tool '{tool_name}': target_channel_id '{target_cid}' "
                    f"has no digits — LLM probably passed a channel name. "
                    f"Available channels are listed by ID in context."
                )
                if explicit_target:
                    result["result"] = "error"
                    result["error"] = "invalid explicit target_channel_id"
                    return
            else:
                try:
                    channel = self.bot.get_channel(int(clean_cid))
                    if channel is None:
                        channel = await self.bot.fetch_channel(int(clean_cid))
                    if channel is not None and not self._channel_allowed(clean_cid):
                        result["result"] = "error"
                        result["error"] = "channel not allowed for autonomy"
                        return
                except (ValueError, TypeError):
                    logger.warning(
                        f"Autonomy run_tool '{tool_name}': bad channel_id '{target_cid}'"
                    )
                except discord.NotFound:
                    logger.warning(
                        f"Autonomy run_tool '{tool_name}': channel {clean_cid} not found (deleted?)"
                    )
                    if explicit_target:
                        result["result"] = "error"
                        result["error"] = "explicit target channel not found"
                        return
                except (discord.Forbidden, discord.HTTPException) as e:
                    logger.warning(
                        f"Autonomy run_tool '{tool_name}': can't access channel {clean_cid}: {e}"
                    )
                    if explicit_target:
                        result["result"] = "error"
                        result["error"] = "explicit target channel unavailable"
                        return

        if explicit_target and channel is None:
            result["result"] = "error"
            result["error"] = "explicit target channel unavailable"
            return

        # if no channel and we can find a default, use the first auto_channel
        # NOTE: this fallback means messages can end up in a channel the LLM didn't
        # intend. We log it so it's at least diagnosable.
        if channel is None:
            for cid in self._auto_channel_candidates():
                try:
                    ch = self.bot.get_channel(int(cid))
                    if ch is None:
                        ch = await self.bot.fetch_channel(int(cid))
                except (
                    ValueError,
                    TypeError,
                    discord.NotFound,
                    discord.Forbidden,
                    discord.HTTPException,
                ):
                    continue
                if ch:
                    if not self._channel_allowed(cid):
                        logger.debug(
                            f"Autonomy run_tool: auto_channel {cid} not allowed, skipping"
                        )
                        continue
                    channel = ch
                    if target_cid:
                        logger.warning(
                            f"Autonomy run_tool '{tool_name}': requested channel '{target_cid}' "
                            f"not found, falling back to auto_channel {cid}"
                        )
                    break

        if channel is None:
            result["result"] = "error"
            result["error"] = "no channel available for tool execution"
            return

        target_message = None
        target_mid = tool_args.get("target_message_id") or tool_args.get("message_id")
        clean_mid = re.sub(r"[^0-9]", "", str(target_mid or ""))
        if clean_mid and hasattr(channel, "fetch_message"):
            try:
                target_message = await channel.fetch_message(int(clean_mid))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                target_message = None

        # Forum/threads: the LLM often passes the forum post's starter
        # message id as target_message_id but omits target_channel_id (the
        # thread id). When fetch on the resolved channel misses, scan channel
        # memory for a row whose message_id matches, switch to that channel,
        # and retry. Also covers the case where the resolved channel was a
        # stale fallback (e.g. an auto_channel that's been deleted) — the
        # message really lives in a thread the bot never listed by id.
        if target_message is None and clean_mid:
            resolved_cid = await self._find_channel_for_message_id(clean_mid)
            if resolved_cid and str(resolved_cid) != str(getattr(channel, "id", "")):
                alt_channel = None
                try:
                    alt_channel = self.bot.get_channel(int(resolved_cid))
                    if alt_channel is None:
                        alt_channel = await self.bot.fetch_channel(int(resolved_cid))
                except (ValueError, TypeError, discord.NotFound, discord.Forbidden, discord.HTTPException):
                    alt_channel = None
                if alt_channel is not None and hasattr(alt_channel, "fetch_message"):
                    if self._channel_allowed(str(resolved_cid)):
                        channel = alt_channel
                        try:
                            target_message = await channel.fetch_message(int(clean_mid))
                        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                            target_message = None

        if tool_name == "react" and target_message is None:
            result["result"] = "error"
            result["error"] = (
                f"react requires a valid target_message_id for autonomy "
                f"(message {clean_mid or target_mid!r} not found in any known channel)"
            )
            return

        # build synthetic message
        bot_user = getattr(self.bot, "user", None)
        author = SimpleNamespace(
            id="autonomy",
            display_name=getattr(bot_user, "display_name", None)
            or getattr(bot_user, "name", None)
            or getattr(self.bot, "bot_name", "Maxwell"),
            name=getattr(bot_user, "name", None) or getattr(self.bot, "bot_name", "Maxwell"),
            bot=True,
        )
        guild = channel.guild if hasattr(channel, "guild") else None
        syn_msg = SyntheticMessage(
            channel=channel,
            author=author,
            guild=guild,
            content=tool_args.get("content", tool_args.get("prompt", "")),
            target_message=target_message,
        )

        # extract tool kwargs (exclude meta fields that aren't real tool params)
        exec_kwargs = {
            k: v
            for k, v in tool_args.items()
            if k not in {"target_channel_id"}
        }
        if "target_message_id" in exec_kwargs and "message_id" not in exec_kwargs:
            exec_kwargs["message_id"] = exec_kwargs["target_message_id"]
        try:
            tool_result = await tool.execute(syn_msg, **exec_kwargs)
            result["result"] = "success"
            result["content_summary"] = (
                str(tool_result)[:300] if tool_result else result["content_summary"]
            )
        except Exception as e:
            result["result"] = "error"
            result["error"] = str(e)[:1000]

    async def _exec_update_memory(self, action: dict, result: dict):
        content = action["content"][:MAX_CONTENT_CHARS]
        result["content_summary"] = content[:200]
        result["target"] = "memory"

        try:
            memory = cast(Any, getattr(self.bot, "memory", None))
            if memory is None:
                result["result"] = "error"
                result["error"] = "memory manager unavailable"
                return
            await memory.add_long_term_memory(content)
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

    async def _log_tick(
        self,
        context: str,
        actions: list[dict],
        results: list[dict],
        duration: float,
        tick_start_iso: str | None = None,
    ):
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
            s["actions_executed_total"] = (
                s.get("actions_executed_total", 0) + total_exec
            )
            s["actions_failed_total"] = s.get("actions_failed_total", 0) + total_fail

        await self.store.update_state(_update)

        # Auto-bump last_acted_on for active goals when this tick actually did
        # something successful. Asking the LLM to "re-create the goal" to bump
        # the timestamp never worked (0 create_goal actions across 200 ticks),
        # so goals stayed at last_acted_on=null even while Maxwell was clearly
        # acting on them. Track it here instead — server-side, reliable.
        acted = total_exec > 0 and any(
            r.get("result") == "success" and r.get("kind") != "do_nothing"
            for r in results
        )
        if acted:
            try:
                goals = await self.store.load_goals()
                active = [g for g in goals if g.get("active")]
                if active:
                    when = tick_start_iso or _utcnow_iso()
                    for g in active:
                        g["last_acted_on"] = when
                    await self.store.save_goals(goals)
            except Exception as e:
                logger.warning(f"Failed to auto-bump goal last_acted_on: {e}")

        # log each action
        for action, result in zip(actions, results, strict=False):
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
