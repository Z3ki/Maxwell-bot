"""Conversational turn-taking for Maxwell's autonomy loop.

The autonomy loop runs on a timer. Conversation runs on turns. Those two
clocks have nothing to do with each other, and reconciling them is the whole
job of this module.

Without it, autonomy wakes up mid-conversation and posts — over a reply the
main bot is still writing, or on top of a line Maxwell just said that nobody
has answered yet. From the outside that reads as a bot barging into its own
conversation, because that is exactly what it is.

People don't need a rule for this. They read the room: who spoke last, whether
anyone is waiting on them, whether two other people are mid-exchange. This
module does that reading and returns a per-channel verdict — does Maxwell hold
the floor here right now, and if not, why not.

The verdict is used twice, deliberately:

1. As *context* — rendered into the planner prompt so the model sees which
   rooms are its to speak in and can choose freely among the rest.
2. As a *gate* — re-checked against live state immediately before anything
   sends, because the plan was made seconds ago and rooms change.

Nothing here restricts WHAT Maxwell does. It only answers WHERE and WHEN
speaking is his turn. Every non-speaking action (research, memory, goals,
reflection) is untouched by this module by design.

Pure functions where possible: `read_floor` takes a normalized snapshot and
returns a verdict with no I/O, so the turn-taking rules are unit-testable
without a Discord connection.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

# ---------------------------------------------------------------------------
# Floor states
# ---------------------------------------------------------------------------
# Two groups: states where speaking is Maxwell's turn, and states where it
# isn't. The names are written to be read by the planner LLM as much as by us,
# so they say what's true about the room rather than what the code decided.

FLOOR_REPLYING = "REPLYING"    # main bot is generating a reply here right now
FLOOR_HOLDING = "HOLDING"      # Maxwell spoke last; nobody has answered yet
FLOOR_HANDLED = "HANDLED"      # the live reply path already answered the newest ping
FLOOR_COOLDOWN = "COOLDOWN"    # Maxwell spoke very recently; too soon to start again
FLOOR_BUSY = "BUSY"            # other people are mid-exchange and not talking to him
FLOOR_ADDRESSED = "ADDRESSED"  # someone is waiting on Maxwell and nothing has answered
FLOOR_OPEN = "OPEN"            # normal room, his turn is available
FLOOR_IDLE = "IDLE"            # quiet room, nothing in flight

#: States in which an unprompted message is Maxwell's to send.
FLOOR_OPEN_STATES = frozenset({FLOOR_ADDRESSED, FLOOR_OPEN, FLOOR_IDLE})

#: Ordering used when the planner has to pick a room: someone waiting beats a
#: live room, which beats a dead one.
FLOOR_PRIORITY = {
    FLOOR_ADDRESSED: 3,
    FLOOR_OPEN: 2,
    FLOOR_IDLE: 1,
}

#: Planner-facing explanation per state. Written in second person because it
#: lands in the prompt verbatim.
FLOOR_HINTS = {
    FLOOR_REPLYING: "you are already answering here right now — do not send a second message",
    FLOOR_HOLDING: "you spoke last and nobody has replied yet — wait for them, don't talk to yourself",
    FLOOR_HANDLED: "your live reply already covered the newest message here",
    FLOOR_COOLDOWN: "you just spoke here — too soon to start something new",
    FLOOR_BUSY: "other people are mid-exchange and not talking to you — let them finish",
    FLOOR_ADDRESSED: "someone is waiting on you here and nothing has answered them yet",
    FLOOR_OPEN: "normal room, nobody mid-thought — speaking here is fine if you have something",
    FLOOR_IDLE: "quiet for a while — fine to start something if you actually want to",
}


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FloorSettings:
    """Timing thresholds for reading a room.

    Defaults are tuned to human conversational rhythm rather than to whatever
    the tick interval happens to be. They intentionally do NOT scale with
    `autonomy_interval_seconds`: how long it's polite to wait before speaking
    again is a property of the conversation, not of how often Maxwell wakes up.
    """

    #: After Maxwell's own last visible line, how long before an *unprompted*
    #: new line is reasonable. Being addressed bypasses this.
    cooldown_seconds: float = 90.0
    #: How long Maxwell keeps holding the floor after speaking into silence.
    #: Past this the room has plainly moved on and a fresh start is fair.
    hold_release_seconds: float = 1800.0
    #: Window in which several messages from several people counts as an
    #: exchange in progress.
    mid_flow_seconds: float = 45.0
    mid_flow_min_messages: int = 3
    #: Silence past this and the room reads as idle rather than active.
    idle_after_seconds: float = 600.0

    @classmethod
    def from_control(cls, control: Any) -> "FloorSettings":
        """Build from the bot's `_control` dict, falling back to defaults.

        `autonomy_recent_reply_block_seconds` is the pre-existing knob for the
        same idea. It's honoured as a *floor* on the cooldown so an operator
        who already tuned it up doesn't silently get a shorter window here.
        """
        control = control if isinstance(control, dict) else {}

        def _num(key: str, default: float) -> float:
            try:
                val = float(control.get(key, default) or default)
            except (TypeError, ValueError):
                return float(default)
            return val if val >= 0 else float(default)

        cooldown = _num("autonomy_floor_cooldown_seconds", cls.cooldown_seconds)
        legacy = _num("autonomy_recent_reply_block_seconds", 0.0)
        return cls(
            cooldown_seconds=max(cooldown, legacy),
            hold_release_seconds=_num(
                "autonomy_floor_hold_release_seconds", cls.hold_release_seconds
            ),
            mid_flow_seconds=_num(
                "autonomy_floor_mid_flow_seconds", cls.mid_flow_seconds
            ),
            mid_flow_min_messages=max(
                2, int(_num("autonomy_floor_mid_flow_messages", cls.mid_flow_min_messages))
            ),
            idle_after_seconds=_num(
                "autonomy_floor_idle_seconds", cls.idle_after_seconds
            ),
        )


# ---------------------------------------------------------------------------
# Normalized message + verdict
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FloorMessage:
    """The only four things turn-taking actually needs to know about a message.

    Deliberately not a Discord object: `read_floor` stays pure and testable,
    and the same rules work for DMs, guild channels, or any future surface.
    """

    created_at: datetime | None = None
    is_self: bool = False          # Maxwell said it
    is_bot: bool = False           # some bot said it (including Maxwell)
    addresses_self: bool = False   # mentions Maxwell or replies to him
    author_id: str = ""


@dataclass(frozen=True)
class FloorVerdict:
    """Whether this room is Maxwell's to speak in, and why."""

    channel_id: str
    state: str
    may_speak: bool
    reason: str
    label: str = ""
    #: Seconds since the newest message, for rendering. None when empty.
    silence_seconds: float | None = None

    @property
    def hint(self) -> str:
        return FLOOR_HINTS.get(self.state, "")

    @property
    def priority(self) -> int:
        return FLOOR_PRIORITY.get(self.state, 0)


# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------


def _aware(value: Any) -> datetime | None:
    """Coerce to a tz-aware UTC datetime, or None."""
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _epoch_to_dt(value: Any) -> datetime | None:
    """Coerce a `time.time()`-style float (as used by `bot._last_bot_reply`)."""
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def floor_message_from_discord(
    message: Any,
    *,
    bot_user: Any = None,
    reply: Any = None,
    implicit_address: bool = False,
) -> FloorMessage:
    """Normalize a discord.Message (plus its already-resolved reply, if any).

    `reply` is passed in rather than fetched so this stays synchronous — the
    caller in gather_context already resolves references through a shared
    cache and there's no reason to pay for that twice.

    `implicit_address` marks surfaces where every inbound message is aimed at
    Maxwell regardless of mentions — a 1:1 DM being the obvious one. Nobody
    @-mentions you in a DM; the whole channel is the mention.
    """
    author = getattr(message, "author", None)
    author_id = str(getattr(author, "id", "") or "")
    bot_id = str(getattr(bot_user, "id", "") or "") if bot_user is not None else ""
    is_self = bool(bot_id and author_id == bot_id)

    addresses_self = bool(implicit_address and not is_self)
    if bot_id and not addresses_self:
        for user in list(getattr(message, "mentions", []) or [])[:20]:
            if str(getattr(user, "id", "") or "") == bot_id:
                addresses_self = True
                break
        if not addresses_self and reply is not None:
            reply_author = getattr(reply, "author", None)
            if str(getattr(reply_author, "id", "") or "") == bot_id:
                addresses_self = True

    return FloorMessage(
        created_at=_aware(getattr(message, "created_at", None)),
        is_self=is_self,
        is_bot=bool(getattr(author, "bot", False)),
        addresses_self=addresses_self,
        author_id=author_id,
    )


