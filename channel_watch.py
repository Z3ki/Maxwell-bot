"""New-channel / ticket-channel detection for Maxwell.

Maxwell watches for channels being created. Two things matter:

* A brand-new **text** channel is a fresh room he has never seen. To be useful
  there he has to notice it — which is what posting a self-authored opening
  line into a support/ticket channel does (the bot's own messages go through
  the normal memory path, so the room lands in his context and his
  conversation-watch gets armed).

* A channel whose name looks like a **support / ticket / modmail / report**
  channel is high-signal: someone is about to ask for help. Those get a
  greeting, and the rest just get logged and watched passively.

Everything here is a pure function or a small lookup so the behaviour can be
tested with no Discord connection. The wiring (the ``on_guild_channel_create``
event handler) lives in ``bot.py``; this module only decides *what kind* a
channel is and *what to say*.
"""

from __future__ import annotations

import re

# Names that flag a channel as support / ticket / modmail / report space. These
# are matched against the channel name (normalised, no separators), so
# "ticket-1", "support-tickets", "reports", "mod-mail", "help" all register.
_TICKET_SIGNALS = (
    "ticket",
    "tickets",
    "support",
    "help",
    "helpdesk",
    "modmail",
    "mod-mail",
    "report",
    "reports",
    "appeal",
    "appeals",
    "complaint",
    "complaints",
    "bugreport",
    "bug-report",
    "requests",
    "request",
    "openticket",
)

# Rate / spam / general chatter names that are NOT support spaces even though a
# single token above might appear inside a longer word. Prevent matching a
# random word that merely *contains* a signal (e.g. "unhelpful").
_TICKET_NEGATIVE_SIGNALS = (
    "general",
    "chat",
    "lounge",
    "offtopic",
    "random",
    "memes",
    "spam",
)

_ticket_signal_re = re.compile(
    r"(?:^|[^a-z0-9])(" + "|".join(_TICKET_SIGNALS) + r")(?:[^a-z0-9]|$)",
    re.IGNORECASE,
)
_negative_signal_re = re.compile(
    r"(?:^|[^a-z0-9])(" + "|".join(_TICKET_NEGATIVE_SIGNALS) + r")(?:[^a-z0-9]|$)",
    re.IGNORECASE,
)


def normalise_channel_name(name: str) -> str:
    """Lowercase, strip Discord separators -> a single token layer."""
    return re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-")


def is_ticket_channel(name: str) -> bool:
    """True when a text channel name reads as a support/ticket space."""
    raw = str(name or "").lower()
    if not raw:
        return False
    # A negative signal ("general", "chat", ...) dominates: "
    # #ticket-chat" or "#support-general" is not a ticket room.
    if _negative_signal_re.search(raw):
        return False
    return bool(_ticket_signal_re.search(raw))


def channel_kind(name: str) -> str:
    """Classify a text channel by name: ``"ticket"`` or ``"normal"``."""
    return "ticket" if is_ticket_channel(name) else "normal"


def greeting_for(name: str, bot_name: str = "maxwell") -> str:
    """The opening line Maxwell posts into a brand-new ticket channel.

    Short and calm. The room is brand new, so this is both a presence marker
    and a promise to help — not an essay.
    """
    label = (str(name or "ticket").strip() or "ticket")
    return (
        f"New {label} channel spotted — I'm {bot_name} and I'm here. "
        f"Say what you need and I'll help."
    )


def default_ticket_greeting() -> bool:
    """Whether auto-greeting new ticket channels is on by default."""
    return True
