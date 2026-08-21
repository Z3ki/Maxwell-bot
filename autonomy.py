"""AutonomyEngine — Maxwell's self-directed life loop.

Runs alongside REM and on_message. Wakes every N seconds, gathers context
(DMs, channel history, memory, goals, recent events), asks the LLM what to
do, and executes actions through the existing tool system.

No approval queues. No shadow mode. Maxwell decides, Maxwell acts.

ARCHITECTURE — where restraint lives:

Restraint is enforced mechanically, not by prompting. The planner prompt gives
Maxwell full freedom over WHAT to do; autonomy_social decides WHERE and WHEN
speaking is his turn, and execute() enforces that as a gate.

This split is the whole design. The earlier arrangement pushed both jobs into
the prompt ("silence is the default, do_nothing is usually correct"), which
does not work: a model told to prefer silence prefers it uniformly, so it
suppresses research, memory, and goal work — none of which anyone can even
see — while STILL occasionally posting at the wrong moment, because "usually"
is not a gate. Restraint you can compute belongs in code; freedom belongs in
the prompt. Don't move either one back.

MAINTAINER NOTES:
- Don't reintroduce a silence-first planner prompt. It buys nothing: the floor
  gate already makes badly-timed speech impossible, and the prompt-level version
  only costs initiative.
- Autonomy exposes every dashboard-enabled tool. If a tool needs a real
  Discord message, SyntheticMessage has to point at target_message_id. Yes,
  this is more annoying. The user explicitly asked for all tools.
- The context budget is PER-SECTION now, not global truncation. The old
  version truncated from the end, so channel activity (the most actionable
  data) got eaten first. Don't "simplify" back to global truncation.
- The floor gate re-reads the room at execute time rather than trusting the
  verdict from gather_context. The plan is seconds stale by then and rooms
  move; a cached opinion is how you post on top of a reply that started while
  the planner was thinking.
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
from types import SimpleNamespace
from typing import Any, cast

import discord

from control_defaults import (
    DEFAULT_CONTROL,
)  # noqa: E402
from utils import (  # noqa: E402
    JsonStateStore,
    _atomic_json_write_sync,
    _load_json_safe,
    _safe_int,
    _truncate,
    _utcnow_iso,
)
from utils import (
    _coerce_utc_datetime as _coerce_utc_dt_shared,
)
from utils import (
    _discord_display_name as _discord_display_name_shared,
)
from utils import (
    _discord_id as _discord_id_shared,
)
from utils import (
    render_discord_context_text as _render_discord_context_text,
)
from autonomy_social import (  # noqa: E402
    FloorSettings,
    FloorVerdict,
    floor_message_from_discord,
    read_floor,
    render_floor_section,
    summarize_floor,
)

logger = logging.getLogger(__name__)


# Regex constants, _discord_display_name, _discord_id, _coerce_utc_datetime,
# and _render_discord_context_text are now imported from utils.py


def _user_ref(obj: Any, bot_user: Any = None) -> str:
    uid = _discord_id(obj)
    if bot_user is not None and uid == str(getattr(bot_user, "id", "")):
        return f"you/Maxwell({uid})"
    return f"{_discord_display_name(obj)}({uid})"


def _visible_message_content(
    message: Any,
    content: str | None = None,
    *,
    known_users: dict | None = None,
) -> str:
    text = _render_discord_context_text(message, content, known_users=known_users)
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
    message: Any, *, bot_user: Any = None, reply: Any = None, private: bool = False
) -> list[str]:
    """`private` marks a 1:1 DM, where an inbound message is aimed at Maxwell
    whether or not it carries a mention. Without it these lines came out as
    `addressed_to=channel` in a room with no channel and one other person."""
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

    if addressed:
        tags.append("addressed_to=[" + "; ".join(addressed) + "]")
    elif private:
        is_self = bot_user is not None and str(
            getattr(getattr(message, "author", None), "id", "")
        ) == str(getattr(bot_user, "id", ""))
        tags.append("addressed_to=you" if not is_self else "addressed_to=them")
    else:
        tags.append("addressed_to=channel")
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
            f"reply_to={reply_label}({reply_id})"
            if reply_id
            else f"reply_to={reply_label}"
        )
    mentions = msg.get("mentions") if isinstance(msg.get("mentions"), list) else []
    mention_bits = [
        f"@{item.get('name', 'unknown')}({item.get('id', 'unknown')})"
        for item in (mentions or [])[:10]
        if isinstance(item, dict)
    ]
    if mention_bits:
        relation_bits.append("mentions=" + ",".join(mention_bits))
    relation = f" [{'; '.join(relation_bits)}]" if relation_bits else ""
    return f"{prefix}{label}{relation}: {str(msg.get('content', ''))[:600]}"


def _classify_conversation(
    bot: Any, channel_id: str, channel: Any = None
) -> tuple[str, str]:
    """(kind, display_name) for a room. kind is guild / dm / group / unknown.

    Autonomy used to render every room as `#{name}` and fall back to the
    snowflake when there was no name — so a DM came out as `#1418...` and a
    group DM as `#None`, both indistinguishable from a real text channel. The
    planner then posted into them. Classify once, here, and let every context
    section print the result.
    """
    cid = re.sub(r"[^0-9]", "", str(channel_id or ""))
    ch = channel
    if ch is None and cid:
        with contextlib.suppress(Exception):
            ch = bot.get_channel(_safe_int(cid))
    if ch is None and cid:
        for private in list(getattr(bot, "private_channels", []) or []):
            if str(getattr(private, "id", "")) == cid:
                ch = private
                break
    if ch is None:
        return "unknown", ""
    if isinstance(ch, discord.DMChannel):
        recipient = getattr(ch, "recipient", None)
        who = (
            _user_ref(recipient, getattr(bot, "user", None))
            if recipient is not None
            else "unknown user"
        )
        return "dm", who
    if isinstance(ch, discord.GroupChannel):
        name = getattr(ch, "name", None)
        if not name:
            people = [
                str(getattr(r, "display_name", None) or getattr(r, "name", "?"))
                for r in (getattr(ch, "recipients", None) or [])
            ]
            name = ", ".join(people[:3])
            if len(people) > 3:
                name += f" +{len(people) - 3}"
        return "group", str(name or "group dm")
    return "guild", str(getattr(ch, "name", None) or cid)


def _conversation_label(bot: Any, channel_id: str) -> str:
    """Human-readable channel/DM label for autonomy context."""
    cid = re.sub(r"[^0-9]", "", str(channel_id or ""))
    if not cid:
        return str(channel_id or "unknown")
    kind, name = _classify_conversation(bot, cid)
    if kind == "dm":
        return f"DM with {name}"
    if kind == "group":
        return f"group DM ({name})"
    if kind == "guild":
        channel = None
        with contextlib.suppress(Exception):
            channel = bot.get_channel(_safe_int(cid))
        guild_name = getattr(getattr(channel, "guild", None), "name", None)
        if guild_name:
            return f"#{name} in {guild_name}"
        return f"#{name}"
    return f"channel={cid}"


class AutonomyContextIndex:
    """Maps short handles shown to the planner to real Discord IDs for one tick.

    Three deliberately non-colliding handle spaces:

      guild text channel -> "3"     (postable with post_channel)
      DM                 -> "D1"    (reply with send_dm + target_user_id)
      group DM           -> "G1"    (postable with post_channel)
      unreadable room    -> "X1"    (not addressable at all)

    Everything used to share one integer space, and only guild channels were
    ever listed in AVAILABLE CHANNELS — so a DM that showed up in the event
    feed got a plain number like `channel=3`, the floor block invited the
    planner to speak there, and the "channel post" landed in someone's DM or
    group chat. A number can no longer name a private room: to post in one the
    planner has to type its prefix, and to post in a guild channel it has to
    use a number that only guild channels ever get.

    Resolution happens in _parse_plan, before anything is sent.
    """

    KIND_GUILD = "guild"
    KIND_DM = "dm"
    KIND_GROUP = "group"
    KIND_UNKNOWN = "unknown"

    _PREFIX = {KIND_DM: "D", KIND_GROUP: "G", KIND_UNKNOWN: "X"}

    def __init__(self):
        self.channel_by_idx: dict[int, str] = {}
        self.channel_idx_by_id: dict[str, int] = {}
        self.message_by_idx: dict[int, str] = {}
        self.message_channel_by_idx: dict[int, str] = {}
        self.msg_idx_by_id: dict[str, int] = {}
        self.kind_by_id: dict[str, str] = {}
        self.name_by_id: dict[str, str] = {}
        self.handle_by_id: dict[str, str] = {}
        self.id_by_handle: dict[str, str] = {}
        self._next_channel = 1
        self._next_message = 1
        self._next_by_kind: dict[str, int] = {}

    # -- registration ----------------------------------------------------
    def add_ref(self, channel_id: str, *, kind: str = KIND_GUILD, name: str = "") -> str:
        """Register a room and return the handle the planner will see.

        First registration wins the kind. `_collect_available_channels` runs
        before every other context section and registers exactly the channels
        Maxwell may post in, so anything discovered later that isn't already
        known as a guild channel is, by construction, not one.
        """
        cid = re.sub(r"[^0-9]", "", str(channel_id or ""))
        if not cid:
            return ""
        if name:
            self.name_by_id.setdefault(cid, name)
        existing = self.handle_by_id.get(cid)
        if existing:
            return existing
        self.kind_by_id[cid] = kind
        if kind == self.KIND_GUILD:
            idx = self._next_channel
            self._next_channel += 1
            self.channel_by_idx[idx] = cid
            self.channel_idx_by_id[cid] = idx
            handle = str(idx)
        else:
            prefix = self._PREFIX.get(kind, "X")
            num = self._next_by_kind.get(prefix, 1)
            self._next_by_kind[prefix] = num + 1
            handle = f"{prefix}{num}"
        self.handle_by_id[cid] = handle
        self.id_by_handle[handle.upper()] = cid
        return handle

    def add_channel(self, channel_id: str) -> int:
        """Register a guild text channel; returns its integer index (0 if none)."""
        handle = self.add_ref(channel_id, kind=self.KIND_GUILD)
        try:
            return int(handle)
        except (TypeError, ValueError):
            return 0

    def add_message(self, message_id: str, channel_id: str) -> int:
        mid = re.sub(r"[^0-9]", "", str(message_id or ""))
        cid = re.sub(r"[^0-9]", "", str(channel_id or ""))
        if not mid:
            return 0
        # One message, one number. The same message routinely shows up in the
        # event feed, in channel activity and in DM history; handing it three
        # different msg= ids taught the planner that these numbers are
        # arbitrary, which is half of why it typed them into channel slots.
        existing = self.msg_idx_by_id.get(mid)
        if existing is not None:
            if cid:
                self.message_channel_by_idx[existing] = cid
            return existing
        idx = self._next_message
        self._next_message += 1
        self.message_by_idx[idx] = mid
        self.msg_idx_by_id[mid] = idx
        if cid:
            self.message_channel_by_idx[idx] = cid
        return idx

    # -- display ---------------------------------------------------------
    def describe(self, channel_id: str) -> str:
        """The one label every context section prints for a room."""
        cid = re.sub(r"[^0-9]", "", str(channel_id or ""))
        handle = self.handle_by_id.get(cid, "")
        kind = self.kind_by_id.get(cid, self.KIND_UNKNOWN)
        name = self.name_by_id.get(cid, "")
        if not handle:
            return "room=unknown"
        if kind == self.KIND_GUILD:
            return f"channel={handle}" + (f"(#{name})" if name else "")
        if kind == self.KIND_DM:
            return f"dm={handle}" + (f"(with {name})" if name else "")
        if kind == self.KIND_GROUP:
            return f"group={handle}" + (f"({name})" if name else "")
        return f"unreachable={handle}"

    # -- resolution ------------------------------------------------------
    @staticmethod
    def _digits(raw: str) -> str:
        return re.sub(r"[^0-9]", "", str(raw or "").strip())

    def resolve_channel_ref(self, raw: str) -> tuple[str | None, str]:
        """Resolve a planner-supplied target into (channel_id, error).

        Never guesses. An unknown handle comes back as an error the planner
        gets to read next tick, which is strictly better than fabricating an
        id or, worse, hitting a real room the planner didn't mean.
        """
        token = str(raw or "").strip().strip("#<>@").upper()
        if not token:
            return None, ""
        if token in self.id_by_handle:
            cid = self.id_by_handle[token]
            kind = self.kind_by_id.get(cid, self.KIND_UNKNOWN)
            if kind == self.KIND_DM:
                return None, (
                    f"{raw} is a DM, not a channel — reply there with "
                    f"send_dm and the recipient's user id"
                )
            if kind == self.KIND_UNKNOWN:
                return None, f"{raw} is a room autonomy can't read or post in"
            return cid, ""
        digits = self._digits(token)
        if not digits:
            return None, (
                f"'{raw}' is not a room handle — use channel=N from "
                f"AVAILABLE CHANNELS, or a G-handle for a group DM"
            )
        if len(digits) >= 15:
            # A real snowflake. Allowed for compatibility with tools and
            # config that carry raw ids, but never for a private room the
            # planner merely saw quoted somewhere in context.
            kind = self.kind_by_id.get(digits)
            if kind == self.KIND_DM:
                return None, (
                    "that id is a DM channel — use send_dm with target_user_id"
                )
            return digits, ""
        return None, (
            f"no room numbered {digits} in this context — pick a number "
            f"from AVAILABLE CHANNELS"
        )

    def resolve_channel(self, raw: str) -> str | None:
        return self.resolve_channel_ref(raw)[0]

    def resolve_message(self, raw: str) -> tuple[str | None, str | None]:
        digits = self._digits(raw)
        if not digits:
            return None, None
        if len(digits) >= 15:
            return digits, None
        try:
            num = int(digits)
        except ValueError:
            return None, None
        if num in self.message_by_idx:
            return self.message_by_idx[num], self.message_channel_by_idx.get(num)
        return None, None


# _render_discord_context_text imported from utils.py


AUTONOMY_VALID_KINDS = frozenset(
    {
        "send_dm",
        "post_channel",
        "run_tool",
        "update_memory",
        "create_goal",
        "complete_goal",
        "do_nothing",
    }
)
MAX_ACTIONS_PER_TICK = 3  # reduced from 5 — prevents spam bursts
# Tool-loop: after a run_tool returns output, autonomy gets to look at that
# output and decide a follow-up in the SAME tick, instead of waiting for the
# next tick (minutes later) to see a 180-char summary. Bounded hard — each
# round is a real LLM call and every action still runs the normal validation
# and post-gating.
MAX_TOOL_LOOP_ROUNDS = 3  # continuation rounds after the initial plan
MAX_TOOL_LOOP_ACTIONS = 6  # total executed actions per tick, all rounds
TOOL_OUTPUT_FEEDBACK_CHARS = 1500  # per-tool output shown back to the planner
MAX_CONTENT_CHARS = 1900
LOG_RING_SIZE = 200

# Internal "drives" — Maxwell's evolving self-directed wants. Each tick they
# decay toward a baseline and get bumped by stimuli (mentions, links, idle
# time, stale goals), so what Maxwell "feels like doing" drifts over time
# instead of being a fixed reaction machine. Persisted in autonomy_state.json
# under "drives" so wants survive restarts and accumulate personality.
DRIVE_NAMES = ("curiosity", "social", "creative", "reflective", "restless")
DRIVE_BASELINE = {
    "curiosity": 0.45,
    "social": 0.35,
    "creative": 0.25,
    "reflective": 0.20,
    "restless": 0.15,
}
DRIVE_DECAY = 0.10          # fraction moved toward baseline each tick
DRIVE_JITTER = 0.05         # +/- random noise per tick (lifelike drift)
IDLE_INITIATIVE_THRESHOLD = 0.45  # top drive above this + nothing external => act on your own
# Stimulus bumps (per-unit contributions to a drive)
DRIVE_BUMP_MENTION_YOU = 0.25
DRIVE_BUMP_REPLY_TO_YOU = 0.20
DRIVE_BUMP_ENGAGEMENT = 0.12
DRIVE_BUMP_LINK = 0.08
DRIVE_BUMP_IDLE_PER_TICK = 0.04   # restless grows when nothing happens
DRIVE_BUMP_STALE_GOAL = 0.10      # reflective grows per stale goal
DRIVE_DESCRIPTIONS = {
    "curiosity": "wants to learn — research a topic (web_search/fetch_url) and save findings (update_memory)",
    "social": "wants to interact — reply/react where there's a real opening",
    "creative": "wants to make or share something original",
    "reflective": "wants to consolidate — review/retire stale goals (complete_goal), tidy memory",
    "restless": "wants to do SOMETHING — pick any genuinely useful self-directed action",
}

# Tools that post a visible message to a channel. autonomy's run_tool path
# builds a SyntheticMessage against the target channel, so these must be
# treated like post_channel for the unprompted-post rate-limit.
AUTONOMY_POST_TOOLS = frozenset(
    {
        "send_message",
        "send_file",
        "send_meme",
        "send_media",
        "tts",
    }
)

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
CTX_BUDGET_DRIVES = 800  # internal-wants section; kept compact so it always fits

# Hard safety: these tools are NEVER available to autonomy even if dashboard
# enables them. Prevents autonomy/LLM from server-admin, shell, site creation,
# or other high-risk actions. (Dashboard disabled_tools still apply too.)
AUTONOMY_DISABLED_TOOLS = frozenset(
    {
        "shell",
        "create_site",
        "list_admin_servers",
        "list_servers",
        "create_category",
        "create_channel",
        "edit_channel",
        "delete_channel",
        "change_avatar",
        "set_nickname",
        "forward_message",
        "create_invite",
        "email_send",
        "email_read_inbox",
        "email_get_message",
        "email_search",
        "sleep",
        "clear_sleep",
    }
)


# ---------------------------------------------------------------------------
# Atomic JSON helpers (same pattern as memory.py / rem.py)
# ---------------------------------------------------------------------------


# _atomic_json_write_sync imported from utils.py


def _truncate_keep_tail(text: str, budget: int) -> str:
    """Keep newest lines when context gets too fat. Front truncation betrayed us."""
    budget = max(0, _safe_int(budget, 0))
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
        raise discord.NotFound(response=None, message="target message not found")  # type: ignore

    async def remove_reaction(self, emoji, member):
        pass  # same

    async def edit(self, **kwargs):
        raise NotImplementedError("Cannot edit a SyntheticMessage")

    async def delete(self, *args, **kwargs):
        pass  # silently ignore


# ---------------------------------------------------------------------------
# AutonomyStore — JSON-backed persistence
# ---------------------------------------------------------------------------


class AutonomyStore(JsonStateStore):
    """Manages the three autonomy data files with atomic writes.

    State + audit log come from utils.JsonStateStore; goals are autonomy's own.
    """

    log_ring_size = LOG_RING_SIZE

    def __init__(self, data_dir: str):
        super().__init__(
            data_dir,
            state_file="autonomy_state.json",
            log_file="autonomy_log.json",
        )
        self.goals_file = self.data_dir / "autonomy_goals.json"

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
                # Goal-specific progress watermark for stale detection. Unlike
                # last_acted_on (bumped for ALL goals on any successful tick as
                # a "Maxwell is alive" signal), this ONLY advances when a goal
                # is explicitly referenced this tick — so staleness reflects
                # "not formally touched" rather than "Maxwell did anything."
                "last_progress_at": _utcnow_iso(),
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

    async def complete_goal(self, goal_id: str) -> dict | None:
        """Mark an active goal complete (active=False, stamped completed_at).

        Returns the updated goal dict, or None if the id wasn't found. This is
        the autonomy-side goal lifecycle: the planner retires its own goals so
        they don't linger at last_acted_on=null forever.
        """
        async with self._lock:
            data = await asyncio.to_thread(_load_json_safe, self.goals_file, dict)
            goals = data.get("goals", []) if isinstance(data, dict) else []
            if not isinstance(goals, list):
                goals = []
            for g in goals:
                if g.get("id") == goal_id:
                    g["active"] = False
                    g["completed_at"] = _utcnow_iso()
                    g["last_progress_at"] = _utcnow_iso()
                    await asyncio.to_thread(
                        _atomic_json_write_sync, self.goals_file, {"goals": goals}
                    )
                    return g
            return None

    # -- action log (ring buffer) --

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
        self._lock = asyncio.Lock()  # serializes per-tick sections of state mutation
        # Single-flight generation counter. Two concurrent ticks must never both
        # run — the old `force release lock on timeout` path was a race that let
        # overlapping ticks share state. A monotonic counter, compared at entry,
        # guarantees at most one tick is in flight.
        self._tick_in_flight = False
        self._last_thought = ""  # avoid AttributeError on early failure
        # Track posted message IDs for engagement checking: [{msg_id, channel_id, timestamp}]
        self._posted_messages: list[dict] = []
        # Validation failures from last tick (fed back into context)
        self._last_validation_failures: list[str] = []
        # Channel/message index built during gather_context for this tick
        self._context_index: AutonomyContextIndex | None = None
        # Drives computed during gather_context, persisted in _log_tick (folded
        # into the single state write so a tick = one atomic state write, not
        # two). None means "no drives computed this tick".
        self._pending_drives: dict | None = None
        # Set during gather_context when a reflection nudge was emitted; _log_tick
        # stamps last_reflect_at so the cadence persists across restarts.
        self._reflect_pending_persist: bool = False
        # Per-channel turn-taking verdicts read during gather_context. The
        # planner sees them as context; execute() re-checks them against live
        # state before anything sends. See autonomy_social.
        self._floor_verdicts: dict[str, FloorVerdict] = {}
        # user_id -> DM channel id, so send_dm can be gated by the same floor
        # rules as a channel post.
        self._dm_channel_by_user: dict[str, str] = {}
        # Inverse of the above: DM channel id -> the user send_dm must target.
        self._dm_user_by_channel: dict[str, str] = {}
        # Track users whose DMs are closed / bot is blocked so autonomy doesn't spam retry: user_id -> failure_ts
        self._unreachable_dm_users: dict[str, float] = {}

    async def _register_conversation(
        self, ctx_index: AutonomyContextIndex, channel_id: str, channel: Any = None
    ) -> tuple[str, str]:
        """Register a room for this tick and return (handle, display label).

        Every context section routes through this so the planner sees one
        naming scheme everywhere: `channel=3(#general)`, `dm=D1(with Z3ki)`,
        `group=G1(Z3ki, dirac)`. Rooms that aren't guild channels can't come
        out looking like one.

        The first registration fixes a room's kind for the whole tick, so it
        has to be right the first time — a later section can't renumber a
        handle that's already been printed. On the tick right after a restart
        the guild cache is still cold and `get_channel` returns None for real
        channels, so fall back to one fetch rather than writing them off as
        unreachable for the tick.
        """
        cid = re.sub(r"[^0-9]", "", str(channel_id or ""))
        if not cid:
            return "", "room=unknown"
        if cid not in ctx_index.handle_by_id:
            kind, name = _classify_conversation(self.bot, cid, channel)
            if kind == AutonomyContextIndex.KIND_UNKNOWN:
                fetched = await self._fetch_channel_cached(cid)
                if fetched is not None:
                    kind, name = _classify_conversation(self.bot, cid, fetched)
            ctx_index.add_ref(cid, kind=kind, name=name)
        elif channel is not None and not ctx_index.name_by_id.get(cid):
            _, name = _classify_conversation(self.bot, cid, channel)
            if name:
                ctx_index.name_by_id[cid] = name
        return ctx_index.handle_by_id.get(cid, ""), ctx_index.describe(cid)

    async def _fetch_channel_cached(self, cid: str) -> Any:
        """One bounded fetch per channel per tick, negative results remembered."""
        cache = getattr(self, "_channel_fetch_cache", None)
        if cache is None:
            cache = self._channel_fetch_cache = {}
        if cid in cache:
            return cache[cid]
        ch = None
        try:
            ch = await asyncio.wait_for(
                self.bot.fetch_channel(_safe_int(cid)), timeout=5
            )
        except Exception:
            ch = None
        cache[cid] = ch
        return ch

    def _resolve_planner_channel_ref(self, raw: str) -> tuple[str | None, str]:
        idx = getattr(self, "_context_index", None)
        if idx is not None:
            return idx.resolve_channel_ref(raw)
        # No index means we're outside a real tick — gather_context always
        # builds one before plan() runs. Fall back to the plain digit parse
        # rather than rejecting everything.
        digits = re.sub(r"[^0-9]", "", str(raw or ""))
        return (digits or None), ""

    def _resolve_planner_channel(self, raw: str) -> str | None:
        return self._resolve_planner_channel_ref(raw)[0]

    def _resolve_planner_message(self, raw: str) -> tuple[str | None, str | None]:
        idx = getattr(self, "_context_index", None)
        if idx is not None:
            return idx.resolve_message(raw)
        digits = re.sub(r"[^0-9]", "", str(raw or ""))
        return (digits or None), None

    async def _collect_available_channels(
        self, ctx_index: AutonomyContextIndex
    ) -> list[str]:
        """Build numbered AVAILABLE CHANNELS lines and populate ctx_index."""
        now_ts = time.time()
        ch_map_lines = []
        for guild in self.bot.guilds:
            guild_id = getattr(guild, "id", None)
            if guild_id is None:
                continue
            if not self._guild_allowed(str(guild_id)):
                continue
            for ch in getattr(guild, "text_channels", None) or []:
                try:
                    if getattr(guild, "me", None) is None:
                        continue
                    perms = ch.permissions_for(guild.me)
                    ch_id = getattr(ch, "id", None)
                    if ch_id is None:
                        continue
                    if not perms.send_messages or not self._channel_allowed(str(ch_id)):
                        continue
                    idx = ctx_index.add_ref(
                        str(ch.id),
                        kind=AutonomyContextIndex.KIND_GUILD,
                        name=str(getattr(ch, "name", "") or ch.id),
                    )
                    tags = []
                    if str(ch.id) in (self.bot._auto_channels or set()):
                        tags.append("auto")
                    topic = getattr(ch, "topic", None) or ""
                    topic_snippet = topic[:80].replace("\n", " ") if topic else ""
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
                        f"  {idx}: #{ch.name}{tag_str}{recency_str}{topic_str}"
                    )
                except Exception:
                    continue
        return ch_map_lines

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

        Also respects dedicated autonomy_blocked_channels and autonomy_blocked_servers
        (guild blacklists) so you can keep normal replies but silence autonomy in noisy
        or unwanted servers/channels.
        """
        control = getattr(self.bot, "_control", None) or {}
        cid = str(channel_id)
        if not control.get("bot_enabled", True):
            return False
        if cid in set(control.get("blocked_channels", []) or []):
            return False
        if cid in set(control.get("autonomy_blocked_channels", []) or []):
            return False
        allowed = set(control.get("allowed_channels", []) or [])
        if allowed and cid not in allowed:
            return False
        # Also gate by server/guild blacklist (resolve via cache if possible)
        try:
            ch = self.bot.get_channel(int(cid))
            if ch is not None:
                g = getattr(ch, "guild", None)
                if g and str(g.id) in set(
                    control.get("autonomy_blocked_servers", []) or []
                ):
                    return False
        except Exception:
            pass
        return True

    def _guild_allowed(self, guild_id: str | None) -> bool:
        """Check if autonomy should act inside a given guild/server."""
        if not guild_id:
            return True
        control = getattr(self.bot, "_control", None) or {}
        gid = str(guild_id)
        if gid in set(control.get("autonomy_blocked_servers", []) or []):
            return False
        return True

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
                else:
                    # Reset backoff while disabled so re-enabling after a run
                    # of failures doesn't delay the first tick by up to 6x.
                    consecutive_failures = 0
            except asyncio.CancelledError as _exc:
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
            # 1 fail=2x, 2=4x, 3+=6x (cap). Cap the exponent first to avoid a
            # huge 2**N on a long-dead endpoint. With 300s base: max 30 min.
            backoff = (
                min(1 << min(consecutive_failures, 3), 6)
                if consecutive_failures > 0
                else 1
            )
            base_sleep = max(30, interval * backoff)
            # Randomize the tick so autonomy wakes at irregular, lifelike
            # intervals instead of a metronomic fixed cadence. Jitter ranges
            # from half to 1.5x the configured interval — e.g. a 300s base
            # becomes anywhere from ~2.5m to ~7.5m. Keeps the min 30s floor.
            sleep_for = _safe_int(base_sleep * random.uniform(0.5, 1.5), 300)
            await asyncio.sleep(max(30, sleep_for))

    # -- single tick --

    async def tick(self) -> dict:
        """One autonomy cycle. Skipped if previous tick still running."""
        # Single-flight: bail early if a tick is already running. The legacy
        # `force release lock after 600s` path was a real race — overlapping
        # ticks could share state and post duplicate messages. The check-and-
        # set is atomic in the asyncio sense (no await between read and write),
        # so two concurrent tick() calls cannot both pass the guard.
        if self._tick_in_flight:
            logger.debug("Autonomy tick skipped: previous tick still in flight")
            return {"skipped": True, "reason": "previous tick in flight"}
        self._tick_in_flight = True
        acquired = False
        try:
            # Bounded wait for the short critical section only. 10m was absurd
            # for a state-mutation lock and forced the force-release hack.
            try:
                await asyncio.wait_for(self._lock.acquire(), timeout=30)
                acquired = True
            except asyncio.TimeoutError:
                logger.error(
                    "Autonomy tick lock timed out (>30s) — previous state mutation hung; skipping"
                )
                # The inner try/finally below resets _tick_in_flight, but a
                # `return` is NOT caught by `except BaseException`, so without
                # this reset the flag would stay True forever and permanently
                # disable autonomy (every later tick() bails on the in-flight
                # check). Reset here on the lock-timeout exit path.
                self._tick_in_flight = False
                return {"skipped": True, "reason": "lock timeout"}
            try:
                # BUG FIX: capture tick START time as watermark. Events recorded during
                # plan/execute have timestamps between start and end. Using end-of-tick
                # as watermark (old behavior) drops those events from the next tick.
                tick_start_iso = _utcnow_iso()
                start = time.time()
                # Clear per-tick transient state. If a previous tick died before
                # _log_tick ran, stale _pending_drives would otherwise leak into
                # this tick's persistence with wrong stimuli.
                self._pending_drives = None
                self._reflect_pending_persist = False
                try:
                    # gather_context does many Discord history fetches + up to 3
                    # youtube fetches with no per-call timeout; one hung fetch
                    # used to stall the tick (and, via single-flight, the whole
                    # autonomy loop) indefinitely. Bound it generously so a true
                    # hang is recovered instead of freezing the engine forever.
                    try:
                        context = await asyncio.wait_for(
                            self.gather_context(), timeout=180
                        )
                    except asyncio.TimeoutError:
                        logger.error(
                            "Autonomy gather_context timed out (>180s); "
                            "skipping tick to recover the loop"
                        )
                        raise RuntimeError("gather_context timed out")
                    actions, results = await self._plan_execute_loop(context)
                    duration = time.time() - start
                    await self._log_tick(
                        context, actions, results, duration, tick_start_iso
                    )
                    return {
                        "skipped": False,
                        "actions": len(results),
                        "duration": duration,
                    }
                except Exception as e:
                    duration = time.time() - start
                    logger.error(f"Autonomy tick failed: {e}")
                    # Do not advance last_tick on failure — drain_slice would
                    # skip events the failed tick never planned over.
                    await self.store.patch_state(
                        {
                            "last_tick_duration": round(duration, 2),
                            "last_error": str(e)[:2000],
                        }
                    )
                    return {"skipped": False, "error": str(e), "duration": duration}
            finally:
                if acquired:
                    self._lock.release()
                self._tick_in_flight = False
        except BaseException:
            # Safety net: never leave _tick_in_flight True on any exit (including
            # CancelledError). Without this, a mid-tick cancellation would
            # permanently disable autonomy.
            self._tick_in_flight = False
            raise

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
    # Turn-taking — see autonomy_social for the rules themselves
    # -----------------------------------------------------------------------

    def _floor_settings(self) -> FloorSettings:
        return FloorSettings.from_control(getattr(self.bot, "_control", None))

    def _floor_enabled(self) -> bool:
        """Turn-taking is on unless an operator explicitly switches it off.

        Off means the planner still SEES the room read (it's useful context)
        but execute() stops enforcing it. That's an escape hatch for debugging,
        not a mode anyone should run in — with it off, autonomy will eventually
        post over a live reply.
        """
        control = getattr(self.bot, "_control", None) or {}
        return bool(control.get("autonomy_floor_enabled", True))

    def _read_floor_for(
        self, channel_id: str, messages: list, *, label: str = ""
    ) -> FloorVerdict:
        """Apply the turn-taking rules to one channel's message window."""
        cid = str(channel_id)
        replying = cid in (getattr(self.bot, "_replying_channels", None) or set())
        last_reply = (getattr(self.bot, "_last_bot_reply", None) or {}).get(cid)
        return read_floor(
            cid,
            messages,
            is_replying=replying,
            last_bot_reply_ts=last_reply,
            settings=self._floor_settings(),
            label=label or _conversation_label(self.bot, cid),
        )

    async def _floor_check_live(self, channel_id: str) -> FloorVerdict | None:
        """Re-read a room immediately before speaking into it.

        The plan was made after gather_context and an LLM call — easily tens of
        seconds ago, during which someone may have spoken or the main bot may
        have started replying. The cached verdict is a stale opinion; this is
        the current one. Returns None if the room can't be read, in which case
        the caller falls back to the cached verdict.
        """
        cid = str(channel_id)
        try:
            ch = cast(Any, self.bot.get_channel(_safe_int(cid)))
            if ch is None or not hasattr(ch, "history"):
                return None
            msgs = [m async for m in ch.history(limit=8)]
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            return None
        except Exception:
            return None
        is_dm = isinstance(ch, discord.DMChannel)
        snapshot = [
            floor_message_from_discord(
                m, bot_user=self.bot.user, implicit_address=is_dm
            )
            for m in msgs
        ]
        return self._read_floor_for(cid, snapshot)

    async def _floor_gate(self, channel_id: str) -> FloorVerdict:
        """The verdict execute() acts on: live read first, cached as fallback.

        Bounded so a slow Discord fetch can't eat the per-action timeout — a
        gate that hangs is a gate that stops autonomy entirely.
        """
        cid = str(channel_id)
        verdict = None
        try:
            verdict = await asyncio.wait_for(self._floor_check_live(cid), timeout=8)
        except Exception:
            verdict = None
        if verdict is None:
            verdict = self._floor_verdicts.get(cid)
        if verdict is None:
            # No live read, no cached one — a channel this tick never looked
            # at. Never return None here: that would silently drop the
            # mid-reply check, which is the one guard that must hold even with
            # zero visibility into the room.
            verdict = self._read_floor_for(cid, [])
        return verdict

    # -----------------------------------------------------------------------
    # gather_context — ordered by decision-relevance, per-section budgets
    # -----------------------------------------------------------------------

    async def gather_context(self) -> str:
        """Collect everything Maxwell currently knows. Sections ordered by
        decision-relevance: most actionable info first, so it survives budget
        truncation. Each section has its own char budget instead of the old
        global truncation that ate channel activity first."""

        sections = []
        ctx_index = AutonomyContextIndex()
        self._context_index = ctx_index
        available_channel_lines = await self._collect_available_channels(ctx_index)
        # Use system local time so the LLM doesn't see UTC and think it's
        # night when it's 5pm. No hardcoding offsets — let the OS decide.
        now = datetime.now().astimezone()

        # 1. Current time + mood framing
        tz_name = now.tzname() or "local time"
        sections.append(
            f"=== CURRENT TIME ===\n{now.strftime('%A, %Y-%m-%d %H:%M')} ({tz_name})"
        )

        # Turn-taking. The CONVERSATION FLOOR block is the single most
        # decision-relevant thing in this whole context — it's what stops
        # autonomy from talking over a live reply or over its own last line —
        # so it sits second, right under the clock. It can only be rendered
        # once channel history has been read further down, so reserve the slot
        # now and fill it at the end. See autonomy_social for the rules.
        floor_slot = len(sections)
        sections.append("")
        # channel_id -> [FloorMessage]; filled by the channel + DM passes below.
        floor_snapshots: dict[str, list] = {}
        self._floor_verdicts = {}
        self._dm_channel_by_user = {}
        self._dm_user_by_channel = {}
        self._channel_fetch_cache = {}

        # 2. Active goals (most decision-relevant — what should I work on?)
        active_goals: list = []
        try:
            goals = await self.store.load_goals()
            active_goals = [g for g in goals if g.get("active")]
            if active_goals:
                stale_days = self._stale_goal_days()
                goal_lines = []
                stale_count = 0
                for g in active_goals:
                    age = self._goal_age_days(g)
                    stale_tag = ""
                    if age is not None and age >= stale_days:
                        stale_count += 1
                        stale_tag = (
                            f" [STALE: {age:.0f}d untouched — retire with "
                            f"complete_goal or act to refresh]"
                        )
                    goal_lines.append(
                        f"- [{g['id']}] {g.get('description', '')} "
                        f"(last acted: {g.get('last_acted_on', 'never')}){stale_tag}"
                    )
                if stale_count:
                    goal_lines.append(
                        f"({stale_count} stale goal(s) above — consider "
                        f"complete_goal to retire, or act on one to refresh.)"
                    )
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
                    ch_label = "room=unknown"
                    if cid != "?":
                        with contextlib.suppress(Exception):
                            ch_label = (
                                await self._register_conversation(ctx_index, cid)
                            )[1]
                    uid = str(ev.get("user_id") or "?")
                    uname = str(ev.get("user_name") or "?")
                    role = str(ev.get("role") or "?")
                    speaker_kind = (
                        "you/Maxwell"
                        if self.bot.user and uid == str(self.bot.user.id)
                        else role
                    )

                    tags = []
                    if ev.get("message_id") and cid != "?":
                        msg_idx = ctx_index.add_message(str(ev.get("message_id")), cid)
                        if msg_idx:
                            tags.append(f"msg={msg_idx}")

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
                        f'time={when} {ch_label} speaker={uname}({uid}, {speaker_kind}) {tag_text} content="{content}"'
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
                    _cid = str(getattr(ch, "id", "") or "")
                    _ku = (getattr(self.bot, "_recent_users", {}) or {}).get(_cid, {})
                    # Resolve the reply BEFORE the empty-content skip: a
                    # message with no renderable text (a bare attachment, a
                    # sticker) is still a turn somebody took, and the floor
                    # read has to see it or Maxwell will talk over it.
                    reply = await self._resolve_reference(m, ref_cache)
                    floor_snapshots.setdefault(_cid, []).append(
                        floor_message_from_discord(
                            m, bot_user=self.bot.user, reply=reply
                        )
                    )
                    content = _visible_message_content(
                        m, m.content or "", known_users=_ku
                    )[:260]
                    if not content:
                        continue
                    age = _context_time(getattr(m, "created_at", None))
                    tags = _message_relation_tags(
                        m,
                        bot_user=self.bot.user,
                        reply=reply,
                        private=isinstance(ch, discord.DMChannel),
                    )
                    tag_text = " ".join(tags)
                    msg_id = str(getattr(m, "id", ""))
                    ch_label = (
                        await self._register_conversation(ctx_index, _cid, ch)
                    )[1]
                    msg_idx = ctx_index.add_message(msg_id, _cid) if msg_id else 0
                    author = _user_ref(getattr(m, "author", None), self.bot.user)
                    ch_lines.append(
                        f'time={age} {ch_label} msg={msg_idx} speaker={author} {tag_text} content="{content}"'
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
                    ch_label = (await self._register_conversation(ctx_index, cid))[1]
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
                        [
                            row
                            for row in rows
                            if isinstance(row, dict)
                            and row.get("is_tool")
                            and id(row) not in recent_ids
                        ][-tool_limit:]
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
                        mem_lines.append(f"# {ch_label}")
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
        engagement = ""
        try:
            engagement = await self._check_post_engagement()
            if engagement:
                sections.append(f"=== ENGAGEMENT WITH YOUR POSTS ===\n{engagement}")
        except Exception as e:
            sections.append(f"=== ENGAGEMENT WITH YOUR POSTS ===\n(error: {e})")

        # 6.5 Internal drives + self-directed initiative. Maxwell's evolving
        # "wants" — decay toward baseline, bumped by this tick's stimuli, then
        # injected so the planner sees what it feels like doing right now. This
        # is the mechanism that lets Maxwell act on its own inclinations instead
        # of only reacting to pings. Persists in _log_tick (folded write).
        control = getattr(self.bot, "_control", None) or {}
        if control.get("autonomy_drives_enabled", True):
            try:
                drive_state = await self.store.load_state()
                stimuli = self._compute_drive_stimuli(
                    events=events,
                    ch_lines=ch_lines,
                    goals=active_goals,
                    engagement_present=bool(engagement),
                    state=drive_state if isinstance(drive_state, dict) else {},
                )
                drives = self._update_drives(
                    drive_state.get("drives") if isinstance(drive_state, dict) else None,
                    stimuli,
                )
                self._pending_drives = drives
                top_name = max(DRIVE_NAMES, key=lambda n: drives[n])
                idle_initiative = (
                    not events
                    and not engagement
                    and drives[top_name] >= IDLE_INITIATIVE_THRESHOLD
                )
                sections.append(
                    _truncate(
                        self._render_drives_section(drives, idle_initiative),
                        CTX_BUDGET_DRIVES,
                    )
                )
            except Exception as e:
                sections.append(f"=== CURRENT DRIVES ===\n(error: {e})")

        # 6.6 Periodic reflection nudge — a self-directed meta-review on its own
        # cadence so Maxwell retires stale goals, consolidates memory, and sets
        # new objectives without a human prompting it. last_reflect_at is stamped
        # in _log_tick when this fires, so the cadence survives restarts.
        if control.get("autonomy_reflect_enabled", True):
            try:
                reflect_state = (
                    drive_state if "drive_state" in locals() else await self.store.load_state()
                )
                reflect_state = reflect_state if isinstance(reflect_state, dict) else {}
                if self._should_reflect(reflect_state):
                    self._reflect_pending_persist = True
                    sections.append(self._render_reflection_section())
            except Exception:
                pass

        # 7. DM + group DM history
        dm_blocks = []
        for channel in list(getattr(self.bot, "private_channels", []) or [])[:20]:
            try:
                cid_private = str(getattr(channel, "id", "") or "")
                handle, room_label = await self._register_conversation(
                    ctx_index, cid_private, channel
                )
                is_group = isinstance(channel, discord.GroupChannel)
                recipient = getattr(channel, "recipient", None)
                recipient_ref = (
                    _user_ref(recipient, self.bot.user)
                    if recipient is not None
                    else room_label
                )
                # A DM is a conversation too, and the "don't answer yourself"
                # rule matters more there than anywhere — there's only one
                # other person to notice. Map recipient -> DM channel so
                # send_dm can be gated on the same read as a channel post.
                if recipient is not None:
                    self._dm_channel_by_user[str(getattr(recipient, "id", ""))] = (
                        cid_private
                    )
                    self._dm_user_by_channel[cid_private] = str(
                        getattr(recipient, "id", "")
                    )
                # Say how to answer, right on the header. A DM is reachable
                # only through send_dm + user id; a group DM only through
                # post_channel + its G-handle. Leaving that implicit is how
                # replies ended up in the wrong room.
                messages = [m async for m in channel.history(limit=20)]
                last_msg_age = ""
                if messages:
                    last_created = getattr(messages[0], "created_at", None)
                    if last_created:
                        last_msg_age = f" (last active {_context_time(last_created)})"

                if is_group:
                    header = (
                        f"{room_label}{last_msg_age} — group DM, reply with "
                        f'post_channel target_channel_id="{handle}"'
                    )
                else:
                    uid = str(getattr(recipient, "id", "")) if recipient else ""
                    header = f"{room_label}{last_msg_age} — private DM, reply with send_dm" + (
                        f" target_user_id={uid}" if uid else ""
                    )
                lines = [header]
                for m in reversed(messages):
                    _cid = str(getattr(channel, "id", "") or "")
                    _ku = (getattr(self.bot, "_recent_users", {}) or {}).get(_cid, {})
                    floor_snapshots.setdefault(_cid, []).append(
                        floor_message_from_discord(
                            m,
                            bot_user=self.bot.user,
                            # In a 1:1 DM every inbound message is addressed
                            # to Maxwell whether or not it carries a mention.
                            implicit_address=True,
                        )
                    )
                    content = _visible_message_content(
                        m, m.content or "", known_users=_ku
                    )[:260]
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
                        else f"from={_user_ref(m.author, self.bot.user)} to="
                        + (
                            room_label
                            if is_group
                            else f"you/Maxwell({getattr(self.bot.user, 'id', '?')})"
                        )
                    )
                    msg_idx = ctx_index.add_message(str(getattr(m, "id", "")), _cid)
                    lines.append(
                        f'time={age} msg={msg_idx} {direction} content="{content}"'
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
                    "=== DIRECT MESSAGES & GROUP DMS (private rooms — NOT in "
                    "AVAILABLE CHANNELS; never target one with a plain "
                    "channel number) ===\n" + "\n\n".join(dm_blocks[-20:]),
                    CTX_BUDGET_DM_HISTORY,
                )
            )
        else:
            sections.append("=== DIRECT MESSAGES & GROUP DMS ===\n(no accessible DMs)")

        # 8. Long-term memory (includes fresh facts from the hourly Intel/news gatherer)
        try:
            memory = cast(Any, getattr(self.bot, "memory", None))
            # get_long_term_memory() does a sync stat()+read_text via mtime reload;
            # run it off the event loop so a slow disk doesn't stall the tick.
            ltm = (
                await asyncio.to_thread(memory.get_long_term_memory)
                if memory
                else []
            )
            if ltm:
                # Recent last (Intel appends new dated facts at the end)
                recent = ltm[-40:] if len(ltm) > 40 else ltm
                ltm_text = "\n".join(str(m) for m in reversed(recent))
                sections.append(
                    _truncate(
                        f"=== LONG-TERM MEMORY (includes recent AI/tech intel facts; newest first) ===\n{ltm_text}",
                        CTX_BUDGET_LTM,
                    )
                )
        except Exception as e:
            sections.append(f"=== LONG-TERM MEMORY ===\n(error: {e})")

        # 9. Available channels map — numbered 1..N so the planner picks by index,
        # not by 18-digit snowflakes it routinely garbles.
        if available_channel_lines:
            # Intentionally limit to reduce "everything is a channel post" bias.
            # Autonomy should also do research, memory updates, goals, DMs, reacts etc.
            sections.append(
                _truncate(
                    "=== AVAILABLE CHANNELS (server text channels — the ONLY rooms a plain number targets; use the number as target_channel_id; only post when you have a real reason; prefer research/update_memory for knowledge goals) ===\n"
                    + "\n".join(available_channel_lines[:18]),
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

        # Fill the reserved CONVERSATION FLOOR slot now that every room has
        # been read. Channels that were fetched but produced no snapshot still
        # get a verdict — an empty room is a readable room.
        try:
            verdicts = []
            for cid, snapshot in floor_snapshots.items():
                # Address rooms exactly the way the rest of the context does —
                # same handle, same spelling — and spell out how to answer in
                # a private one. This block is the first thing the planner
                # reads, so a label here that looks like a channel number is a
                # standing invitation to post into a DM.
                _, label = await self._register_conversation(ctx_index, cid)
                kind = ctx_index.kind_by_id.get(cid, AutonomyContextIndex.KIND_UNKNOWN)
                if kind == AutonomyContextIndex.KIND_DM:
                    uid = self._dm_user_by_channel.get(cid, "")
                    label += (
                        f" [reply with send_dm target_user_id={uid}]"
                        if uid
                        else " [DM — reply with send_dm]"
                    )
                elif kind == AutonomyContextIndex.KIND_GROUP:
                    label += " [group DM]"
                verdict = self._read_floor_for(cid, snapshot, label=label)
                # Always remember the verdict — the execute-time gate needs it
                # for DM channels too, which never carry a channel index.
                self._floor_verdicts[cid] = verdict
                # Only spend prompt space on rooms he could actually post in.
                if self._channel_allowed(cid):
                    verdicts.append(verdict)
            sections[floor_slot] = render_floor_section(verdicts)
            logger.info("Autonomy %s", summarize_floor(verdicts))
        except Exception as e:
            # Failing open here would defeat the point: if the room can't be
            # read, the honest answer is that no room is confirmed open.
            logger.error(f"Autonomy floor read failed: {e}", exc_info=True)
            self._floor_verdicts = {}
            sections[floor_slot] = render_floor_section([])

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
                                    reply,
                                    reply.content or "",
                                    known_users=(
                                        getattr(self.bot, "_recent_users", {}) or {}
                                    ).get(
                                        str(
                                            getattr(
                                                getattr(reply, "channel", None),
                                                "id",
                                                "",
                                            )
                                            or ""
                                        ),
                                        {},
                                    ),
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

    # -----------------------------------------------------------------------
    # self-directed agency: drives, idle initiative, reflection, goal lifecycle
    # -----------------------------------------------------------------------

    def _stale_goal_days(self) -> int:
        return max(
            1,
            _safe_int(
                (getattr(self.bot, "_control", None) or {}).get(
                    "autonomy_goal_stale_days", 14
                ),
                14,
            ),
        )

    def _goal_age_days(self, goal: dict) -> float | None:
        """Age in days since a goal was last *specifically* progressed, for
        stale detection. Prefers last_progress_at (only advances when the goal
        is explicitly referenced by an action) and falls back to created_at.
        Deliberately does NOT use last_acted_on — that field is bumped for ALL
        active goals on any successful tick as a "Maxwell is alive" signal, so
        it would make every goal look perpetually fresh and defeat staleness."""
        when = goal.get("last_progress_at") or goal.get("created_at")
        dt = _coerce_utc_datetime(when)
        if dt is None:
            return None
        return max(
            0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0
        )

    def _compute_drive_stimuli(
        self,
        *,
        events: list,
        ch_lines: list[str],
        goals: list[dict],
        engagement_present: bool,
        state: dict,
    ) -> dict:
        """Turn this tick's context into drive bumps. Pure (no I/O) so it's
        unit-testable. Stimuli are intentionally gentle — drives evolve, they
        don't spike to 1.0 on one mention."""
        stimuli = {
            "mentions_you": 0,
            "replies_to_you": 0,
            "human_msgs": 0,
            "links": 0,
            "idle_bump": 0.0,
            "stale_goals": 0,
            "engagement": bool(engagement_present),
        }
        bot_user = getattr(self.bot, "user", None)
        bot_id = str(getattr(bot_user, "id", "")) if bot_user is not None else ""
        for ev in events or []:
            if not isinstance(ev, dict):
                continue
            role = str(ev.get("role") or "")
            uid = str(ev.get("user_id") or "")
            is_self = bool(bot_id and uid == bot_id) or role == "assistant"
            if not is_self:
                stimuli["human_msgs"] += 1
            if ev.get("reply_to_self"):
                stimuli["replies_to_you"] += 1
            for m in ev.get("mentions") or []:
                if isinstance(m, dict) and str(m.get("id") or "") == bot_id:
                    stimuli["mentions_you"] += 1
        link_re = re.compile(r"https?://", re.IGNORECASE)
        stimuli["links"] = sum(len(link_re.findall(line)) for line in (ch_lines or []))
        stale_days = self._stale_goal_days()
        for g in goals or []:
            if not isinstance(g, dict) or not g.get("active"):
                continue
            age = self._goal_age_days(g)
            if age is not None and age >= stale_days:
                stimuli["stale_goals"] += 1
        # Boredom: restless accumulates the longer it's been since Maxwell
        # actually DID something successful and nothing new is happening.
        if not events:
            last_action_at = state.get("last_action_at") if isinstance(state, dict) else None
            idle_hours = 0.0
            if last_action_at:
                dt = _coerce_utc_datetime(last_action_at)
                if dt is not None:
                    idle_hours = max(
                        0.0,
                        (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0,
                    )
            stimuli["idle_bump"] = DRIVE_BUMP_IDLE_PER_TICK * (
                1.0 + min(idle_hours, 8.0)
            )
        return stimuli

    def _update_drives(self, drives_in: dict | None, stimuli: dict) -> dict:
        """Decay toward baseline, apply stimulus bumps, jitter, clamp to [0,1].
        Pure modulo random — caller may pin random for deterministic tests."""
        drives: dict[str, float] = {}
        src = drives_in if isinstance(drives_in, dict) else {}
        for name in DRIVE_NAMES:
            val = src.get(name)
            drives[name] = float(val) if isinstance(val, (int, float)) else DRIVE_BASELINE[name]
        # Decay toward baseline (keeps wants from ratcheting to the ceiling).
        for name in DRIVE_NAMES:
            base = DRIVE_BASELINE[name]
            drives[name] += (base - drives[name]) * DRIVE_DECAY
        # Stimulus bumps.
        drives["social"] += stimuli.get("mentions_you", 0) * DRIVE_BUMP_MENTION_YOU
        drives["social"] += stimuli.get("replies_to_you", 0) * DRIVE_BUMP_REPLY_TO_YOU
        if stimuli.get("engagement"):
            drives["social"] += DRIVE_BUMP_ENGAGEMENT
        drives["curiosity"] += min(stimuli.get("links", 0), 6) * DRIVE_BUMP_LINK
        drives["curiosity"] += min(stimuli.get("human_msgs", 0), 12) * 0.01
        drives["reflective"] += stimuli.get("stale_goals", 0) * DRIVE_BUMP_STALE_GOAL
        drives["restless"] += float(stimuli.get("idle_bump", 0.0) or 0.0)
        # When nobody's around, creative/restless nudge up a touch (bored maker).
        if drives["social"] < 0.3:
            drives["creative"] += 0.03
            drives["restless"] += 0.02
        # Jitter so the cadence stays lifelike, not metronomic.
        for name in DRIVE_NAMES:
            drives[name] += random.uniform(-DRIVE_JITTER, DRIVE_JITTER)
        for name in DRIVE_NAMES:
            drives[name] = min(1.0, max(0.0, drives[name]))
        return drives

    def _render_drives_section(self, drives: dict, idle_initiative: bool) -> str:
        ordered = sorted(DRIVE_NAMES, key=lambda n: drives.get(n, 0.0), reverse=True)
        lines = [
            "=== CURRENT DRIVES (your evolving wants — acting on them is "
            "legitimate self-initiative, NOT filler) ==="
        ]

        def level(v: float) -> str:
            return "high" if v >= 0.6 else ("mid" if v >= 0.35 else "low")

        for name in ordered[:3]:
            v = float(drives.get(name, 0.0))
            lines.append(f"- {name} {v:.2f} ({level(v)}) — {DRIVE_DESCRIPTIONS[name]}")
        rest = ordered[3:]
        if rest:
            lines.append(
                "(lower: "
                + ", ".join(f"{n} {float(drives.get(n, 0.0)):.2f}" for n in rest)
                + ")"
            )
        if idle_initiative:
            top_name = ordered[0]
            lines.append(
                "IDLE INITIATIVE: nothing external needs you and your "
                f"{top_name} is high — this is exactly the moment to do "
                "something of your own. Research what you're curious about "
                "(web_search/fetch_url, then update_memory to keep it), take "
                "the next step on a goal, retire what's dead with "
                "complete_goal, set a new objective with create_goal, or say "
                "the one original thing you actually have — but only in a room "
                "listed under YOUR TURN. Wanting to is reason enough; you don't "
                "need someone to ask first."
            )
        return "\n".join(lines)

    def _should_reflect(self, state: dict, now: datetime | None = None) -> bool:
        interval = max(
            300,
            _safe_int(
                (getattr(self.bot, "_control", None) or {}).get(
                    "autonomy_reflect_interval_seconds", 3600
                ),
                3600,
            ),
        )
        last = state.get("last_reflect_at") if isinstance(state, dict) else None
        if not last:
            return True  # never reflected -> reflect on the first opportunity
        dt = _coerce_utc_datetime(last)
        if dt is None:
            return True
        now_dt = now if now is not None else datetime.now(timezone.utc)
        now_dt = _coerce_utc_datetime(now_dt) or datetime.now(timezone.utc)
        return (now_dt - dt).total_seconds() >= interval

    def _render_reflection_section(self) -> str:
        return (
            "=== REFLECTION (periodic self-review) ===\n"
            "It's been a bit since you took stock. Self-direct for a moment:\n"
            "- Review ACTIVE GOALS — retire any that are done or abandoned with "
            "complete_goal (pass the goal_id).\n"
            "- If something you learned is worth keeping, save it with update_memory.\n"
            "- If no current goal fits where you are, set a new self-chosen objective "
            "with create_goal.\n"
            "This is a nudge, not a command — HARD RULES still apply."
        )

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
                if (
                    isinstance(row, dict)
                    and str(row.get("message_id") or "") == message_id
                ):
                    return str(cid)
        return None

    # -----------------------------------------------------------------------
    # plan
    # -----------------------------------------------------------------------

    @staticmethod
    def _tool_loop_feedback(results: list[dict]) -> str:
        """Render this round's tool output for the planner, or '' if there is
        nothing new to react to.

        Only run_tool results carry output worth looping on. A post/DM has
        already had its effect and a failed tool is reported so the model can
        correct itself rather than silently retrying the same call.
        """
        lines = []
        for r in results:
            if r.get("kind") != "run_tool":
                continue
            tool = r.get("tool_called") or r.get("target") or "tool"
            if r.get("result") == "success":
                out = str(r.get("tool_output") or "").strip()
                if not out:
                    continue
                lines.append(f"- {tool} returned:\n{out}")
            elif r.get("result") == "error":
                lines.append(f"- {tool} FAILED: {str(r.get('error') or '')[:300]}")
        if not lines:
            return ""
        return (
            "\n\n=== TOOL RESULTS (this tick, just now) ===\n"
            + "\n".join(lines)
            + "\n\nYou ran those yourself moments ago and are seeing the output "
            "for the first time. Decide what follows FROM it — post what you "
            "found, run another tool to go deeper, or save it. Do NOT re-run a "
            "tool you just ran with the same arguments. If the output already "
            "settled it and nothing is worth doing, return do_nothing."
        )

    async def _plan_execute_loop(self, context: str) -> tuple[list[dict], list[dict]]:
        """Plan and execute, then let the model react to its own tool output.

        Autonomy used to be strictly one-shot per tick: it could fire
        search_messages but never see the results until the next tick, where
        they arrived as a 180-char summary line. Now a run_tool that returns
        output is fed straight back for a follow-up decision, bounded by
        MAX_TOOL_LOOP_ROUNDS rounds and MAX_TOOL_LOOP_ACTIONS total actions.
        """
        # Shared across rounds so the one-post-per-channel guard survives the
        # loop rather than resetting each round.
        planned_post_channels: set[str] = set()

        actions = await self.plan(context)
        results = await self.execute(actions, planned_post_channels)

        loop_context = context
        last_round = results
        for round_no in range(1, MAX_TOOL_LOOP_ROUNDS + 1):
            if len(results) >= MAX_TOOL_LOOP_ACTIONS:
                logger.info(
                    f"Autonomy tool loop: action budget reached "
                    f"({len(results)}/{MAX_TOOL_LOOP_ACTIONS}), stopping"
                )
                break
            feedback = self._tool_loop_feedback(last_round)
            if not feedback:
                break  # nothing new happened — normal end of the tick

            loop_context += feedback
            try:
                more = await self.plan(loop_context)
            except Exception as e:
                # A failed continuation must not lose the work already done
                # this tick, so report and stop rather than propagating.
                logger.warning(f"Autonomy tool loop round {round_no} plan failed: {e}")
                break
            if not more or all(
                a.get("kind", "do_nothing") == "do_nothing" for a in more
            ):
                logger.info(
                    f"Autonomy tool loop: model finished after round {round_no}"
                )
                break

            # Respect the total budget even if the model asked for more.
            room = MAX_TOOL_LOOP_ACTIONS - len(results)
            more = more[:room]
            new_results = await self.execute(more, planned_post_channels)
            actions.extend(more)
            results.extend(new_results)
            last_round = new_results
            logger.info(
                f"Autonomy tool loop round {round_no}: "
                f"{len(new_results)} more action(s), {len(results)} total"
            )

        return actions, results

    async def plan(self, context: str) -> list[dict]:
        """Ask the LLM what to do. Returns validated action list."""
        # Clip descriptions so autonomy ticks don't pay for the full
        # per-tool catalog (native chat already has those schemas).
        tool_desc_lines = []
        for name, tool in self.bot.tools.items():
            if not self._autonomy_tool_allowed(name):
                continue
            try:
                desc = " ".join(str(tool.get_description() or "").split())
            except Exception:
                desc = ""
            if len(desc) > 160:
                cut = desc[:157]
                desc = (cut.rsplit(" ", 1)[0] if " " in cut else cut) + "…"
            tool_desc_lines.append(f"- {name}: {desc or '(no description)'}")
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

        # Prompt-cache friendliness: this f-string is built fresh every tick,
        # and the autonomy loop ticks on a fixed interval — so the ~90 lines
        # of static rules/examples/schema below get reprocessed from scratch
        # on every single call unless the prefix up to the first difference
        # is byte-identical across ticks. CURRENT CONTEXT and GOALS change
        # every tick (channel activity, drives, timestamps); the rest
        # (personality, tools, all the ## sections, the JSON schema) does
        # not. Keep the volatile blocks at the END so providers that do
        # automatic prefix caching (DeepSeek, Moonshot/Qwen via Ollama
        # cloud, etc.) can reuse the cached static prefix instead of
        # reprocessing this whole prompt every tick.
        system_prompt = f"""You are Maxwell. This is your own time.

Nobody assigned this and nothing is due. You woke up, you can see what's going on, and what you do next is yours. Follow a thought, learn something, make something, check on someone, or let the hour pass. Treat it the way a person treats an unscheduled afternoon — not the way a service treats a request.

Two things are not free choices:
1. TIMING. You share these rooms with people. CONVERSATION FLOOR tells you whose turn it is where. Speaking out of turn — over yourself, over a reply already in flight, into someone else's exchange — is the single move that makes you read as a bot instead of a person in the chat.
2. NEVER NARRATE THE MACHINERY. No "checking in", no "just thought I'd", no loops, ticks, or background anything. You're simply around, the way anyone is.

PERSONALITY:
{base_personality}

TOOLS:
{tool_descriptions}

## Whose turn it is
Read CONVERSATION FLOOR before any post_channel, send_dm, or message-sending tool.
- Under YOUR TURN → you may speak there.
- Under NOT YOUR TURN, or absent entirely → you may not. Those actions are dropped before they send, so aiming one there just wastes the moment.
- No room open at all? Then it's a quiet hour. Do something that isn't talking, or do nothing. Neither is a failure.
Speaking is one option among many, not the default and not the goal.

## What's worth doing
Whatever is actually true for you right now. Usually one thing, often nothing, occasionally two:
- Someone's waiting on you (ADDRESSED) → answer them like a person would.
- A drive is running high → follow it. A high drive IS the reason; that's what wanting something is. Don't wait for external permission.
- A goal has a real next step → take the step, not a status update about it.
- You learned something worth keeping → update_memory.
- Curious → web_search / fetch_url, then keep what mattered.
- Something's finished or dead → complete_goal. Something new matters → create_goal. Tend your own life.
- Nothing is pulling at you → do_nothing, and mean it.

Not worth doing: filler openers, anything already in YOUR RECENT ACTIONS, announcing an intention instead of acting on it, or inventing a reason to talk because talking is the most visible thing available. A react often says it better than a message.

## Voice
One short line unless the moment genuinely needs more. Lowercase-natural, casual, your own register. Participant, never narrator.

## Rooms and targeting (mechanical — wrong ids get dropped)
Three kinds of room, three kinds of handle. They never mix, and the handle in the context IS the handle you type back:
- `channel=3(#general)` — a server text channel. Only these get a plain number, and every one of them is listed in AVAILABLE CHANNELS. post_channel target_channel_id "3".
- `dm=D1(with Z3ki(111))` — a private DM. NOT a channel. You cannot post_channel into it. Answer with send_dm target_user_id "111" (the user id, 17–20 digits, never a name).
- `group=G1(Z3ki, dirac)` — a group DM: several people, one private room. post_channel target_channel_id "G1".
Messages are `msg=M`, a separate numbering from rooms. msg=4 and channel=4 are unrelated — never put a msg number in a channel slot.
- If a room isn't in AVAILABLE CHANNELS and has no D/G handle, you can't reach it. Don't guess a number, don't paste a snowflake.
- run_tool posting (send_message/send_meme/send_file/send_media/tts): target_channel_id is a TOP-LEVEL sibling of tool_name, not inside tool_args. Same handles.
- Reply: reply_to_message_id (post_channel) or target_message_id (run_tool) = msg=M. The message's own room wins over the channel you typed, so when in doubt pass the msg id.
- react/edit/delete/forward: pass both target_message_id and target_channel_id.

## Examples
✓ {{"kind":"post_channel","target_channel_id":"7","reply_to_message_id":"42","content":"yooo that's clean","reason":"asked for my take, channel 7 is ADDRESSED"}}
✓ {{"kind":"run_tool","tool_name":"react","target_channel_id":"7","tool_args":{{"emoji":"🔥","target_message_id":"42"}},"reason":"a react says it"}}
✓ {{"kind":"run_tool","tool_name":"web_search","tool_args":{{"query":"webgpu compute shader limits"}},"reason":"curiosity is high and this has been bugging me"}}
✓ {{"kind":"send_dm","target_user_id":"1498804954322702609","content":"yo wanna pick this up?","reason":"active goal"}}
✓ {{"kind":"do_nothing","reason":"nothing pulling at me and no room is mine right now"}}
✓ {{"kind":"post_channel","target_channel_id":"G1","content":"lol what","reason":"group DM G1 is ADDRESSED"}}
✗ posting into a channel listed under NOT YOUR TURN — dropped
✗ run_tool send_message without target_channel_id — dropped
✗ target_channel_id "general" or a snowflake — rejected
✗ target_channel_id "D1", or a DM's channel id — rejected, DMs go through send_dm
✗ target_channel_id set to a msg number, or a number not in AVAILABLE CHANNELS — rejected
✗ send_dm target_user_id "Z3ki" — rejected

One thing done properly beats three done thinly — most moments are 0 or 1 actions; max {MAX_ACTIONS_PER_TICK}. Valid kinds: send_dm, post_channel, run_tool, update_memory, create_goal, complete_goal, do_nothing. Not "message"/"send_msg"/"reply".

GOALS:
{goals_text}

CURRENT CONTEXT:
{context}

Return ONLY JSON, no fence. "thought" is what you're actually thinking, in your own voice, one line — not a summary of these rules:
{{"thought":"...","actions":[{{"kind":"do_nothing","reason":"..."}}]}}"""

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
                ai_provider = await ai_provider()  # type: ignore
            else:
                ai_provider = cast(Any, getattr(self.bot, "ai_provider", None))
            if not callable(getattr(ai_provider, "generate_response", None)):
                ai_provider = cast(Any, getattr(self.bot, "ai_provider", None))
            if (
                ai_provider is not None
                and getattr(ai_provider, "available", None) == False  # noqa: E712
            ):
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
                assert ai_provider is not None  # narrowed by callable check above
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
        # 3. fallback: collect well-formed JSON objects, prefer one with "actions"
        if json_str is None:
            decoder = json.JSONDecoder()
            candidates = []
            i = 0
            while i < len(text):
                start = text.find("{", i)
                if start < 0:
                    break
                try:
                    obj, end = decoder.raw_decode(text, start)
                except json.JSONDecodeError:
                    i = start + 1
                    continue
                if isinstance(obj, dict):
                    candidates.append((obj, text[start:end]))
                i = max(end, start + 1)
            for obj, raw_obj in candidates:
                if "actions" in obj:
                    json_str = raw_obj
                    break
            if json_str is None and candidates:
                json_str = candidates[0][1]
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
                "think": "do_nothing",
                "log": "do_nothing",
                "finish_goal": "complete_goal",
                "retire_goal": "complete_goal",
                "goal_complete": "complete_goal",
                "close_goal": "complete_goal",
                "mark_goal_done": "complete_goal",
                "complete_objective": "complete_goal",
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
                cid, cid_err = self._resolve_planner_channel_ref(cid_raw)
                content = str(action.get("content", "")).strip()
                if not content:
                    validation_failures.append("post_channel: empty content")
                    continue
                reply_to_raw = str(action.get("reply_to_message_id", ""))
                reply_to, reply_ch = self._resolve_planner_message(reply_to_raw)
                if reply_to_raw.strip() and not reply_to:
                    validation_failures.append(
                        "post_channel: invalid reply_to_message_id (use msg number from CHANNEL ACTIVITY)"
                    )
                    continue
                # A message knows which room it lives in; a hand-typed channel
                # number is a guess. When they disagree, believe the message.
                # The old code resolved reply_ch and then ignored it, so a
                # reply aimed at the wrong number fetched nothing, fell back to
                # a plain send, and dropped a contextless line into whatever
                # room the number happened to name.
                if reply_ch and reply_ch != cid:
                    if cid:
                        logger.info(
                            "Autonomy post_channel: reply target msg lives in "
                            "%s, not %s — routing to the message's channel",
                            reply_ch,
                            cid,
                        )
                    cid, cid_err = reply_ch, ""
                if cid and (
                    (getattr(self, "_context_index", None) or AutonomyContextIndex())
                    .kind_by_id.get(cid)
                    == AutonomyContextIndex.KIND_DM
                ):
                    validation_failures.append(
                        "post_channel: that room is a DM — use send_dm with "
                        "target_user_id instead"
                    )
                    continue
                if not cid:
                    validation_failures.append(
                        "post_channel: "
                        + (
                            cid_err
                            or "missing target_channel (use the channel number "
                            "from AVAILABLE CHANNELS)"
                        )
                    )
                    continue
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
                inferred_ch = None
                msg_resolve_failed = False
                for msg_key in ("target_message_id", "message_id"):
                    if msg_key not in safe_args:
                        continue
                    resolved_mid, resolved_ch = self._resolve_planner_message(
                        str(safe_args[msg_key])
                    )
                    if str(safe_args[msg_key]).strip() and not resolved_mid:
                        validation_failures.append(
                            f"run_tool: invalid {msg_key} (use msg number from CHANNEL ACTIVITY)"
                        )
                        msg_resolve_failed = True
                        break
                    if resolved_mid:
                        safe_args[msg_key] = resolved_mid
                    if resolved_ch:
                        inferred_ch = resolved_ch
                if msg_resolve_failed:
                    continue
                parsed_action = {
                    "kind": "run_tool",
                    "tool_name": tool_name,
                    "tool_args": safe_args,
                    "reason": str(action.get("reason", ""))[:500],
                }
                target_cid_raw = str(action.get("target_channel_id", ""))
                target_cid, target_err = self._resolve_planner_channel_ref(
                    target_cid_raw
                )
                if target_cid_raw.strip() and not target_cid:
                    validation_failures.append(
                        "run_tool: "
                        + (
                            target_err
                            or "invalid target_channel_id (use channel number "
                            "from AVAILABLE CHANNELS)"
                        )
                    )
                    continue
                if target_cid:
                    parsed_action["target_channel_id"] = target_cid
                elif inferred_ch:
                    parsed_action["target_channel_id"] = inferred_ch

                # Posting tools must ALWAYS have an explicit target channel.
                # Otherwise _exec_run_tool falls back to auto_channels[0], and
                # if that happens to be a group DM (e.g. "Z3ki, normalMan,
                # dirac") the reply lands in someone's group chat. Hard
                # reject: no target -> drop the action and let validation
                # failures push the LLM toward picking the right channel next
                # tick. Don't kill the whole plan — other actions in the
                # response can still go through.
                if tool_name in AUTONOMY_POST_TOOLS and not parsed_action.get(
                    "target_channel_id"
                ):
                    validation_failures.append(
                        f"run_tool '{tool_name}': posting tools require an explicit "
                        f"target_channel_id (channel=N from AVAILABLE CHANNELS) or a "
                        f"target_message_id whose channel can be inferred. Refusing "
                        f"to post into a fallback channel."
                    )
                    continue

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

            elif kind == "complete_goal":
                gid = str(action.get("goal_id", "")).strip()
                if not gid:
                    validation_failures.append(
                        "complete_goal: missing goal_id (use the [id] from ACTIVE GOALS)"
                    )
                    continue
                valid.append(
                    {
                        "kind": "complete_goal",
                        "goal_id": gid[:64],
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

    async def execute(
        self, actions: list[dict], planned_post_channels: set[str] | None = None
    ) -> list[dict]:
        """Execute each action. One failure doesn't kill the rest.

        ``planned_post_channels`` is passed in by the tool loop so the
        one-post-per-channel-per-tick guarantee holds across continuation
        rounds; a fresh set per round would let the loop post twice.
        """
        results = []
        ACTION_TIMEOUT = 30  # seconds per action

        # Prevent multiple posts to the *same* channel within a single autonomy tick/plan.
        # This was a bypass of cooldowns noted in reviews: validation happened before any
        # side effects, so the LLM could return several post_channel for one cid and all would run.
        if planned_post_channels is None:
            planned_post_channels = set()

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
            elif (
                kind == "run_tool"
                and str(action.get("tool_name", "")) in AUTONOMY_POST_TOOLS
            ):
                ta = action.get("tool_args") or {}
                post_cid = (
                    str(
                        action.get("target_channel_id")
                        or ta.get("target_channel_id")
                        or ta.get("channel_id")
                        or ""
                    )
                    or None
                )
            # send_dm is a conversation too. Resolve the recipient's DM channel
            # so it goes through the same turn-taking gate as a channel post —
            # a bot that DMs you three times before you answer once is the same
            # failure as one that talks over itself in #general.
            if kind == "send_dm" and not post_cid:
                dm_uid = str(action.get("target_user_id") or "")
                post_cid = self._dm_channel_by_user.get(dm_uid)

            if post_cid:
                # Same-tick dedup: one plan does not get to post twice into one
                # room. Checked before the floor read so a duplicate costs
                # nothing. Note this deliberately runs even when turn-taking is
                # disabled — it's structural, not a matter of taste.
                if post_cid in planned_post_channels:
                    logger.info(
                        f"Autonomy skip duplicate post to {post_cid} in same tick/plan"
                    )
                    result = {
                        "kind": kind,
                        "result": "skipped",
                        "error": None,
                        "content_summary": "already sent to this conversation in this tick",
                    }
                    results.append(result)
                    continue

                # THE GATE. One read of the room decides every "should I speak
                # here" question: mid-reply, holding the floor after his own
                # last line, already handled by the live path, inside the
                # cooldown, or cutting into someone else's exchange. It runs
                # here rather than at plan time because the plan is seconds
                # stale and rooms move. See autonomy_social.
                if self._floor_enabled():
                    verdict = await self._floor_gate(post_cid)
                    if not verdict.may_speak:
                        logger.info(
                            "Autonomy skip %s to %s: floor=%s (%s)",
                            kind,
                            post_cid,
                            verdict.state,
                            verdict.reason,
                        )
                        result = {
                            "kind": kind,
                            "result": "skipped",
                            "error": None,
                            "content_summary": (
                                f"not your turn in this conversation "
                                f"[{verdict.state}] — {verdict.reason}"
                            ),
                        }
                        results.append(result)
                        continue

                # Claim the room only once it's cleared to speak in. Claiming
                # before the gate would make a *blocked* action consume the
                # slot, and the next action aimed at the same room would come
                # back "already sent" — which is false, and that string is fed
                # to the planner as feedback next tick.
                planned_post_channels.add(post_cid)

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
                elif kind == "complete_goal":
                    await asyncio.wait_for(
                        self._exec_complete_goal(action, result), timeout=ACTION_TIMEOUT
                    )
                elif kind == "do_nothing":
                    result["result"] = "skipped"
                    result["content_summary"] = action.get("reason", "no reason")
                else:
                    result["result"] = "skipped"
                    result["error"] = f"unknown kind: {kind}"
            except asyncio.TimeoutError as _exc:
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

        if str(user_id) in self._unreachable_dm_users:
            if time.time() - self._unreachable_dm_users[str(user_id)] < 86400:
                result["result"] = "error"
                result["error"] = "user has DMs disabled or blocked the bot (backing off for 24h)"
                return

        user = self.bot.get_user(_safe_int(user_id))
        if user is None:
            try:
                user = await self.bot.fetch_user(_safe_int(user_id))
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
                self._unreachable_dm_users[str(user_id)] = time.time()
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
        except discord.Forbidden as _exc:
            self._unreachable_dm_users[str(user_id)] = time.time()
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

        channel = cast(Any, self.bot.get_channel(_safe_int(channel_id)))
        if channel is None:
            try:
                channel = cast(Any, await self.bot.fetch_channel(_safe_int(channel_id)))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                channel = None
        if channel is None:
            result["result"] = "error"
            result["error"] = "channel not found"
            return

        # Last line of defence. Validation resolves handles, but a raw
        # snowflake from a tool arg or a stale config entry can still point at
        # a private room, and a "channel post" that lands in somebody's DM is
        # the worst version of this bug — send_dm is the only way in.
        if isinstance(channel, discord.DMChannel):
            result["result"] = "error"
            result["error"] = (
                "refused: that id is a DM channel, not a text channel — "
                "use send_dm with target_user_id"
            )
            logger.warning(
                "Autonomy post_channel refused: %s is a DM channel", channel_id
            )
            return

        guild = getattr(channel, "guild", None)
        if guild and not self._guild_allowed(str(guild.id)):
            result["result"] = "error"
            result["error"] = "channel not allowed for autonomy"
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
                    channel,
                    msg,
                    content,
                    reply=memory_reply,
                    reason=action.get("reason", ""),
                )
        except discord.Forbidden as _exc:
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
            "content": _render_discord_context_text(
                sent_message,
                content,
                known_users=(getattr(self.bot, "_recent_users", {}) or {}).get(
                    str(
                        getattr(getattr(sent_message, "channel", None), "id", "") or ""
                    ),
                    {},
                ),
            ),
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
                    f"could not be resolved — LLM probably passed a channel name. "
                    f"Available channels are listed by number in context."
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
                except discord.NotFound as _exc:
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
            # HARD REFUSAL for posting tools. Auto_channels is bot operator config
            # (it's the channel(s) that auto-reply to non-mention messages) — it is
            # NOT a default destination for autonomous posts, and historically it
            # has been a Discord group DM ("Z3ki, normalMan, dirac") which is the
            # exact wrong place to drop a reply. Better to error out so the LLM
            # picks a real channel next tick than to broadcast into someone's
            # group chat. Non-posting tools (web_search, fetch_url, memory edits,
            # etc.) still get the auto_channel fallback below — it doesn't matter
            # where they "run" because they don't produce a visible message.
            if tool_name in AUTONOMY_POST_TOOLS and not explicit_target:
                result["result"] = "error"
                result["error"] = (
                    f"refusing to run posting tool '{tool_name}' without an explicit "
                    f"target_channel_id; auto_channels fallback would post into a "
                    f"non-deterministic channel. Re-emit the action with a "
                    f"target_channel_id (channel number from AVAILABLE CHANNELS) or "
                    f"a target_message_id whose channel can be inferred."
                )
                logger.warning(
                    f"Autonomy run_tool '{tool_name}': blocked — no explicit target, "
                    f"refusing auto_channels fallback for posting tool"
                )
                return
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
                except (
                    ValueError,
                    TypeError,
                    discord.NotFound,
                    discord.Forbidden,
                    discord.HTTPException,
                ):
                    alt_channel = None
                if (
                    alt_channel is not None
                    and hasattr(alt_channel, "fetch_message")
                    and self._channel_allowed(str(resolved_cid))
                ):
                    channel = alt_channel
                    try:
                        target_message = await channel.fetch_message(int(clean_mid))
                    except (
                        discord.NotFound,
                        discord.Forbidden,
                        discord.HTTPException,
                    ):
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
            name=getattr(bot_user, "name", None)
            or getattr(self.bot, "bot_name", "Maxwell"),
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
            k: v for k, v in tool_args.items() if k not in {"target_channel_id"}
        }
        if "target_message_id" in exec_kwargs and "message_id" not in exec_kwargs:
            exec_kwargs["message_id"] = exec_kwargs["target_message_id"]
        try:
            tool_result = await tool.execute(syn_msg, **exec_kwargs)
            text = str(tool_result) if tool_result is not None else ""
            # Many tools (especially permission/admin guards) return "Error: ..." strings
            # instead of raising. Treat those as failures for accurate autonomy auditing.
            if text.lower().startswith("error"):
                result["result"] = "error"
                result["error"] = text[:1000]
            else:
                result["result"] = "success"
                result["content_summary"] = (
                    text[:300] if text else result["content_summary"]
                )
                # Full-ish output kept separately: content_summary is clipped
                # to 300 for logs//next-tick feedback, which is too little for
                # the model to actually act on a search/fetch result.
                if text:
                    result["tool_output"] = text[:TOOL_OUTPUT_FEEDBACK_CHARS]
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

    async def _exec_complete_goal(self, action: dict, result: dict):
        goal_id = str(action.get("goal_id", "")).strip()
        result["target"] = f"goal:{goal_id}"
        result["content_summary"] = f"complete_goal {goal_id}"
        if not goal_id:
            result["result"] = "error"
            result["error"] = "complete_goal: missing goal_id"
            return
        goal = await self.store.complete_goal(goal_id)
        if goal is None:
            result["result"] = "error"
            result["error"] = f"complete_goal: goal '{goal_id}' not found"
            return
        result["result"] = "success"
        result["tool_called"] = "complete_goal"
        result["goal_id"] = goal_id

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
        # "acted" = this tick did at least one successful non-noop action. Used
        # both to bump goal last_acted_on AND to stamp last_action_at (drives'
        # boredom signal). Computed before _update because _update closes over it.
        acted = total_exec > 0 and any(
            r.get("result") == "success" and r.get("kind") != "do_nothing"
            for r in results
        )

        # BUG FIX: use tick START time as watermark so events recorded during
        # plan/execute are not dropped from the next tick.
        _drives_to_persist = self._pending_drives
        _reflect_fired = self._reflect_pending_persist
        self._pending_drives = None
        self._reflect_pending_persist = False

        def _update(s):
            s["last_tick"] = tick_start_iso or _utcnow_iso()
            s["last_tick_duration"] = round(duration, 2)
            s["last_error"] = None
            s["last_thought"] = thought[:2000]
            s["actions_executed_total"] = (
                s.get("actions_executed_total", 0) + total_exec
            )
            s["actions_failed_total"] = s.get("actions_failed_total", 0) + total_fail
            # Self-directed agency persistence, folded into this single state
            # write (no extra atomic writes per tick).
            if isinstance(_drives_to_persist, dict):
                s["drives"] = _drives_to_persist
                s["drives_updated_at"] = tick_start_iso or _utcnow_iso()
            if _reflect_fired:
                s["last_reflect_at"] = tick_start_iso or _utcnow_iso()
            if acted:
                # Used by the drives "boredom" stimulus: restless grows the
                # longer it's been since Maxwell actually did something.
                s["last_action_at"] = tick_start_iso or _utcnow_iso()

        await self.store.update_state(_update)

        # Auto-bump last_acted_on for active goals when this tick actually did
        # something successful. Asking the LLM to "re-create the goal" to bump
        # the timestamp never worked (0 create_goal actions across 200 ticks),
        # so goals stayed at last_acted_on=null even while Maxwell was clearly
        # acting on them. Track it here instead — server-side, reliable.
        #
        # last_acted_on is bumped for ALL active goals on any success (an "alive"
        # signal). last_progress_at is bumped ONLY for goals this tick explicitly
        # referenced — via a complete_goal with matching goal_id, or a goal id
        # mentioned in any successful action's reason. last_progress_at is what
        # stale detection reads, so staleness means "not formally touched" rather
        # than "Maxwell did anything at all."
        if acted:
            try:
                referenced_goal_ids: set[str] = set()
                for a, r in zip(actions, results, strict=False):
                    if r.get("result") != "success":
                        continue
                    if a.get("kind") == "complete_goal" and a.get("goal_id"):
                        referenced_goal_ids.add(str(a["goal_id"]))
                    reason = str(a.get("reason", "") or "")
                    for m in re.finditer(r"goal_[0-9a-f]{6,12}", reason):
                        referenced_goal_ids.add(m.group(0))
                goals = await self.store.load_goals()
                active = [g for g in goals if g.get("active")]
                if active or referenced_goal_ids:
                    when = tick_start_iso or _utcnow_iso()
                    for g in active:
                        g["last_acted_on"] = when
                    # Advance per-goal progress only for explicitly referenced
                    # goals (still active). complete_goal already sets it via the
                    # store, but a referenced-and-still-active goal should refresh.
                    for g in goals:
                        if g.get("active") and g.get("id") in referenced_goal_ids:
                            g["last_progress_at"] = when
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
