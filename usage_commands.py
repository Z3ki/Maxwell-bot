"""Discovery commands for message usage.

Usage is counted in messages over a rolling window. That is the only usage
measure. These commands answer only when someone asks; they never start an AI
turn and never send a DM.
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

DISCOVERY_COMMANDS = frozenset({"help", "usage"})

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
                {"name": "Server settings", "value": "server"},
                {"name": "Application owner", "value": "owner"},
            ],
        },
    ],
}
USAGE_COMMAND = {
    "name": "usage",
    "description": "Show your message allowance.",
    **_COMMAND_META,
}


_HELP_TOPICS = {
    "start": (
        "Getting started",
        [
            "`/maxwell prompt:<your question>` — brief plain-text answers by default.",
            "Message → Apps → Ask Maxwell — enter a request and choose the same actions as /maxwell. Rewrite / Translate opens the same form; Summarize, Explain, and Fact-check run immediately.",
            "`/cancel` — stop your running request in this interaction context.",
            "`/help` — browse this list by topic.",
            "`/usage` — check your current message allowance.",
        ],
    ),
    "personal": (
        "Personal settings",
        [
            "`/config` — open the private settings menu for response mode, research, detail, context, language, and reply style.",
            "`/config` → Personality — set or clear a personal writing preference.",
            "`/config` → Reply visibility — choose private or public replies; public is the default.",
            "`/config` → Bring your own key — save, test, view status, or delete a supported provider key privately.",
        ],
    ),
    "server": (
        "Server settings",
        [
            "`/config` — open the settings menu. Server controls appear for the server owner or members with Manage Server.",
            "Authorized managers can set the allowed channel, capability groups, moderation policy, progress messages, and ticket greetings.",
        ],
    ),
    "owner": (
        "Application owner settings",
        [
            "Configured application owners can open the Application owner section of `/config` for redacted diagnostics, global tool/autonomy/quota switches, quota overrides, and trusted control reload.",
            "Guild ownership and server administrator permissions do not grant access to application-owner settings.",
        ],
    ),
}


def command_help_text(*, topic: str | None = None) -> str:
    normalized = str(topic or "").strip().lower()
    if normalized in _HELP_TOPICS:
        heading, rows = _HELP_TOPICS[normalized]
        lines = [f"**{heading}**", *rows]
    else:
        lines = [
            "**Maxwell commands**",
            "**Start:** `/maxwell`, `/help`, `/usage`.",
            "**Personal:** `/config` opens private personal settings, including response preferences, style, visibility, and BYOK.",
            "**Server:** `/config` exposes channel, capability, moderation, progress, and ticket settings to authorized server managers.",
            "**Application owner:** `/config` exposes redacted diagnostics and fixed global controls only to configured application owners.",
            "Use `/maxwell` for requests, tools, memory, creative work, reminders, jobs, and voice features.",
            "Use `/cancel` to stop your active request in the current interaction context.",
            "Use `/help topic:<category>` to see one set of commands.",
        ]
    return "\n".join(lines)


def usage_status_text(state: dict) -> str:
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
    return "\n".join(lines)


def _control(bot: Any) -> dict:
    raw = getattr(bot, "_control", None)
    return raw if isinstance(raw, dict) else {}


def _user_id(interaction: Any) -> str:
    user = getattr(interaction, "user", None) or getattr(interaction, "author", None)
    return str(getattr(user, "id", "") or "")


def usage_text_for(bot: Any, user_id: str) -> str:
    control = _control(bot)
    if not control.get("message_quota_enabled", False):
        return "Unlimited messages. This bot has no configured message allowance."
    ledger = getattr(bot, "_message_quota", None)
    if ledger is None or not user_id:
        return "Message allowance is unavailable."
    state = ledger.status(
        user_id,
        enforced_message_limit(control),
        enforced_window_seconds(control),
    )
    return usage_status_text(state)


async def handle_discovery_interaction(bot: Any, interaction: Any) -> bool:
    """Answer /help and /usage. Never starts an AI turn or a DM."""
    data = ui._interaction_data(interaction)
    name = str(data.get("name") or "")
    if name not in DISCOVERY_COMMANDS:
        return False
    if name == "help":
        options = dict(ui._option_pairs(data.get("options")))
        text = command_help_text(topic=options.get("topic"))
    else:
        text = usage_text_for(bot, _user_id(interaction))
    try:
        await ui._ephemeral(interaction, text[:1900])
    except Exception:
        logger.exception("Could not deliver %s command", name)
    return True


def install_usage_commands(bot: Any = None) -> None:
    """Register the help and usage commands. Safe to call more than once."""
    for command in (HELP_COMMAND, USAGE_COMMAND):
        ui.register_command(command)
    ui.unregister_command("premium")
    ui.register_interaction_handler(
        handle_discovery_interaction, priority=20, name="usage_discovery"
    )
