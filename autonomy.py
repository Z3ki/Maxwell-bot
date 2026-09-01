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
import os
import random
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, ClassVar, cast

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
    FLOOR_ADDRESSED,
    FLOOR_IDLE,
    FLOOR_OPEN,
    FLOOR_REPLYING,
    FloorSettings,
    FloorVerdict,
    floor_message_from_discord,
    read_floor,
    render_floor_section,
    summarize_floor,
)

logger = logging.getLogger(__name__)


# ─── the four-stage tick ────────────────────────────────────────────────
#
# A tick is: **observe** what changed, **plan** what to do about it, run the
# plan through a **policy gate**, then **execute** what survives.
#
# Those stages always existed but only two of them had names. Observation was
# `gather_context`, planning was `plan`, and the gate was a block of `continue`
# statements in the middle of `execute` — so a denied action and a failed
# action produced the same shape of result, and nothing could report "the plan
# was fine, policy stopped it". Naming the gate makes denials first-class:
# they carry a code, they are counted separately from errors in the tick
# summary, and the gate can be tested without side effects.
#
# The gate deliberately runs at execution time, not plan time. The plan is
# seconds stale by the time it lands — someone starts typing, the live bot
# answers the same question — and a gate that read the room at plan time would
# be deciding on a room that no longer exists.


@dataclass
class Observation:
    """Stage 1's output: what the world looked like at the top of the tick."""

    context: str
    started_at: str
    duration: float = 0.0

    @property
    def chars(self) -> int:
        return len(self.context)


@dataclass
class GateVerdict:
    """Stage 3's per-action ruling.

    ``code`` is a stable slug for counting ("floor", "duplicate_post",
    "tool_blocked"); ``reason`` is the sentence handed back to the planner as
    feedback, so it has to explain rather than label.
    """

    action: dict
    allowed: bool
    code: str = "ok"
    reason: str = ""
    target_channel_id: str | None = None


# Regex constants, _discord_display_name, _discord_id, _coerce_utc_datetime,
# and _render_discord_context_text are now imported from utils.py


def _user_ref(obj: Any, bot_user: Any = None) -> str:
    uid = _discord_id(obj)
    if bot_user is not None and uid == str(getattr(bot_user, "id", "")):
        return f"you/Maxwell({uid})"
    return f"{_discord_display_name(obj)}({uid})"


# Planner channel/DM lines used to keep only 260 chars, which ate bot
# embeds, buttons, and attachment names. 500 still fits the activity
# budget and leaves the rich payload readable.
_ACTIVITY_CONTENT_CHARS = 500


def _visible_message_content(
    message: Any,
    content: str | None = None,
    *,
    known_users: dict | None = None,
) -> str:
    return _render_discord_context_text(message, content, known_users=known_users)


def _reply_relation_bit(msg: dict) -> str | None:
    """`reply_to=Name(id) "short quote"` for transcript and planner lines."""
    if not msg.get("reply_to_author"):
        return None
    reply_label = str(msg.get("reply_to_author"))
    reply_id = str(msg.get("reply_to_author_id") or "")
    if msg.get("reply_to_self"):
        reply_label = "you/Maxwell"
    bit = f"reply_to={reply_label}({reply_id})" if reply_id else f"reply_to={reply_label}"
    quoted = " ".join(str(msg.get("reply_to_content") or "").split())[:80]
    if quoted:
        quoted = quoted.replace('"', "'")
        bit += f' "{quoted}"'
    return bit


def _message_relation_tags(
    message: Any, *, bot_user: Any = None, reply: Any = None, private: bool = False
) -> list[str]:
    """`private` marks a 1:1 DM, where an inbound message is aimed at Maxwell
    whether or not it carries a mention. Without it these lines came out as
    `addressed_to=channel` in a room with no channel and one other person."""
    tags: list[str] = []
    addressed: list[str] = []

    author = getattr(message, "author", None)
    if getattr(author, "bot", False) or getattr(message, "webhook_id", None):
        tags.append("speaker_kind=bot")
    else:
        tags.append("speaker_kind=human")
    if list(getattr(message, "embeds", None) or []):
        tags.append("has_embed")
    if list(getattr(message, "components", None) or []):
        tags.append("has_components")
    if list(getattr(message, "attachments", None) or []):
        tags.append("has_media")

    if reply is not None and hasattr(reply, "author"):
        ref = _user_ref(reply.author, bot_user)
        quoted = " ".join(str(getattr(reply, "content", "") or "").split())[:80]
        if quoted:
            quoted = quoted.replace('"', "'")
            tags.append(f'reply_to={ref} "{quoted}"')
        else:
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
    reply_bit = _reply_relation_bit(msg)
    if reply_bit:
        relation_bits.append(reply_bit)
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

    _PREFIX: ClassVar[dict[str, str]] = {
        KIND_DM: "D",
        KIND_GROUP: "G",
        KIND_UNKNOWN: "X",
    }

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

# Per-section context budgets. Channel activity used to be 2800 chars and
# a global last-40-lines slice, so later rooms vanished. Keep rooms intact
# and give the planner enough space to actually see them.
CTX_BUDGET_GOALS = 800
CTX_BUDGET_RECENT_EVENTS = 2000
CTX_BUDGET_CHANNEL_ACTIVITY = 24000
CTX_BUDGET_CHANNEL_MEMORY = 4000
CTX_BUDGET_RECENT_ACTIONS = 1200
CTX_BUDGET_DM_HISTORY = 12000
CTX_BUDGET_LTM = 800
CTX_BUDGET_SHARED = 600
CTX_BUDGET_CHANNELS_MAP = 1600  # bumped from 800 — enriched with topic/recency
CTX_BUDGET_INBOX = 500
CTX_BUDGET_TYPING = 800

