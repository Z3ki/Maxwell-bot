"""When Maxwell should speak up, and when a message is worth remembering.

Two decisions used to be made by fixed constants and a list of trigger
phrases:

* **Conversation watch.** After he speaks, a room stayed "on watch" for a
  flat 180 seconds — the same 180 whether the room was mid-conversation with
  him or had ignored his last four contributions. Every human line in that
  window became a full LLM turn whose answer was usually `no_response`.
* **Context extraction.** A message was worth remembering if it contained one
  of about fifteen English phrases — "remember", "i like", "my name is". Say
  the same thing in other words and nothing was kept; say "I like it" about a
  sandwich and it was.

Both are replaced here by scores over observable signals. Nothing in this
module matches on wording. It reads who a message is addressed to, how the
room is moving, whether he was engaged with recently, and how his own last
few decisions in that room went — and it keeps state, so a room where he is
being ignored quietly falls out of watch instead of burning a turn per line.

Everything is a pure function or a small dataclass so the behaviour can be
tested without a Discord connection.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# Conversation watch
# --------------------------------------------------------------------------

# Bounds on the adaptive watch window, as multiples of the configured base.
# A room he is actively in conversation with can hold the watch about twice
# as long as configured; a room ignoring him collapses to a quarter of it.
WINDOW_MAX_FACTOR = 2.0
WINDOW_MIN_FACTOR = 0.25

# Bounds on the adaptive debounce, same idea. Fast rooms wait longer so a
# burst becomes one turn; a slow room answers nearly immediately.
DEBOUNCE_MAX_FACTOR = 4.0
DEBOUNCE_MIN_FACTOR = 0.5

# How many consecutive silent decisions it takes to fully discount a room.
SILENCE_PATIENCE = 4


@dataclass
class WatchState:
    """What the bot has observed about one room's conversation.

    All times are event-loop monotonic seconds; the caller supplies `now` so
    tests don't have to sleep.
    """

    # Consecutive watch follow-ups where he chose not to speak. Reset the
    # moment he says something.
    silent_streak: int = 0
    # Last time he actually posted in this room.
    spoke_at: float = 0.0
    # Last time somebody addressed him directly (@, reply, DM).
    engaged_at: float = 0.0
    # Exponential moving average of the gap between messages: the room's
    # speed. Starts unset (0.0) and is only trusted once seeded.
    interval_ema: float = 0.0
    last_message_at: float = 0.0
    # Watch follow-ups considered in this room, ever. Used only for logging.
    turns: int = 0

    def observe_message(self, now: float, *, alpha: float = 0.3) -> None:
        """Fold one message into the room's velocity estimate."""
        if self.last_message_at:
            gap = max(0.0, now - self.last_message_at)
            # Cap the sample: a room silent for an hour then speaking is not a
            # room with an hour-long rhythm, it's a room that just woke up.
            gap = min(gap, 120.0)
            self.interval_ema = (
                gap if not self.interval_ema
                else (alpha * gap) + ((1 - alpha) * self.interval_ema)
            )
        self.last_message_at = now

    def observe_engagement(self, now: float) -> None:
        """Somebody addressed him. Fresh start for the patience counter."""
        self.engaged_at = now
        self.silent_streak = 0

    def observe_spoke(self, now: float) -> None:
        self.spoke_at = now
        self.silent_streak = 0

    def observe_silence(self) -> None:
        self.silent_streak += 1


def engagement_factor(state: WatchState, now: float, base_window: float) -> float:
    """0..1 — how recently this room was actually interacting with him.

    Decays over the base window, so "engaged" means engaged on the same
    timescale the operator configured the watch for, not a fixed constant.
    """
    if not state.engaged_at or base_window <= 0:
        return 0.0
    age = max(0.0, now - state.engaged_at)
    return math.exp(-age / base_window)


def patience_factor(state: WatchState) -> float:
    """1.0 when he is being answered, falling toward 0 as he is ignored."""
    if state.silent_streak <= 0:
        return 1.0
    return max(0.0, 1.0 - (state.silent_streak / SILENCE_PATIENCE))


def presence_factor(state: WatchState, now: float, base_window: float) -> float:
    """0..1 — how much of a participant he currently is in this room.

    Being addressed counts fully. Having spoken recently counts for most of
    it: he is plausibly still in the conversation, but nobody has confirmed
    it by talking back.
    """
    if base_window <= 0:
        return 0.0
    addressed = engagement_factor(state, now, base_window)
    spoke = 0.0
    if state.spoke_at:
        spoke = math.exp(-max(0.0, now - state.spoke_at) / base_window)
    return max(addressed, 0.7 * spoke)


