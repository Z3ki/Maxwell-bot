"""User-initiated Premium information. No entitlement, routing or billing hooks."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import user_install as ui

STATES = frozenset({"off", "preview", "launched"})


def state(bot: Any) -> str:
    value = str(getattr(bot, "_control", {}).get("premium_discovery_state", "off"))
    return value if value in STATES else "off"


class DiscoveryStore:
    """Durable preferences and announcement authorization; never sends messages."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS preferences (user_id TEXT PRIMARY KEY, notices INTEGER NOT NULL DEFAULT 1)")
            db.execute("CREATE TABLE IF NOT EXISTS announcement_channels (guild_id TEXT PRIMARY KEY, channel_id TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS announcement_receipts (launch_id TEXT NOT NULL, guild_id TEXT NOT NULL, PRIMARY KEY(launch_id, guild_id))")

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.execute("PRAGMA busy_timeout=30000")
        return db

    def notices(self, user_id: Any) -> bool:
        with self._connect() as db:
            row = db.execute("SELECT notices FROM preferences WHERE user_id=?", (str(user_id),)).fetchone()
        return bool(row[0]) if row else True

    def set_notices(self, user_id: Any, enabled: bool) -> None:
        with self._connect() as db:
            db.execute("INSERT INTO preferences VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET notices=excluded.notices", (str(user_id), int(enabled)))

    def opt_in_channel(self, guild_id: Any, channel_id: Any) -> None:
        """Call only after checking administrator authority for the guild."""
        with self._connect() as db:
            db.execute("INSERT INTO announcement_channels VALUES (?,?) ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id", (str(guild_id), str(channel_id)))

    def claim_announcement(self, launch_id: str, guild_id: Any, channel_id: Any) -> bool:
        """Reserve a one-time delivery for an explicitly authorized channel.

        A crash between claim and send favors no duplicate over automatic retry.
        No scheduler or sender is wired to this method before launch approval.
        """
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            allowed = db.execute("SELECT channel_id FROM announcement_channels WHERE guild_id=?", (str(guild_id),)).fetchone()
            if not allowed or allowed[0] != str(channel_id):
                return False
            result = db.execute("INSERT OR IGNORE INTO announcement_receipts VALUES (?,?)", (launch_id, str(guild_id)))
            return result.rowcount == 1


HELP_COMMAND = {"name": "help", "description": "Show Maxwell commands", "type": 1, "integration_types": [0, 1], "contexts": [0, 1, 2]}
USAGE_COMMAND = {"name": "usage", "description": "Check your daily AI usage and notice preference", "type": 1, "integration_types": [0, 1], "contexts": [0, 1, 2], "options": [{"name": "notices", "description": "Optional Premium notices in usage: on or off", "type": 3, "required": False, "choices": [{"name": "On", "value": "on"}, {"name": "Off", "value": "off"}]}]}
PREMIUM_COMMAND = {"name": "premium", "description": "Future Premium plans (informational preview)", "type": 1, "integration_types": [0, 1], "contexts": [0, 1, 2]}


def help_text(mode: str) -> str:
    lines = ["Maxwell commands: `/maxwell` ask a question · `/help` commands · `/usage` daily AI usage"]
    if mode != "off":
        lines.append("`/premium` future optional plans (informational; unavailable)")
    lines.append("The self-hosted software is free and open source. More commands: `,help` in a server.")
    return "\n".join(lines)


def premium_text(mode: str) -> str:
    if mode == "off":
        return "Maxwell Premium is not available. Maxwell remains free and open source."
    intro = ("Premium launch mode is configured, but verified plans are not connected; subscriptions are unavailable. Proposed plans:\n"
             if mode == "launched" else "Maxwell Premium is not yet available. These are proposals, not purchasable plans:\n")
    return (intro +
            "Free: $0/month. Plus Personal: proposed $3/month per user, with higher personal AI allowances and a more capable model in eligible servers and supported DMs. "
            "Plus Server: proposed $5/month per server, with a larger shared AI allowance and optional server customization. "
            "Allowances, model access, transfer terms and prices require verification before launch. "
            "The software remains free and open source; paid plans would cover optional hosted inference.")


async def _send(interaction: Any, content: str) -> None:
    response = interaction.response
    if not response.is_done():
        await response.send_message(content=content, ephemeral=True)
    else:
        await interaction.followup.send(content=content, ephemeral=True)


async def handle_interaction(bot: Any, interaction: Any) -> bool:
    name = str(ui._interaction_data(interaction).get("name") or "")
    if name not in {"help", "usage", "premium"}:
        return False
    mode = state(bot)
    if name == "help":
        content = help_text(mode)
    elif name == "premium":
        content = premium_text(mode)
    else:
        user_id = getattr(getattr(interaction, "user", None), "id", None)
        if user_id is None:
            await _send(interaction, "Could not identify your Discord account.")
            return True
        store = DiscoveryStore(Path(bot.config.DATA_DIR) / "premium_discovery.sqlite3")
        options = dict(ui._option_pairs(ui._interaction_data(interaction).get("options")))
        choice = options.get("notices")
        if choice in {"on", "off"}:
            store.set_notices(user_id, choice == "on")
        ledger = getattr(bot, "_daily_tokens", None)
        if ledger is None:
            content = "Daily AI usage is unavailable right now."
        else:
            default_limit = int(getattr(bot, "_control", {}).get("daily_user_token_limit", 3_000_000))
            status = ledger.status(str(user_id), default_limit)
            content = (f"Daily AI usage ({status['day']} UTC): {status['spent']:,}/{status['limit']:,} tokens "
                       f"({status['reserved']:,} pending). Resets at 00:00 UTC.")
            if status["exempt"]:
                content += " Your account is exempt from this limit."
        content += f"\nOptional notices in /usage: {'on' if store.notices(user_id) else 'off'}."
        # No verified entitlement catalog is connected. Launch state alone never
        # claims paid allowances or adds a promotional notice.
    await _send(interaction, content)
    return True


def install(bot: Any) -> None:
    # Reconcile command registration on each sync, including a state change back to off.
    ui.USER_INSTALL_COMMANDS[:] = [c for c in ui.USER_INSTALL_COMMANDS if c.get("name") != "premium"]
    ui.USER_INSTALL_NAMES = frozenset(n for n in ui.USER_INSTALL_NAMES if n != "premium")
    if getattr(bot, "_premium_discovery_installed", False):
        if state(bot) != "off":
            ui.register_command(PREMIUM_COMMAND)
        return
    ui.register_command(HELP_COMMAND)
    ui.register_command(USAGE_COMMAND)
    if state(bot) != "off":
        ui.register_command(PREMIUM_COMMAND)
    ui.register_interaction_handler(handle_interaction, priority=11, name="premium_discovery")
    bot._premium_discovery_installed = True