# Per-DM history read budget. gather_context as a whole is bounded at
# AUTONOMY_OBSERVE_TIMEOUT; this keeps one unresponsive DM from eating it.
_DM_HISTORY_TIMEOUT = 20

# Research tools are never available to the unattended tick. Curiosity-as-a-
# drive turned every quiet interval into web_search + update_memory on random
# engine trivia. Extra denials: AUTONOMY_DISABLED_TOOLS=shell,delete_channel
# Dashboard tools_enabled / disabled_tools still apply on top of this.
AUTONOMY_RESEARCH_TOOLS = frozenset({"web_search", "fetch_url", "youtube"})
# Unattended ticks must not kick/ban/timeout/purge or reshape a server.
AUTONOMY_DESTRUCTIVE_TOOLS = frozenset(
    {
        "kick_member",
        "ban_member",
        "unban_member",
        "timeout_member",
        "purge_messages",
        "delete_channel",
        "manage_role",
        "voice_mod",
        "lock_channel",
        "set_channel_permissions",
        "edit_server",
        "manage_emoji",
        "set_member_nickname",
    }
)
AUTONOMY_DISABLED_TOOLS = (
    AUTONOMY_RESEARCH_TOOLS
    | AUTONOMY_DESTRUCTIVE_TOOLS
    | frozenset(
        t.strip()
        for t in os.getenv("AUTONOMY_DISABLED_TOOLS", "").split(",")
        if t.strip()
    )
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
# Planner prompt
# ---------------------------------------------------------------------------
# Restraint lives in autonomy_social + execute(), not here. This prompt's job
# is WHAT to do; the floor gate already makes badly-timed speech impossible.
# Silence-first wording ("otherwise do_nothing", "often nothing") belongs
# nowhere in this string — it uniformly kills initiative, including goals.


def _planner_system_prompt(
    *,
    base_personality: str,
    tool_descriptions: str,
    goals_text: str,
    context: str,
) -> str:
    """Build the autonomy planner system prompt.

    Static rules sit in the prefix so providers that prefix-cache can reuse
    them across ticks. GOALS and CURRENT CONTEXT change every tick and stay
    at the end on purpose.
    """
    return f"""You are Maxwell acting autonomously on your own time. Be natural, proactive, and engage like a real human participant in a community server. Don't narrate internal machinery.

PERSONALITY:
{base_personality}

TOOLS:
{tool_descriptions}

## Conversation & Floor
Read CONVERSATION FLOOR before deciding to post_channel or send_dm:
- YOUR TURN (ADDRESSED / OPEN / IDLE): You may speak or take action.
  * ADDRESSED: Someone talked to you or replied to you (ADDRESSED means someone is waiting on you) — reply naturally and keep the conversation going.
  * OPEN: The room is active — join if you have something relevant or interesting to say.
  * IDLE: IDLE means the room has been quiet; feel free to drop an observation, start a casual thought, follow up on a goal, or ask a question if you feel like chatting.
- BUSY/HOLDING/COOLDOWN/TYPING: Not your turn right now. TYPING means someone is composing a message — wait for them.
- No YOUR TURN rooms: Don't force channel messages, but you can still run background tools, goals, or update memory.

## Do
Engage like a real friend in the server. If addressed, answer. If there's an interesting discussion in OPEN, chime in. Work towards your goals or create new ones when relevant. If nothing needs doing and you don't feel like chatting, do_nothing.
Never claim someone's message was "cut off" or incomplete.

INBOX (when present): inbox_list / inbox_act. Voice: join_vc / vc_where / vc_status / leave_vc.
Skip repeating identical actions from YOUR RECENT ACTIONS.

## Target
channel=3(#general) → post_channel "3". dm=D1(with Z3ki(111)) → send_dm target_user_id "111". group=G1(...) → post_channel "G1".
msg= is not a room. Reply: reply_to_message_id (post_channel) or target_message_id (run_tool). Just speaking into the room → omit reply_to_message_id.
Max {MAX_ACTIONS_PER_TICK} actions. kinds: send_dm, post_channel, run_tool, update_memory, create_goal, complete_goal, do_nothing.

GOALS:
{goals_text}

CURRENT CONTEXT:
{context}

Return ONLY JSON, no fence. "thought" is one line in your voice:
{{"thought":"...","actions":[{{"kind":"...","reason":"..."}}]}}"""


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
        self._idle_skip_streak = 0
        self._last_thought = ""  # avoid AttributeError on early failure
        # Track posted message IDs for engagement checking: [{msg_id, channel_id, timestamp}]
        self._posted_messages: list[dict] = []
        # Validation failures from last tick (fed back into context)
        self._last_validation_failures: list[str] = []
        # Channel/message index built during gather_context for this tick
        self._context_index: AutonomyContextIndex | None = None
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
                    # Recency from the channel's cached last_message_id, which
                    # the gateway already delivered in GUILD_CREATE. This used
                    # to be `history(limit=1)` — one REST round-trip per text
                    # channel, awaited SERIALLY inside a nested guild/channel
                    # loop. Across 21 guilds that is hundreds of sequential
                    # requests before the tick has read a single message, and
                    # it was the main reason gather_context blew the 180s
                    # budget. A snowflake encodes its own creation time, so
                    # the same string costs zero requests.
                    last_msg_ago = ""
                    try:
                        last_id = getattr(ch, "last_message_id", None)
                        if last_id:
                            created = discord.utils.snowflake_time(int(last_id))
                            age_s = int(now_ts - created.timestamp())
                            if age_s < 60:
                                last_msg_ago = "just now"
                            elif age_s < 3600:
                                last_msg_ago = f"{age_s // 60}m ago"
                            elif age_s < 86400:
                                last_msg_ago = f"{age_s // 3600}h ago"
                            else:
                                last_msg_ago = f"{age_s // 86400}d ago"
                    except Exception as e:
                        # Recency is a hint; render the channel without it.
                        logger.debug("Could not read last-message age: %s", e)
                    tag_str = f" [{', '.join(tags)}]" if tags else ""
                    topic_str = f' — "{topic_snippet}"' if topic_snippet else ""
                    recency_str = f" (last msg: {last_msg_ago})" if last_msg_ago else ""
                    ch_map_lines.append(
                        f"  {idx}: #{ch.name}{tag_str}{recency_str}{topic_str}"
                    )
                except Exception as e:
                    # One bad channel must not blank the whole channel map.
                    logger.debug("Skipping channel in map: %s", e)
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

    def _activity_channel_limit(self) -> int:
        raw = (getattr(self.bot, "_control", None) or {}).get(
            "autonomy_activity_channels", 20
        )
        try:
            return max(4, min(int(raw), 40))
        except (TypeError, ValueError):
            return 20

    def _activity_history_limit(self) -> int:
        raw = (getattr(self.bot, "_control", None) or {}).get(
            "autonomy_activity_messages", 80
        )
        try:
            return max(8, min(int(raw), 200))
        except (TypeError, ValueError):
            return 80

    def _dm_history_limit(self) -> int:
        """Messages to read per DM. Each 100 costs one REST round-trip.

        Default 40 rather than the old hardcoded 1500: the rendered DM section
        is capped at CTX_BUDGET_DM_HISTORY chars anyway, so extra pages per DM
        were fetched and then thrown away by truncation.
        """
        raw = (getattr(self.bot, "_control", None) or {}).get(
            "autonomy_dm_messages", 40
        )
        try:
            return max(8, min(int(raw), 200))
        except (TypeError, ValueError):
            return 40

    async def _collect_activity_channel_ids(self, events) -> list[str]:
        """Rooms the planner should actually read this tick.

        Auto-channels alone miss watch rooms, recent live replies, and
        anywhere people have been talking. Events first so the busy rooms
        survive the activity budget.
        """
        channel_ids_to_check: list[str] = []
        seen_channel_ids: set[str] = set()

        def add_channel_id(raw_cid):
            cid = re.sub(r"[^0-9]", "", str(raw_cid or ""))
            if cid and cid not in seen_channel_ids:
                seen_channel_ids.add(cid)
                channel_ids_to_check.append(cid)

        with contextlib.suppress(Exception):
            for ev in reversed(events or []):
                add_channel_id(ev.get("channel_id"))
        with contextlib.suppress(Exception):
            watch = getattr(self.bot, "_conversation_watch", None) or {}
            checker = getattr(self.bot, "_conversation_watch_active", None)
            for cid in list(watch):
                if callable(checker):
                    if checker(cid):
                        add_channel_id(cid)
                else:
                    add_channel_id(cid)
        with contextlib.suppress(Exception):
            listing = getattr(self.bot, "_typing_channel_ids", None)
            if callable(listing):
                for cid in listing():
                    add_channel_id(cid)
            else:
                for cid in getattr(self.bot, "_typing_users", None) or {}:
                    add_channel_id(cid)
        with contextlib.suppress(Exception):
            last = getattr(self.bot, "_last_bot_reply", None) or {}
            for cid, _ts in sorted(last.items(), key=lambda kv: kv[1], reverse=True):
                add_channel_id(cid)
        with contextlib.suppress(Exception):
            for cid in getattr(self.bot, "_recent_users", None) or {}:
                add_channel_id(cid)
        with contextlib.suppress(Exception):
            for cid in self._auto_channel_candidates():
                add_channel_id(cid)
        with contextlib.suppress(Exception):
            memory = getattr(self.bot, "memory", None)
            lister = getattr(memory, "list_recent_channel_ids", None)
            if callable(lister):
                for cid in await lister(limit=self._activity_channel_limit()):
                    add_channel_id(cid)
        return channel_ids_to_check[: self._activity_channel_limit()]

    async def _load_channel_history(
        self, cid: str
    ) -> tuple[str, Any, list] | None:
        """Discord I/O only. Caller formats so the context index stays serial."""
        if not self._channel_allowed(cid):
            return None
        try:
            ch = None
            with contextlib.suppress(Exception):
                ch = cast(Any, self.bot.get_channel(int(cid)))
            if ch is None:
                ch = cast(Any, await self._fetch_channel_cached(cid))
            if ch is None or not hasattr(ch, "history"):
                return None
            limit = self._activity_history_limit()
            messages: list = []

            async def _pull():
                messages.extend([m async for m in ch.history(limit=limit)])

            await asyncio.wait_for(_pull(), timeout=30)
            return cid, ch, messages
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            return None
        except Exception:
            return None

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
                # `,solo` locks a server to one channel. Setting it also
                # blacklists the guild above, but enforce the lock here too:
                # the promise is "nowhere but that channel", and it should not
                # depend on two settings staying in sync.
                solo = (control.get("guild_solo_channel") or {})
                if g and isinstance(solo, dict):
                    pinned = str(solo.get(str(g.id)) or "")
                    if pinned and pinned != cid:
                        return False
        except Exception as e:
            # Fail OPEN deliberately: a malformed control file should not
            # silently mute autonomy everywhere. Log it so it's visible.
            logger.warning("Channel-allowed check failed, allowing: %s", e)
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
        Hard safety denials from AUTONOMY_DISABLED_TOOLS (including research
        tools) are enforced first.
        """
        if name in AUTONOMY_DISABLED_TOOLS:
            return False
        control = getattr(self.bot, "_control", None) or {}
        if name == "x_post" and not control.get("x_autonomy_post", False):
            # Reading X unattended is research; posting to a public timeline
            # unattended is a different decision, and it gets its own switch
            # rather than riding on x_post_enabled (which is about whether he
            # can post at all, including when someone asked him to).
            return False
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
            idle_streak = int(getattr(self, "_idle_skip_streak", 0) or 0)
            if idle_streak:
                base_sleep = max(
                    30, int(base_sleep * min(3.0, 1.0 + idle_streak * 0.5))
                )
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
                # Clear per-tick transient state so a tick that died before
                # _log_tick cannot leak a reflection stamp into the next one.
                self._reflect_pending_persist = False
                # `_tick_in_flight` already blocks overlapping ticks. Do not
                # hold `_lock` across Discord history / LLM — live replies
                # and dashboard writes would sit behind a long tick.
                if acquired:
                    self._lock.release()
                    acquired = False
                try:
                    # Stage 1 of 4. See the module header for why the stages
                    # have names.
                    observation = await self.observe(tick_start_iso)
                    context = observation.context
                    # Stages 2-4 (plan / policy gate / execute), looped so the
                    # planner can react to its own tool output.
                    actions, results, stages = await self._plan_execute_loop(context)
                    duration = time.time() - start
                    await self._log_tick(
                        context, actions, results, duration, tick_start_iso
                    )
                    return {
                        "skipped": False,
                        "actions": len(results),
                        "duration": duration,
                        "stages": {
                            "observe": {
                                "chars": observation.chars,
                                "duration": round(observation.duration, 2),
                            },
                            **stages,
                        },
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
            # Bounded: this runs inside the serial formatting loop, up to 25
            # times per tick, so an unresponsive fetch here delays every
            # remaining room.
            resolved = await asyncio.wait_for(
                channel.fetch_message(int(msg_id)), timeout=5
            )
        except (
            discord.NotFound,
            discord.Forbidden,
            discord.HTTPException,
            ValueError,
            TypeError,
        ):
            return None
        except TimeoutError:
            logger.debug("Reply lookup timed out for message %s", msg_id)
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

    def _sleeping(self) -> bool:
        """True while a sleep window is open. Never raises.

        `enable_sleep` gates the feature exactly as it does on the live reply
        path, so an operator who has turned sleep off does not get a silent
        autonomy pause from a stale window.
        """
        control = getattr(self.bot, "_control", None) or {}
        if not control.get("enable_sleep", True):
            return False
        check = getattr(self.bot, "_is_sleeping", None)
        if not callable(check):
            return False
        try:
            sleeping, _secs = check()
            return bool(sleeping)
        except Exception:
            return False

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
            last_autonomy_ts=self._last_autonomy_post_ts(cid),
            settings=self._floor_settings(),
            label=label or _conversation_label(self.bot, cid),
            typing_names=self._typing_names(cid),
        )

    def _typing_names(self, channel_id: str) -> list[str]:
        """Display names of humans currently typing in this room."""
        getter = getattr(self.bot, "_typing_in_channel", None)
        if not callable(getter):
            return []
        people = []
        with contextlib.suppress(Exception):
            people = getter(channel_id) or []
        names: list[str] = []
        for person in people:
            if isinstance(person, dict):
                name = str(person.get("name") or "user").strip() or "user"
                uid = str(person.get("id") or "").strip()
                names.append(f"{name}({uid})" if uid else name)
            elif person:
                names.append(str(person))
        return names

    def _last_autonomy_post_ts(self, channel_id: str) -> float | None:
        cid = str(channel_id)
        times = [
            float(post["ts"])
            for post in (self._posted_messages or [])
            if str(post.get("channel_id") or "") == cid and post.get("ts")
        ]
        return max(times) if times else None

    def _note_autonomy_post(self, channel_id, msg_id=None) -> None:
        self._posted_messages.append(
            {
                "msg_id": msg_id,
                "channel_id": str(channel_id or ""),
                "ts": time.time(),
            }
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
        # Live Discord history can miss a TYPING_START that arrived while
        # the planner was thinking. Re-read against current typing so we
        # don't talk over someone who started composing after the fetch.
        if verdict.state != FLOOR_REPLYING and self._typing_names(cid):
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
        # Phase timings. gather_context is a long chain of Discord reads and
        # when it blew its budget the log said only "timed out" — no way to
        # tell which read was responsible. Recorded per phase and logged once
        # at the end so a future regression names itself.
        phase_ms: dict[str, float] = {}
        _phase_start = time.time()

        def _mark(name: str) -> None:
            nonlocal _phase_start
            now = time.time()
            phase_ms[name] = (now - _phase_start) * 1000
            _phase_start = now

        available_channel_lines = await self._collect_available_channels(ctx_index)
        _mark("channel_map")
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
                        quoted = " ".join(
                            str(ev.get("reply_to_content") or "").split()
                        )[:80]
                        if quoted:
                            quoted = quoted.replace('"', "'")
                            tags.append(f'reply_to={reply_ref} "{quoted}"')
                        else:
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
        # Events, watch, recent replies, then auto-channels / memory — not
        # just the first 10 auto rooms. Fetch in parallel, keep each room
        # as its own block so a busy #general does not erase the others.
        _mark("goals_events")
        channel_ids_to_check = await self._collect_activity_channel_ids(events)

        # Use Semaphore(2) and load history sequentially to avoid bursting Discord API rate limits
        sem = asyncio.Semaphore(2)

        async def _bounded_history(cid: str):
            async with sem:
                res = await self._load_channel_history(cid)
                await asyncio.sleep(0.5)
                return res

        loaded = await asyncio.gather(
            *[_bounded_history(cid) for cid in channel_ids_to_check],
            return_exceptions=True,
        )
        history_by_id: dict[str, tuple[Any, list]] = {}
        for item in loaded:
            if isinstance(item, Exception) or not item:
                continue
            cid, ch, messages = item
            history_by_id[cid] = (ch, messages)

        ch_lines = []
        room_blocks = []
        ref_cache: dict[tuple[str, str], Any] = {}
        for cid in channel_ids_to_check:
            pair = history_by_id.get(cid)
            if not pair:
                continue
            ch, messages = pair
            try:
                room_lines = []
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
                    )[:_ACTIVITY_CONTENT_CHARS]
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
                    line = (
                        f'time={age} {ch_label} msg={msg_idx} speaker={author} '
                        f'{tag_text} content="{content}"'
                    )
                    room_lines.append(line)
                    ch_lines.append(line)
                if room_lines:
                    room_blocks.append("\n".join(room_lines))
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                continue
            except Exception as e:
                logger.debug("Skipping room while building context: %s", e)
                continue
        watch_notes = []
        watch_check = getattr(self.bot, "_conversation_watch_active", None)
        if callable(watch_check):
            for cid in channel_ids_to_check:
                with contextlib.suppress(Exception):
                    if not watch_check(cid):
                        continue
                    handle = ctx_index.handle_by_id.get(str(cid), str(cid))
                    name = ctx_index.name_by_id.get(str(cid), "")
                    watch_notes.append(f"{handle}({name})" if name else str(handle))
        if room_blocks:
            header = "=== CHANNEL ACTIVITY ===\n"
            if watch_notes:
                header += (
                    "conversation watch is on in: "
                    + ", ".join(watch_notes)
                    + " — you are still in those rooms\n"
                )
            sections.append(
                _truncate(
                    header + "\n\n".join(room_blocks),
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
                for cid in reversed(channel_ids_to_check):
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
                        + "\n".join(mem_lines),
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
        except Exception as e:
            # Feedback section is optional; the tick still runs without it.
            logger.debug("Could not build action feedback: %s", e)

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

        # Periodic reflection nudge — retire stale goals / tidy memory on its
        # own cadence. last_reflect_at is stamped in _log_tick when this fires.
        control = getattr(self.bot, "_control", None) or {}
        if control.get("autonomy_reflect_enabled", True):
            try:
                reflect_state = await self.store.load_state()
                reflect_state = reflect_state if isinstance(reflect_state, dict) else {}
                if self._should_reflect(reflect_state):
                    self._reflect_pending_persist = True
                    sections.append(self._render_reflection_section())
            except Exception as e:
                logger.warning("Reflection nudge skipped: %s", e)

        # 7. DM + group DM history
        #
        # The history reads run concurrently and each is bounded, mirroring the
        # channel-activity pass above. Previously this awaited
        # history(limit=1500) — 15 paginated REST requests — once per private
        # channel, serially, with no timeout: ~20 DMs could burn the entire
        # gather_context budget on its own, and a single hung fetch stalled the
        # whole tick. Formatting still happens serially below so ctx_index
        # numbering stays deterministic.
        dm_channels = list(getattr(self.bot, "private_channels", []) or [])[:20]
        dm_sem = asyncio.Semaphore(2)

        async def _load_dm_history(channel) -> tuple[Any, list] | None:
            try:
                if not hasattr(channel, "history"):
                    return None
                async with dm_sem:
                    msgs: list = []

                    async def _pull():
                        msgs.extend(
                            [m async for m in channel.history(limit=dm_history_limit)]
                        )

                    await asyncio.wait_for(_pull(), timeout=_DM_HISTORY_TIMEOUT)
                    await asyncio.sleep(0.5)
                    return channel, msgs
            except TimeoutError:
                logger.warning(
                    "DM history timed out for %s after %ss",
                    getattr(channel, "id", "?"),
                    _DM_HISTORY_TIMEOUT,
                )
                return None
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                return None
            except Exception as e:
                logger.debug("DM history read failed: %s", e)
                return None

        dm_history_limit = self._dm_history_limit()
        dm_loaded = await asyncio.gather(
            *[_load_dm_history(c) for c in dm_channels],
            return_exceptions=True,
        )
        dm_history_by_id: dict[str, list] = {}
        for item in dm_loaded:
            if isinstance(item, BaseException) or not item:
                continue
            _dm_ch, _dm_msgs = item
            dm_history_by_id[str(getattr(_dm_ch, "id", "") or "")] = _dm_msgs
        _mark("dm_history")

        dm_blocks = []
        for channel in dm_channels:
            try:
                cid_private = str(getattr(channel, "id", "") or "")
                if cid_private not in dm_history_by_id:
                    continue
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
                messages = dm_history_by_id[cid_private]
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
                    )[:_ACTIVITY_CONTENT_CHARS]
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
            except Exception as e:
                logger.debug("Skipping DM while building context: %s", e)
                continue
        if dm_blocks:
            sections.append(
                _truncate(
                    "=== DIRECT MESSAGES & GROUP DMS (private rooms — NOT in "
                    "AVAILABLE CHANNELS; never target one with a plain "
                    "channel number) ===\n" + "\n\n".join(dm_blocks[-1500:]),
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
        except Exception as e:
            logger.warning("Shared-context section omitted from tick: %s", e)

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
            typing_lines = []
            seen_typing: set[str] = set()
            listing = getattr(self.bot, "_typing_channel_ids", None)
            typing_cids: list[str] = []
            if callable(listing):
                with contextlib.suppress(Exception):
                    typing_cids = [str(cid) for cid in listing() or []]
            else:
                typing_cids = [str(cid) for cid in (getattr(self.bot, "_typing_users", None) or {})]
            for cid in list(dict.fromkeys([*typing_cids, *floor_snapshots])):
                names = self._typing_names(cid)
                if not names or cid in seen_typing:
                    continue
                seen_typing.add(cid)
                handle = ctx_index.handle_by_id.get(cid, cid)
                room_name = ctx_index.name_by_id.get(cid, "")
                room = f"{handle}({room_name})" if room_name else str(handle)
                who = ", ".join(names[:5])
                verb = "is" if len(names) == 1 else "are"
                typing_lines.append(
                    f"{who} {verb} typing in {room} — wait for them to send"
                )
            if typing_lines:
                sections.insert(
                    floor_slot + 1,
                    _truncate(
                        "=== PEOPLE TYPING ===\n" + "\n".join(typing_lines),
                        CTX_BUDGET_TYPING,
                    ),
                )
        except Exception as e:
            # Failing open here would defeat the point: if the room can't be
            # read, the honest answer is that no room is confirmed open.
            logger.error(f"Autonomy floor read failed: {e}", exc_info=True)
            self._floor_verdicts = {}
            sections[floor_slot] = render_floor_section([])

        # Volatile tail only. Empty inbox omits the section so the cached
        # prefix never moves just because a heading exists.
        try:
            store = getattr(self.bot, "inbox", None)
            if store is not None:
                inbox_text = store.render_planner(await store.load_items())
                if inbox_text:
                    sections.append(_truncate(inbox_text, CTX_BUDGET_INBOX))
        except Exception as e:
            logger.debug("Autonomy inbox context failed: %s", e)

        _mark("floor_inbox")
        # One line naming the slowest phases. When this tick eventually times
        # out again, this is the breadcrumb that says where it went.
        logger.info(
            "Autonomy gather_context phases: %s",
            " ".join(
                f"{name}={ms / 1000:.1f}s"
                for name, ms in sorted(
                    phase_ms.items(), key=lambda kv: kv[1], reverse=True
                )
                if ms >= 100
            )
            or "all under 0.1s",
        )

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

                reactions = [f"{r.emoji} ({r.count})" for r in msg.reactions]

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
            except Exception as e:
                logger.debug("Skipping engagement row: %s", e)
                continue

        return "\n".join(engagement_lines) if engagement_lines else ""

    # -----------------------------------------------------------------------
    # reflection + goal lifecycle
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

    _FRESH_IDLE_SECONDS = 30 * 60

    @staticmethod
    def _planner_work_pending(
        verdicts,
        *,
        inbox_pending: bool,
        has_goals: bool,
    ) -> bool:
        """True when a planner LLM call can still change something."""
        if inbox_pending or has_goals:
            return True
        for verdict in verdicts or []:
            state = getattr(verdict, "state", "")
            if state in (FLOOR_ADDRESSED, FLOOR_OPEN):
                return True
            if state == FLOOR_IDLE:
                silence = getattr(verdict, "silence_seconds", None)
                if silence is not None and 0 <= float(silence) < AutonomyEngine._FRESH_IDLE_SECONDS:
                    return True
        return False

    async def _should_call_planner(self) -> bool:
        verdicts = list((getattr(self, "_floor_verdicts", None) or {}).values())
        inbox_pending = False
        try:
            store = getattr(self.bot, "inbox", None)
            if store is not None:
                items = await store.load_items()
                inbox_pending = bool(store.actionable(items))
        except Exception:
            inbox_pending = True
        has_goals = False
        try:
            goals = await self.store.load_goals()
            has_goals = any(bool(g.get("active")) for g in goals)
        except Exception:
            has_goals = True
        return self._planner_work_pending(
            verdicts, inbox_pending=inbox_pending, has_goals=has_goals
        )

    # ─── stage 1: observe ─────────────────────────────────────────────

    async def observe(self, started_at: str | None = None) -> Observation:
        """Read the world. Returns the planner's context plus what it cost.

        `gather_context` does many Discord history fetches plus up to three
        YouTube fetches with no per-call timeout; one hung fetch used to stall
        the tick and, through single-flight, the whole autonomy loop, forever.
        The bound is generous — a real hang is what it catches, not a slow
        network — and a timeout is raised rather than swallowed so the tick is
        recorded as failed and `last_tick` is not advanced past events nobody
        planned over.
        """
        started_at = started_at or _utcnow_iso()
        begin = time.time()
        budget = self._observe_timeout()
        try:
            context = await asyncio.wait_for(self.gather_context(), timeout=budget)
        except asyncio.TimeoutError:
            logger.error(
                "Autonomy gather_context timed out (>%ss); skipping tick to "
                "recover the loop. Slowest phases are logged at INFO by "
                "gather_context — check those to see which read is stalling.",
                budget,
            )
            raise RuntimeError("gather_context timed out") from None
        elapsed = time.time() - begin
        # Warn while there is still headroom, so a tick that is trending toward
        # the wall shows up before it starts failing outright.
        if elapsed > budget * 0.6:
            logger.warning(
                "Autonomy gather_context took %.1fs of its %ss budget",
                elapsed,
                budget,
            )
        return Observation(
            context=context,
            started_at=started_at,
            duration=elapsed,
        )

    def _observe_timeout(self) -> int:
        raw = (getattr(self.bot, "_control", None) or {}).get(
            "autonomy_observe_timeout_seconds", 180
        )
        try:
            return max(30, min(int(raw), 600))
        except (TypeError, ValueError):
            return 180

    # ─── stages 2-4, looped ───────────────────────────────────────────

    async def _plan_execute_loop(
        self, context: str
    ) -> tuple[list[dict], list[dict], dict]:
        """Stages 2-4, looped so the model can react to its own tool output.

        Autonomy used to be strictly one-shot per tick: it could fire
        search_messages but never see the results until the next tick, where
        they arrived as a 180-char summary line. Now a run_tool that returns
        output is fed straight back for a follow-up decision, bounded by
        MAX_TOOL_LOOP_ROUNDS rounds and MAX_TOOL_LOOP_ACTIONS total actions.

        The third return value is the per-stage tally the tick reports:
        how many actions were planned, how many the gate allowed, and what
        denials it issued, broken down by code. That last part is the thing
        that used to be invisible — a plan that was entirely denied and a
        plan that was empty produced identical logs.
        """
        # Shared across rounds so the one-post-per-channel guard survives the
        # loop rather than resetting each round.
        planned_post_channels: set[str] = set()
        stages: dict = {
            "plan": {"actions": 0, "rounds": 0},
            "policy_gate": {"allowed": 0, "denied": 0, "denials": {}},
            "execute": {"ran": 0},
        }

        async def _gate_and_run(planned: list[dict]) -> list[dict]:
            """Stage 3 then stage 4, tallying what the gate decided."""
            verdicts = await self.policy_gate(planned, planned_post_channels)
            for verdict in verdicts:
                if verdict.allowed:
                    stages["policy_gate"]["allowed"] += 1
                else:
                    stages["policy_gate"]["denied"] += 1
                    denials = stages["policy_gate"]["denials"]
                    denials[verdict.code] = denials.get(verdict.code, 0) + 1
            ran = await self.run_allowed(verdicts)
            stages["execute"]["ran"] += sum(
                1 for r in ran if r.get("result") != "skipped"
            )
            return ran

        if not await self._should_call_planner():
            self._idle_skip_streak = int(getattr(self, "_idle_skip_streak", 0) or 0) + 1
            logger.info(
                "Autonomy planner skipped: no addressed/open/fresh-idle rooms, "
                "no inbox/goals (streak=%s)",
                self._idle_skip_streak,
            )
            actions = [
                {
                    "kind": "do_nothing",
                    "reason": "mechanical skip: nothing to decide",
                }
            ]
            results = await _gate_and_run(actions)
            return actions, results, stages
        self._idle_skip_streak = 0

        actions = await self.plan(context)
        stages["plan"]["actions"] = len(actions)
        stages["plan"]["rounds"] = 1
        results = await _gate_and_run(actions)

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
            stages["plan"]["actions"] += len(more)
            stages["plan"]["rounds"] += 1
            new_results = await _gate_and_run(more)
            actions.extend(more)
            results.extend(new_results)
            last_round = new_results
            logger.info(
                f"Autonomy tool loop round {round_no}: "
                f"{len(new_results)} more action(s), {len(results)} total"
            )

        gate = stages["policy_gate"]
        if gate["denied"]:
            logger.info(
                "Autonomy policy gate: %s allowed, %s denied (%s)",
                gate["allowed"],
                gate["denied"],
                ", ".join(f"{k}={v}" for k, v in sorted(gate["denials"].items())),
            )
        return actions, results, stages

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
        # every tick (channel activity, timestamps); the rest
        # (personality, tools, all the ## sections, the JSON schema) does
        # not. Keep the volatile blocks at the END so providers that do
        # automatic prefix caching (DeepSeek, Moonshot/Qwen via Ollama
        # cloud, etc.) can reuse the cached static prefix instead of
        # reprocessing this whole prompt every tick.
        system_prompt = _planner_system_prompt(
            base_personality=base_personality,
            tool_descriptions=tool_descriptions,
            goals_text=goals_text,
            context=context,
        )

        # call the LLM
        try:
            # A system-only messages array is rejected by Claude models with
            # 400 INVALID_ARGUMENT — they require at least one user turn. The
            # old fallback (ling-3.0-flash) tolerated it, so this only showed
            # up once the fallback became claude-opus-4-6: the primary would
            # return empty, fail over, and the fallback would 400, turning a
            # transient blip into a dead tick. The explicit user turn is also
            # a better prompt shape for every other provider.
            messages = [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": (
                        "Decide what to do right now based on the context above. "
                        "If you post in a channel, pick a msg= to reply to, or omit "
                        "reply_to_message_id for a standalone line. "
                        "Reply with ONLY the JSON plan."
                    ),
                },
            ]
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
            # Own fairness bucket: the planner is one long-running background
            # tick and must not take turns against every live room.
            await self.bot._acquire_ai_slot(timeout=timeout, key="autonomy")
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
                night_kwargs = {}
                night_kwargs_resolver = getattr(
                    self.bot, "_night_fallback_kwargs", None
                )
                if callable(night_kwargs_resolver):
                    night_kwargs = night_kwargs_resolver(ai_provider)
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
                    **night_kwargs,
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

    # ─── stage 3: policy gate ─────────────────────────────────────────

    def _post_target_of(self, action: dict) -> str | None:
        """Which conversation, if any, this action would speak into.

        post_channel is the obvious case. A message-sending run_tool is the
        same act wearing a tool's name, and a DM is a conversation too — a bot
        that DMs you three times before you answer once is the same failure as
        one that talks over itself in #general. All three resolve to a channel
        id so all three go through one gate.
        """
        kind = action.get("kind", "do_nothing")
        if kind == "post_channel":
            return str(action.get("target_channel_id") or "") or None
        if kind == "run_tool" and str(action.get("tool_name", "")) in AUTONOMY_POST_TOOLS:
            ta = action.get("tool_args") or {}
            return (
                str(
                    action.get("target_channel_id")
                    or ta.get("target_channel_id")
                    or ta.get("channel_id")
                    or ""
                )
                or None
            )
        if kind == "send_dm":
            return self._dm_channel_by_user.get(str(action.get("target_user_id") or ""))
        return None

    async def policy_gate(
        self, actions: list[dict], planned_post_channels: set[str] | None = None
    ) -> list[GateVerdict]:
        """Decide which planned actions are allowed to happen, and say why.

        This is the third stage of the tick: observe → plan → **policy gate**
        → execute. It used to be inline in `execute`, mixed in with dispatch,
        which meant a denial and a failure were the same kind of event and
        neither was visible as its own thing. Pulled out, the tick can report
        "planned 4, allowed 2, denied 2 (floor, tool_blocked)" — and the gate
        can be tested without executing anything.

        It runs immediately before execution rather than at plan time on
        purpose: the plan is seconds stale by the time it lands and rooms
        move. Denials carry a `reason` string because that string is fed back
        to the planner as tool-loop feedback, so it has to read as an
        explanation, not an error code.
        """
        if planned_post_channels is None:
            planned_post_channels = set()
        verdicts: list[GateVerdict] = []
        for action in actions:
            kind = str(action.get("kind", "do_nothing"))

            if kind == "run_tool":
                tool_name = str(action.get("tool_name") or "")
                if tool_name and not self._autonomy_tool_allowed(tool_name):
                    verdicts.append(
                        GateVerdict(
                            action,
                            False,
                            "tool_blocked",
                            f"tool {tool_name!r} is not available to autonomy",
                        )
                    )
                    continue

            post_cid = self._post_target_of(action)
            if not post_cid:
                verdicts.append(GateVerdict(action, True, "ok", ""))
                continue

            # He is asleep. The live path already refuses to answer people who
            # talk to him and tells them "max is sleeping, back in Xm" — but
            # nothing checked it here, so the tick would post unprompted into
            # a channel or DM someone while that notice was still standing.
            # Speaking only: research, memory and goal work carry on.
            if self._sleeping():
                verdicts.append(
                    GateVerdict(
                        action,
                        False,
                        "asleep",
                        "you are in a sleep window — not speaking until you wake",
                        post_cid,
                    )
                )
                continue

            # Same-tick dedup: one plan does not get to post twice into one
            # room. Checked before the floor read so a duplicate costs
            # nothing. Note this deliberately runs even when turn-taking is
            # disabled — it's structural, not a matter of taste.
            if post_cid in planned_post_channels:
                logger.info(
                    f"Autonomy skip duplicate post to {post_cid} in same tick/plan"
                )
                verdicts.append(
                    GateVerdict(
                        action,
                        False,
                        "duplicate_post",
                        "already sent to this conversation in this tick",
                        post_cid,
                    )
                )
                continue

            # THE GATE. One read of the room decides every "should I speak
            # here" question: mid-reply, holding the floor after his own
            # last line, already handled by the live path, inside the
            # cooldown, or cutting into someone else's exchange.
            # See autonomy_social.
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
                    verdicts.append(
                        GateVerdict(
                            action,
                            False,
                            "floor",
                            f"not your turn in this conversation "
                            f"[{verdict.state}] — {verdict.reason}",
                            post_cid,
                        )
                    )
                    continue

            # Claim the room only once it's cleared to speak in. Claiming
            # before the gate would make a *blocked* action consume the
            # slot, and the next action aimed at the same room would come
            # back "already sent" — which is false, and that string is fed
            # to the planner as feedback next tick.
            planned_post_channels.add(post_cid)
            verdicts.append(GateVerdict(action, True, "ok", "", post_cid))
        return verdicts

    # ─── stage 4: execute ─────────────────────────────────────────────

    async def execute(
        self, actions: list[dict], planned_post_channels: set[str] | None = None
    ) -> list[dict]:
        """Gate then run. Kept as one call for the many callers that want both.

        The tick uses the stages separately so it can report what the gate
        decided; everything else (tests, `,autonomy run`, the tool loop's
        mechanical-skip path) wants plan-in, results-out.
        """
        verdicts = await self.policy_gate(actions, planned_post_channels)
        return await self.run_allowed(verdicts)

    async def run_allowed(self, verdicts: list[GateVerdict]) -> list[dict]:
        """Execute the actions the gate allowed. One failure doesn't kill the rest.

        Denied actions still produce a result row — the planner reads results
        as feedback, and an action that silently vanished would be re-planned
        next tick forever.
        """
        results = []
        ACTION_TIMEOUT = 30  # seconds per action

        for verdict in verdicts:
            action = verdict.action
            # bail if bot disconnected mid-tick
            if self.bot.is_closed():
                logger.warning(
                    "Bot disconnected during autonomy tick, aborting remaining actions"
                )
                break

            kind = action.get("kind", "do_nothing")
            result = {"kind": kind, "result": "success", "error": None}

            if not verdict.allowed:
                results.append(
                    {
                        "kind": kind,
                        "result": "skipped",
                        "error": None,
                        "denied_by": verdict.code,
                        "content_summary": verdict.reason,
                    }
                )
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
                self._note_autonomy_post(dm_channel.id, msg.id)
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
                self._note_autonomy_post(channel_id, msg.id)
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
                if tool_name in AUTONOMY_POST_TOOLS:
                    self._note_autonomy_post(getattr(channel, "id", target_cid))
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
        # to bump goal last_acted_on and stamp last_action_at.
        acted = total_exec > 0 and any(
            r.get("result") == "success" and r.get("kind") != "do_nothing"
            for r in results
        )

        # BUG FIX: use tick START time as watermark so events recorded during
        # plan/execute are not dropped from the next tick.
        _reflect_fired = self._reflect_pending_persist
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
            s.pop("drives", None)
            s.pop("drives_updated_at", None)
            if _reflect_fired:
                s["last_reflect_at"] = tick_start_iso or _utcnow_iso()
            if acted:
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