# ---------------------------------------------------------------------------
# The rules
# ---------------------------------------------------------------------------


def read_floor(
    channel_id: str,
    messages: Iterable[FloorMessage],
    *,
    now: datetime | None = None,
    is_replying: bool = False,
    last_bot_reply_ts: Any = None,
    settings: FloorSettings | None = None,
    label: str = "",
) -> FloorVerdict:
    """Decide whether Maxwell may speak unprompted in this channel.

    Pure: no I/O, no bot access. `messages` may be in any order and may
    include messages with no timestamp (those are ignored — an undated
    message can't be placed in a turn order).

    The checks run in the order a person would apply them, most conclusive
    first. Order matters: being mid-reply beats everything, holding the floor
    beats being addressed (if he spoke last, whatever addressed him came
    before that and is already answered), and cooldown only applies once
    we know nobody is actually waiting on him.
    """
    settings = settings or FloorSettings()
    now = _aware(now) or datetime.now(timezone.utc)

    def verdict(state: str, reason: str, silence: float | None = None) -> FloorVerdict:
        return FloorVerdict(
            channel_id=str(channel_id),
            state=state,
            may_speak=state in FLOOR_OPEN_STATES,
            reason=reason,
            label=label,
            silence_seconds=silence,
        )

    # 1. The main bot is mid-reply here. Nothing else matters — a second
    #    message now lands on top of one that's still being written.
    if is_replying:
        return verdict(FLOOR_REPLYING, "main reply path is generating here")

    dated = sorted(
        (m for m in messages if m.created_at is not None),
        key=lambda m: m.created_at,  # type: ignore[arg-type,return-value]
    )
    if not dated:
        # Nothing visible — but "I can't see the room" is not the same as "the
        # room is empty". If the main path replied here recently, that's real
        # evidence of a live conversation the history window just missed.
        empty_reply_dt = _epoch_to_dt(last_bot_reply_ts)
        if empty_reply_dt is not None:
            since_reply = (now - empty_reply_dt).total_seconds()
            if 0 <= since_reply < settings.cooldown_seconds:
                return verdict(
                    FLOOR_COOLDOWN, f"you replied here {_ago(since_reply)}"
                )
        return verdict(FLOOR_IDLE, "no visible messages in window")

    newest = dated[-1]
    silence = max(0.0, (now - newest.created_at).total_seconds())  # type: ignore[operator]

    # Two different notions of "when did Maxwell last speak", and conflating
    # them is a real bug rather than a nicety.
    #
    # `last_self_msg_dt` is what's actually visible in the room, and it's what
    # decides which inbound messages are still unanswered.
    #
    # `last_self_dt` also folds in `_last_bot_reply`, the in-memory stamp for a
    # reply the main path sent that may not have landed in the history window
    # yet. That one drives the cooldown — but it must NOT truncate the pending
    # scan, or a ping the live path just answered reads as "you spoke recently"
    # instead of "that's already handled". Both block, so the gate is right
    # either way; the state string is what the planner reads, so it has to be
    # the true one.
    self_times = [m.created_at for m in dated if m.is_self and m.created_at]
    last_self_msg_dt = max(self_times, default=None)
    last_reply_dt = _epoch_to_dt(last_bot_reply_ts)
    last_self_dt = max(
        [dt for dt in (last_self_msg_dt, last_reply_dt) if dt is not None],
        default=None,
    )

    # 2. Maxwell spoke last and the room hasn't answered. This is the exact
    #    shape of "the bot randomly barged into its own conversation": the
    #    only thing on screen is him, and autonomy wants to add more of him.
    #    Held until the room has plainly moved on.
    if newest.is_self and silence < settings.hold_release_seconds:
        return verdict(
            FLOOR_HOLDING,
            f"you were the last speaker {_ago(silence)} and nobody replied",
            silence,
        )

    # 3. Is anyone actually waiting on him? Only messages that arrived AFTER
    #    his last appearance count — earlier pings were answered by whatever
    #    he said afterward.
    pending = None
    for m in reversed(dated):
        if m.is_self:
            break
        if (
            last_self_msg_dt is not None
            and m.created_at is not None
            and m.created_at <= last_self_msg_dt
        ):
            break
        if m.addresses_self and not m.is_bot:
            pending = m
            break

    if pending is not None:
        # The live reply path may have picked it up between the plan and now.
        if last_reply_dt is not None and pending.created_at is not None and last_reply_dt >= pending.created_at:
            return verdict(
                FLOOR_HANDLED, "live reply already answered the newest ping", silence
            )
        waited = (
            max(0.0, (now - pending.created_at).total_seconds())
            if pending.created_at
            else silence
        )
        return verdict(FLOOR_ADDRESSED, f"waiting since {_ago(waited)}", silence)

    # 4. Nobody is waiting. From here on, speaking is Maxwell starting
    #    something — so the ordinary politeness rules apply.
    if last_self_dt is not None:
        since_self = max(0.0, (now - last_self_dt).total_seconds())
        if since_self < settings.cooldown_seconds:
            return verdict(
                FLOOR_COOLDOWN,
                f"you spoke here {_ago(since_self)}, inside the {int(settings.cooldown_seconds)}s cooldown",
                silence,
            )

    # 5. Two or more other people trading messages right now. Cutting in is
    #    what makes a bot feel like an interruption rather than a participant.
    if silence <= settings.mid_flow_seconds:
        recent_authors = set()
        recent_count = 0
        for m in reversed(dated):
            if m.created_at is None:
                continue
            if (now - m.created_at).total_seconds() > settings.mid_flow_seconds:
                break
            if m.is_self:
                continue
            recent_count += 1
            if m.author_id:
                recent_authors.add(m.author_id)
        if (
            recent_count >= settings.mid_flow_min_messages
            and len(recent_authors) >= 2
        ):
            return verdict(
                FLOOR_BUSY,
                f"{recent_count} messages from {len(recent_authors)} people in the last "
                f"{int(settings.mid_flow_seconds)}s",
                silence,
            )

    # 6. Quiet room vs live room. Both are open; the distinction only shapes
    #    what's natural to say.
    if silence >= settings.idle_after_seconds:
        return verdict(FLOOR_IDLE, f"quiet for {_duration(silence)}", silence)
    return verdict(FLOOR_OPEN, f"last message {_ago(silence)}, nobody mid-thought", silence)