def window_seconds(state: WatchState, base: float, now: float) -> float:
    """How long this room should stay on watch, given how it is going.

    A room mid-conversation earns more time than the configured base; a room
    where his last few contributions went unanswered earns much less. The two
    terms multiply rather than average, because being ignored has to shorten
    the window even in a room he was just part of — that is the case the flat
    timer got wrong, and the whole reason for this.
    """
    if base <= 0:
        return 0.0
    presence = presence_factor(state, now, base)
    patience = patience_factor(state)
    factor = WINDOW_MIN_FACTOR + (
        (WINDOW_MAX_FACTOR - WINDOW_MIN_FACTOR) * presence * patience
    )
    return base * max(WINDOW_MIN_FACTOR, min(factor, WINDOW_MAX_FACTOR))


def debounce_seconds(state: WatchState, base: float) -> float:
    """How long to wait for more lines before collapsing them into one turn.

    Tied to the room's own rhythm: in a room where people post every second,
    replying after a 1s gap means replying mid-thought. In a slow room the
    same wait is dead air.
    """
    if base <= 0:
        return 0.0
    if not state.interval_ema:
        return base
    # Aim to wait roughly one-and-a-half of the room's own beats, so we land
    # in a real gap rather than between two words of the same thought.
    target = state.interval_ema * 1.5
    lo, hi = base * DEBOUNCE_MIN_FACTOR, base * DEBOUNCE_MAX_FACTOR
    return max(lo, min(target, hi))


@dataclass
class AddressSignal:
    """Everything observable about who a message is aimed at.

    Populated from Discord metadata only — no text matching beyond "does his
    name appear", which is a fact about the string, not a rule about wording.
    """

    direct: bool = False           # DM, @ him, or a Discord reply to him
    soft: bool = False             # @everyone / @here / a role he holds
    reply_to_other: bool = False   # a Discord reply aimed at someone else
    mentions_other: bool = False   # @ someone else, not him
    names_him: bool = False        # his name appears in the text
    from_bot: bool = False
    has_media: bool = False
    is_question: bool = False
    text_length: int = 0


def name_mentioned(text: str, names: list[str]) -> bool:
    """Whole-word check for any of his names in the text.

    Not a trigger list — the names come from his own Discord identity at
    runtime, so this follows a rename with no code change.
    """
    body = str(text or "")
    if not body:
        return False
    for name in names:
        candidate = str(name or "").strip()
        if len(candidate) < 3:
            continue
        if re.search(rf"(?<!\w){re.escape(candidate)}(?!\w)", body, re.IGNORECASE):
            return True
    return False


def reply_pressure(signal: AddressSignal, state: WatchState, now: float,
                   base_window: float) -> float:
    """0..1 — how much this line is asking him to say something.

    A hard ping short-circuits to 1.0; the interesting range is the soft
    middle, where a line lands in a room he was recently part of.
    """
    if signal.direct:
        return 1.0
    if signal.from_bot:
        return 0.0
    score = 0.0
    # Being named, or included in a broadcast, is the strongest soft signal.
    if signal.names_him:
        score += 0.45
    if signal.soft:
        score += 0.2
    # Continuity: was he part of what is happening right now?
    score += 0.35 * engagement_factor(state, now, base_window)
    # A question in a room he is part of is more likely aimed at him than a
    # statement is.
    if signal.is_question:
        score += 0.12
    if signal.has_media:
        score += 0.05
    # Explicitly aimed elsewhere. Not disqualifying — people talk to two
    # people at once — but it is evidence.
    if signal.reply_to_other:
        score -= 0.3
    if signal.mentions_other and not signal.names_him:
        score -= 0.25
    # A bare interjection carries almost no request for a reply.
    if signal.text_length and signal.text_length < 4 and not signal.has_media:
        score -= 0.15
    # How his last few turns here went. If four in a row ended in silence,
    # this room is background noise and the bar rises.
    score *= 0.4 + 0.6 * patience_factor(state)
    return max(0.0, min(score, 1.0))


def describe_signal(signal: AddressSignal, state: WatchState, pressure: float,
                    now: float) -> str:
    """One compact line of facts for the prompt, instead of prose rules.

    The model is better at deciding "is this for me" from the actual
    situation than from three paragraphs of instructions that have to
    anticipate every case.
    """
    bits = []
    if signal.direct:
        bits.append("addressed to you directly")
    else:
        if signal.names_him:
            bits.append("your name appears in the text, but nobody pinged you")
        if signal.soft:
            bits.append("broadcast to the room (@everyone/@here/a role), not a ping to you")
        if signal.reply_to_other:
            bits.append("a reply to someone else, not a ping to you")
        if signal.mentions_other:
            bits.append("@ mentioned someone else, not a ping to you")
        if not bits:
            bits.append("posted to the channel, not sent to you — not a ping")
    if state.spoke_at:
        bits.append(f"you last spoke here {int(max(0, now - state.spoke_at))}s ago")
    if state.silent_streak:
        bits.append(f"you stayed quiet the last {state.silent_streak} time(s)")
    if state.interval_ema:
        bits.append(f"room pace ~{state.interval_ema:.0f}s between lines")
    bits.append(f"reply pressure {pressure:.2f}")
    return "Watch signals: " + "; ".join(bits) + "."


