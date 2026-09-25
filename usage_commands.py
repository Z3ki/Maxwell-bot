"""Optional discovery commands for message allowance and Maxwell Plus.

Premium is not for sale. These commands answer only when someone asks.
They do not check out, announce a launch, or send a DM.
"""

from __future__ import annotations

import logging
from typing import Any

import user_install as ui
from message_quota import (
    FREE_WINDOW_SECONDS,
    enforced_message_limit,
    enforced_window_seconds,
    format_window,
)

logger = logging.getLogger(__name__)

PERSONAL_PLUS_PRICE = "$2.99/month per user"
SERVER_PLUS_PRICE = "$4.99/month per server"
DISCOVERY_COMMANDS = frozenset({"help", "usage", "premium"})

_COMMAND_META = {
    "type": 1,
    "integration_types": [0, 1],
    "contexts": [0, 1, 2],
}

HELP_COMMAND = {
    "name": "help",
    "description": "Browse Maxwell commands by category.",
    **_COMMAND_META,
    "options": [
        {
            "name": "topic",
            "description": "Show commands for one topic",
            "type": 3,
            "required": False,
            "choices": [
                {"name": "Getting started", "value": "start"},
                {"name": "Personal settings", "value": "personal"},
                {"name": "AI and games", "value": "creative"},
                {"name": "Server settings", "value": "server"},
                {"name": "Memory", "value": "memory"},
                {"name": "Jobs and voice", "value": "tools"},
                {"name": "Moderation", "value": "moderation"},
                {"name": "Developer tools", "value": "developer"},
            ],
        },
    ],
}
USAGE_COMMAND = {
    "name": "usage",
    "description": "Show your message allowance.",
    **_COMMAND_META,
}
PREMIUM_COMMAND = {
    "name": "premium",
    "description": "Show Maxwell Plus plan details.",
    **_COMMAND_META,
}


def discovery_enabled(control: dict | None) -> bool:
    raw = (control or {}).get("premium_discovery_enabled", True)
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


_HELP_TOPICS = {
    "start": (
        "Getting started",
        [
            "`/maxwell prompt:<your question>` — ask Maxwell anything.",
            "`/help` — browse this list by topic.",
            "`/usage` — check your current message allowance.",
        ],
    ),
    "personal": (
        "Personal settings",
        [
            "`/config` — open the private settings menu for response mode, research, detail, context, language, and reply style.",
            "`/personality` — view or edit your reply-style preference directly.",
        ],
    ),
    "creative": (
        "AI and games",
        [
            "`/image prompt:<request>` — create or edit an image; attach an image to use it as a reference.",
            "`/chess prompt:<request>` and `/checkers prompt:<request>` — start or play a game.",
            "`/reminder prompt:<request>` — create, inspect, or cancel a reminder.",
            "`/memory prompt:<request>` — ask Maxwell to recall or manage scoped memory.",
        ],
    ),
    "server": (
        "Server settings",
        [
            "`/config` — open the settings menu. Server controls appear for the server owner, Manage Server administrators, or configured Maxwell admins.",
            "`/progress`, `/ticket-greetings`, and `/solo` — manage individual server settings.",
            "`/autonomy` and `/plugins` — inspect or update server behavior and enabled features.",
        ],
    ),
    "memory": (
        "Memory and feedback",
        [
            "`/memory` — ask Maxwell to recall, save, or manage scoped memory.",
            "`/context` and `/clear-memory` — inspect or clear this channel's stored context.",
            "`/negative-memory`, `/summarize-memory`, and `/downvote` — manage memory and give response feedback.",
        ],
    ),
    "tools": (
        "Jobs and voice",
        [
            "`/stop` — stop a running response.",
            "`/jobs` and `/job` — list, inspect, or cancel your jobs.",
            "`/sleep` and `/wake` — pause or resume Maxwell in a server.",
            "`/voice` — control Maxwell's voice connection and speech.",
            "`/rem` — inspect or run REM maintenance.",
        ],
    ),
    "moderation": (
        "Moderation and access",
        [
            "`/moderation prompt:<request>` — ask Maxwell to help with server moderation; Discord permissions still apply.",
            "`/blacklist` and `/unblacklist` — manage blocked users.",
            "`/admin` — manage configured Maxwell operators.",
        ],
    ),
    "developer": (
        "Developer tools",
        [
            "`/diagnostics` — view restricted runtime diagnostics.",
            "`/maintenance` — run restricted maintenance actions.",
            "`/debug` — view restricted runtime information.",
            "These commands check the configured Maxwell developer IDs when run; visibility alone does not grant access.",
        ],
    ),
}