def _duration(seconds: float) -> str:
    """Compact human duration. Rendering only — never parsed back."""
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def _ago(seconds: float) -> str:
    return f"{_duration(seconds)} ago"


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_floor_section(verdicts: Sequence[FloorVerdict]) -> str:
    """Render the planner-facing CONVERSATION FLOOR block.

    Open rooms are listed first and sorted by priority, so if the model reads
    only the first line it still reads the most relevant one.
    """
    if not verdicts:
        return (
            "=== CONVERSATION FLOOR ===\n"
            "(no rooms read this tick — no channel is confirmed open, so don't "
            "post; non-speaking actions are unaffected)"
        )

    open_v = sorted(
        (v for v in verdicts if v.may_speak),
        key=lambda v: (-v.priority, v.silence_seconds or 0.0),
    )
    closed_v = sorted(
        (v for v in verdicts if not v.may_speak),
        key=lambda v: v.silence_seconds or 0.0,
    )

    lines = [
        "=== CONVERSATION FLOOR (whose turn it is in each room) ===",
        "Timing is the one thing that isn't your free choice. This is the room "
        "read, not a suggestion — messages aimed at a closed room are dropped "
        "before they send.",
    ]
    if open_v:
        lines.append("YOUR TURN — you may speak in these:")
        for v in open_v:
            lines.append(f"- {v.label or v.channel_id} [{v.state}] {v.hint} ({v.reason})")
    else:
        lines.append(
            "YOUR TURN — none right now. Say nothing anywhere this tick; "
            "spend it on something that isn't talking."
        )
    if closed_v:
        lines.append("NOT YOUR TURN — no messages here:")
        for v in closed_v[:12]:
            lines.append(f"- {v.label or v.channel_id} [{v.state}] {v.hint}")
    return "\n".join(lines)


def summarize_floor(verdicts: Sequence[FloorVerdict]) -> str:
    """One-line summary for logs."""
    if not verdicts:
        return "floor: no rooms read"
    parts = [f"{v.label or v.channel_id}={v.state}" for v in verdicts[:8]]
    return "floor: " + " ".join(parts)
