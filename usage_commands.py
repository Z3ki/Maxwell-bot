"""Optional discovery commands for message allowance and Maxwell Plus.

Premium is not for sale. These commands answer only when someone asks.
They do not check out, announce a launch, or send a DM.
"""

from __future__ import annotations

import logging
from typing import Any

import user_install as ui
from message_quota import (
    FREE_MESSAGE_LIMIT,
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
    "description": "Show Maxwell commands.",
    **_COMMAND_META,
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


def command_help_text(*, discovery: bool) -> str:
    lines = [
        "Maxwell slash commands:",
        "AI: `/maxwell`, `/image`, `/chess`, `/checkers`, `/moderation`, `/memory`, `/reminder`.",
        "Personal: `/config` for your defaults; `/personality` for your reply-style preference; `/usage` for your allowance.",
        "Server settings: `/config scope:server` (Manage Server or configured Maxwell admin), `/progress`, `/ticket-greetings`, `/solo`, `/server-prompt`, `/clear-server-prompt`.",
        "Memory: `/memory`, `/context`, `/clear-memory`, `/negative-memory`, `/downvote`, `/summarize-memory`.",
        "Operations: `/stop`, `/jobs`, `/job`, `/rem`, `/autonomy`, `/sleep`, `/wake`, `/voice`, `/plugins`, `/admin`, `/blacklist`, `/unblacklist`.",
        "Restricted: `/diagnostics` and `/maintenance` are available only to configured Maxwell developers.",
    ]
    if discovery:
        lines.append("Discovery: `/premium` shows current plan information; it does not start a purchase.")
    return "\n".join(lines)


def usage_status_text(state: dict, *, discovery: bool) -> str:
    window = format_window(int(state.get("window_seconds") or FREE_WINDOW_SECONDS))
    lines = [
        f"Messages: {int(state.get('used') or 0)}/{int(state.get('limit') or 0)} "
        f"used in the last {window}."
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


def premium_discovery_text(
    *,
    limit: int = FREE_MESSAGE_LIMIT,
    window_seconds: int = FREE_WINDOW_SECONDS,
    discovery: bool = True,
) -> str:
    if not discovery:
        return "Plan details are turned off."
    window = format_window(window_seconds)
    return (
        "Maxwell Plus is not for sale. Billing and paid restrictions are off. "
        "This does not start a purchase.\n"
        f"Free allowance: {int(limit)} messages per rolling {window}.\n"
        f"Personal Plus is proposed at {PERSONAL_PLUS_PRICE}. It would raise that "
        "individual allowance. The higher amount is not decided.\n"
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
    return premium_discovery_text(
        limit=enforced_message_limit(control),
        window_seconds=enforced_window_seconds(control),
        discovery=discovery_enabled(control),
    )


async def handle_discovery_interaction(bot: Any, interaction: Any) -> bool:
    """Answer /help, /usage, and /premium. Never starts an AI turn or a DM."""
    data = ui._interaction_data(interaction)
    name = str(data.get("name") or "")
    if name not in DISCOVERY_COMMANDS:
        return False
    control = _control(bot)
    discovery = discovery_enabled(control)
    if name == "help":
        text = command_help_text(discovery=discovery)
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
