"""Private custom-agent configuration with no legal or unsolicited prompts."""

import asyncio
import time

import discord

from .config_ui import _config_action, _send
from .task_workers import has_premium


class AgentProfileModal(discord.ui.Modal):
    def __init__(self, panel):
        super().__init__(title="Manage custom agent", timeout=180)
        self.panel = panel
        self._config_context = (panel.scope, panel.selected_key)
        self.started_at = time.time()
        self.agent_id = discord.ui.TextInput(
            label="Agent ID", placeholder="website-reviewer", max_length=32
        )
        self.kind = discord.ui.TextInput(
            label="Scope",
            placeholder="shell, website, research or custom",
            default="custom",
            max_length=8,
            required=False,
        )
        self.instructions = discord.ui.TextInput(
            label="Instructions (save only; no credentials)",
            style=discord.TextStyle.paragraph,
            max_length=4000,
            required=False,
        )
        self.tools = discord.ui.TextInput(
            label="Tool permissions (comma-separated; optional)",
            placeholder="shell, edit_site, list_sites",
            max_length=180,
            required=False,
        )
        self.action = discord.ui.TextInput(
            label="Action: save or delete", default="save", max_length=6
        )
        for field in (
            self.agent_id,
            self.kind,
            self.instructions,
            self.tools,
            self.action,
        ):
            self.add_item(field)

    @_config_action(thinking=True)
    async def on_submit(self, interaction):
        store = getattr(self.panel.bot, "_task_agents", None)
        if store is None:
            await _send(
                interaction, "Agent storage is unavailable. No change was saved."
            )
            return
        name = str(self.agent_id.value).strip()
        action = str(self.action.value).strip().lower()
        try:
            if action == "delete":
                count = await asyncio.to_thread(store.delete, self.panel.user_id, name)
                notice = f"Deleted {count} profile(s) matching {name}."
            elif action == "save":
                if not await asyncio.to_thread(
                    has_premium, self.panel.bot, self.panel.user_id
                ):
                    await _send(
                        interaction,
                        "Saving agents requires Premium. Existing profiles can still be reviewed or deleted.",
                    )
                    return
                tools = [
                    part.strip()
                    for part in str(self.tools.value).split(",")
                    if part.strip()
                ] or None
                await asyncio.to_thread(
                    store.save,
                    self.panel.user_id,
                    name,
                    str(self.kind.value).strip().lower(),
                    str(self.instructions.value),
                    tools=tools,
                    started_at=self.started_at,
                )
                notice = f"Saved {name}. This optional profile uses your AI connection and current permissions."
            else:
                raise ValueError("Choose save or delete")
            await self.panel._refresh_personal()
        except ValueError as exc:
            await _send(interaction, str(exc))
            return
        except Exception:
            await _send(
                interaction,
                "Agent change could not be confirmed. Review your profiles before retrying.",
            )
            return
        self.panel.notice = notice
        self.panel._build()
        await _send(interaction, notice)
        try:
            await self.panel.command_interaction.edit_original_response(
                content=None,
                embed=self.panel.embed(),
                view=self.panel,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            pass  # The durable result was already reported privately.


class AgentProfileButton(discord.ui.Button):
    def __init__(self, panel):
        super().__init__(label="Manage agent", row=1)
        self.panel = panel

    async def callback(self, interaction):
        if not await self.panel.authorized(interaction):
            return
        if (
            self.panel.closed
            or self.panel.is_finished()
            or (self.panel.scope, self.panel.selected_key) != ("personal", "agents")
        ):
            await _send(interaction, "Open the current Custom agents screen first.")
            return
        await interaction.response.send_modal(AgentProfileModal(self.panel))
