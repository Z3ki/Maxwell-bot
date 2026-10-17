"""Private owner-only manual plan grants; no checkout or billing."""

import asyncio

import discord

from .command_suite import _config_action, _is_application_owner, _send


class PremiumGrantModal(discord.ui.Modal):
    def __init__(self, panel):
        super().__init__(title="Grant or revoke Premium", timeout=180)
        self.panel = panel
        self._config_context = (panel.scope, panel.selected_key)
        self.user_id = discord.ui.TextInput(
            label="Discord user ID", min_length=1, max_length=20, required=True
        )
        self.plan = discord.ui.TextInput(
            label="Plan: premium or free",
            default="premium",
            max_length=7,
            required=True,
        )
        self.add_item(self.user_id)
        self.add_item(self.plan)

    @_config_action(thinking=True)
    async def on_submit(self, interaction):
        if not await self.panel.authorized(interaction):
            return
        if not _is_application_owner(self.panel.bot, interaction):
            await _send(
                interaction, "Only the configured application owner can grant Premium."
            )
            return
        uid = str(self.user_id.value).strip()
        plan = str(self.plan.value).strip().lower()
        if (
            not uid.isascii()
            or not uid.isdecimal()
            or not 1 <= int(uid) < 2**64
            or plan not in {"premium", "free"}
        ):
            await _send(
                interaction, "Enter a valid Discord user ID and premium or free."
            )
            return
        ledger = getattr(self.panel.bot, "_message_quota", None)
        if ledger is None:
            await _send(
                interaction, "Plan storage is unavailable. No grant was changed."
            )
            return
        try:
            await asyncio.to_thread(ledger.configure, str(int(uid)), plan=plan)
        except Exception:
            await _send(
                interaction,
                "Plan update could not be confirmed. Review it before retrying.",
            )
            return
        await _send(
            interaction,
            f"User `{int(uid)}` now has the **{plan}** plan. No payment was taken. Custom message limits remain in effect.",
        )


class PremiumGrantButton(discord.ui.Button):
    def __init__(self, panel):
        super().__init__(
            label="Grant / revoke Premium", style=discord.ButtonStyle.secondary, row=3
        )
        self.panel = panel

    async def callback(self, interaction):
        if not await self.panel.authorized(interaction):
            return
        if (
            self.panel.closed
            or self.panel.is_finished()
            or self.panel.scope != "owner"
            or self.panel.selected_key != "overview"
        ):
            await _send(
                interaction, "Open the current owner controls to manage Premium."
            )
            return
        if not _is_application_owner(self.panel.bot, interaction):
            await _send(
                interaction,
                "Premium grants are restricted to the configured application owner.",
            )
            return
        await interaction.response.send_modal(PremiumGrantModal(self.panel))
