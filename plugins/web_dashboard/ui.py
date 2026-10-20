"""The /dashboard slash command. Replies are ephemeral, always."""

from __future__ import annotations

import logging

import discord

import user_install as ui

from .links import DashboardError

logger = logging.getLogger(__name__)
COMMAND = "dashboard"


def command_definition() -> dict:
    return {
        "name": COMMAND,
        "description": "Open your Maxwell web dashboard (private sign-in link)",
        "type": 1,
        "integration_types": [0, 1],
        "contexts": [0, 1, 2],
    }


def _text(links, link: str) -> str:
    if not links.config.link_mode:
        return "Open the dashboard and sign in with Discord."
    return (
        "Here is your private dashboard link. It signs you in as you, works once and expires in "
        "15 minutes, so don't share it. Settings, files, your public web folder and schedules are "
        "all in there. Reloading the page signs you out; run `/dashboard` again for a new link."
    )


class LinkView(discord.ui.View):
    def __init__(self, url: str):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(label="Open dashboard", style=discord.ButtonStyle.link, url=url))


async def reply_for(bot, links, interaction) -> tuple[str, discord.ui.View | None]:
    allows = getattr(bot, "_dev_scope_allows", None)
    if callable(allows) and not allows(interaction):
        return "The dashboard is unavailable outside the configured development server.", None
    user = getattr(interaction, "user", None)
    try:
        link = await links.link_for(user)
    except DashboardError as exc:
        return str(exc), None
    except Exception:
        logger.exception("dashboard link failed")
        return "Could not make a dashboard link right now. Try again shortly.", None
    return _text(links, link), LinkView(link)


def install(bot, links) -> None:
    ui.register_command(command_definition())

    async def handler(host, interaction):
        if host is not bot:
            return False
        data = interaction.data or {}
        if data.get("name") != COMMAND or data.get("custom_id"):
            return False
        await interaction.response.defer(ephemeral=True)
        text, view = await reply_for(bot, links, interaction)
        kwargs = {"ephemeral": True, "allowed_mentions": discord.AllowedMentions.none()}
        if view is not None:
            kwargs["view"] = view
        await interaction.followup.send(text, **kwargs)
        return True

    ui.register_interaction_handler(handler, priority=5, name=COMMAND)


def uninstall() -> None:
    ui.unregister_command(COMMAND)
    ui.unregister_interaction_handler(COMMAND)