# --------------------------------------------------------------------------
# Context extraction
# --------------------------------------------------------------------------

# What "worth an extraction call" looks like, before any per-room adjustment.
#
# Deliberately low. Structure alone cannot tell "I prefer dark mode" from
# "bro that's crazy" — they are the same shape — so this errs toward asking
# the extractor, which is good at exactly that judgement and answers
# should_store:false for free. What keeps the cost down is the per-room
# recency term below, not a higher bar on any single message.
EXTRACT_THRESHOLD = 0.25

_URL = re.compile(r"https?://\S+")
_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)
# A capitalised word that is not the first in its sentence — a decent
# language-agnostic proxy for a name or a product, without a list of names.
_MIDCAP = re.compile(r"(?<=[a-z,\s])\b[A-Z][a-z]{2,}")


@dataclass
class ExtractionContext:
    """Cheap facts about a message and where it landed."""

    text: str = ""
    is_dm: bool = False
    author_is_admin: bool = False
    has_attachments: bool = False
    # Seconds since this room last produced an extraction. Recency raises the
    # bar so one talkative room can't monopolise the extractor.
    since_last_extract: float = math.inf
    author_seen_before: bool = True


@dataclass
class ExtractionScore:
    value: float = 0.0
    reasons: list[str] = field(default_factory=list)

    def add(self, amount: float, why: str) -> None:
        if amount:
            self.value += amount
            self.reasons.append(f"{why}{amount:+.2f}")


def extraction_score(ctx: ExtractionContext) -> ExtractionScore:
    """0..1-ish density score: is there a durable fact plausibly in here?

    Structural only. Long, varied, specific text about named things scores
    high; short repetitive chatter scores low, in any language and with no
    phrase list to keep up to date.
    """
    score = ExtractionScore()
    text = str(ctx.text or "").strip()
    words = _WORD.findall(text)
    count = len(words)

    if count:
        # Saturating length. Most durable facts are short — "call me Z", "the
        # box reboots at 04:00" — so this saturates around six or seven words
        # rather than rewarding essays.
        density = 1 - math.exp(-count / 6.0)
        score.add(0.35 * density, "length")
        # Lexical variety, scaled by that same density. Unscaled, a one-word
        # message scores a perfect variety ratio and "lol" comes out looking
        # as information-dense as a sentence.
        variety = len({w.lower() for w in words}) / count
        score.add(0.2 * variety * density, "variety")
    if ctx.has_attachments:
        score.add(0.15, "media")
    if not count and not ctx.has_attachments:
        return score

    # Specificity: names, numbers, links — the things a durable fact is made of.
    if _MIDCAP.search(text):
        score.add(0.15, "proper-noun")
    if _URL.search(text):
        score.add(0.1, "link")
    if any(ch.isdigit() for ch in text):
        score.add(0.05, "number")

    # A question is a request, not usually a fact worth keeping.
    if text.endswith("?"):
        score.add(-0.15, "question")
    # Shouting and pure punctuation are reactions.
    letters = [c for c in text if c.isalpha()]
    if len(letters) >= 8 and sum(c.isupper() for c in letters) / len(letters) > 0.7:
        score.add(-0.1, "shouting")

    # Where it was said. A DM from an admin is the highest-signal channel he
    # has; this preserves the old DM/admin behaviour without the length rule.
    if ctx.is_dm:
        score.add(0.2, "dm")
    if ctx.author_is_admin:
        score.add(0.15, "admin")
    if not ctx.author_seen_before:
        score.add(0.1, "new-author")

    # Rate limiting as a score, not a lockout. This is the main cost control,
    # which is why it is heavy: a room that just produced an extraction has to
    # clear a much higher bar, so ordinary chatter is blocked for minutes
    # while something genuinely specific still gets through immediately.
    if ctx.since_last_extract < 600:
        score.add(-0.35 * (1 - ctx.since_last_extract / 600.0), "recent-extract")

    score.value = max(0.0, min(score.value, 1.0))
    return score


def should_extract(ctx: ExtractionContext, threshold: float = EXTRACT_THRESHOLD) -> bool:
    return extraction_score(ctx).value >= threshold