def command_help_text(*, discovery: bool, topic: str | None = None) -> str:
    normalized = str(topic or "").strip().lower()
    if normalized in _HELP_TOPICS:
        heading, rows = _HELP_TOPICS[normalized]
        lines = [f"**{heading}**", *rows]
    else:
        lines = [
            "**Maxwell commands**",
            "**Start:** `/maxwell`, `/help`, `/usage`.",
            "**Personal:** `/config` opens a private settings menu; `/personality` edits your reply style.",
            "**AI and games:** `/image`, `/chess`, `/checkers`, `/memory`, `/reminder`, `/moderation`.",
            "**Server:** `/config` also shows authorized server controls; `/progress`, `/ticket-greetings`, `/solo`, `/autonomy`, `/plugins`.",
            "**Memory:** `/context`, `/clear-memory`, `/negative-memory`, `/summarize-memory`, `/downvote`.",
            "**Jobs and voice:** `/stop`, `/jobs`, `/job`, `/sleep`, `/wake`, `/voice`, `/rem`.",
            "**Restricted:** `/diagnostics`, `/maintenance`, `/debug`.",
            "Use `/help topic:<category>` to see one set of commands.",
        ]
    if discovery:
        lines.append("`/premium` shows plan information only; it does not start a purchase.")
    return "\n".join(lines)


def usage_status_text(state: dict, *, discovery: bool) -> str:
    window = format_window(int(state.get("window_seconds") or FREE_WINDOW_SECONDS))
    limit = max(1, int(state.get("limit") or 0))
    used = max(0, int(state.get("used") or 0))
    percent_used = int((used * 100 / limit) + 0.5)
    lines = [
        f"Usage: {percent_used}% of your current message allowance used "
        f"in the last {window}."
    ]
    if state.get("exempt"):
        lines.append("This account is exempt from the message limit.")
    resets_in = int(state.get("resets_in") or 0)
    if resets_in > 0:
        lines.append(f"The oldest counted message leaves the window in {format_window(resets_in)}.")
    else:
        lines.append("The window rolls forward as older messages age out.")
    if discovery:
        lines.append("Plan details: /premium")
    return "\n".join(lines)


def premium_discovery_text(*, discovery: bool = True) -> str:
    if not discovery:
        return "Plan details are turned off."
    return (
        "Maxwell Plus is not for sale. Billing and paid restrictions are off. "
        "This does not start a purchase.\n"
        "During Public Alpha, message limits and reset windows may change as "
        "needed to keep the hosted service available. Check /usage for a "
        "percentage of your current allowance and its reset status.\n"
        f"Personal Plus is proposed at {PERSONAL_PLUS_PRICE}. It would provide "
        "a higher individual allowance; exact limits and reset behavior are not decided.\n"
        f"Server Plus is proposed at {SERVER_PLUS_PRICE}. It would use Discord's "
        "native Guild Subscription and remain associated with the purchased server. "
        "It cannot be transferred. The shared server allowance and per-user "
        "fair-use cap are not decided."
    )


def _control(bot: Any) -> dict:
    raw = getattr(bot, "_control", None)
    return raw if isinstance(raw, dict) else {}


def _user_id(interaction: Any) -> str:
    user = getattr(interaction, "user", None) or getattr(interaction, "author", None)
    return str(getattr(user, "id", "") or "")


def usage_text_for(bot: Any, user_id: str, *, discovery: bool | None = None) -> str:
    control = _control(bot)
    if discovery is None:
        discovery = discovery_enabled(control)
    ledger = getattr(bot, "_message_quota", None)
    if ledger is None or not user_id:
        return "Message allowance is unavailable."
    state = ledger.status(
        user_id,
        enforced_message_limit(control),
        enforced_window_seconds(control),
    )
    return usage_status_text(state, discovery=discovery)


def premium_text_for(bot: Any) -> str:
    control = _control(bot)
    return premium_discovery_text(discovery=discovery_enabled(control))


async def handle_discovery_interaction(bot: Any, interaction: Any) -> bool:
    """Answer /help, /usage, and /premium. Never starts an AI turn or a DM."""
    data = ui._interaction_data(interaction)
    name = str(data.get("name") or "")
    if name not in DISCOVERY_COMMANDS:
        return False
    control = _control(bot)
    discovery = discovery_enabled(control)
    if name == "help":
        options = dict(ui._option_pairs(data.get("options")))
        text = command_help_text(discovery=discovery, topic=options.get("topic"))
    elif name == "usage":
        text = usage_text_for(bot, _user_id(interaction), discovery=discovery)
    else:
        text = premium_text_for(bot)
    try:
        await ui._ephemeral(interaction, text[:1900])
    except Exception:
        logger.exception("Could not deliver %s command", name)
    return True


def install_usage_commands() -> None:
    """Register the discovery commands. Safe to call more than once."""
    for command in (HELP_COMMAND, USAGE_COMMAND, PREMIUM_COMMAND):
        ui.register_command(command)
    ui.register_interaction_handler(
        handle_discovery_interaction, priority=20, name="usage_discovery"
    )
