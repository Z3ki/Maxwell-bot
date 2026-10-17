"""User-initiated, private privacy controls; no model can affirm these choices."""

from __future__ import annotations

import asyncio
import discord

from privacy import VOICE_VERSION, policy_urls, voice_policy
from privacy_data import delete_live_data, deletion_text
from .config_ui import _send, _acknowledge


class VoiceAuthorization(discord.ui.Modal, title="Authorize one voice reference"):
    reference = discord.ui.TextInput(
        label="Exact reference URL (or attachment URL)", max_length=1000
    )
    affirmation = discord.ui.TextInput(
        label="Type I HAVE PERMISSION",
        max_length=30,
        placeholder="I own this voice or the speaker explicitly authorized cloning",
    )

    def __init__(self, panel):
        super().__init__()
        self.panel = panel

    async def on_submit(self, interaction):
        if not await self.panel.authorized(interaction):
            return
        from urllib.parse import urlsplit

        source = urlsplit(str(self.reference).strip())
        if (
            source.scheme not in {"http", "https"}
            or not source.hostname
            or source.username
            or source.password
        ):
            await _send(
                interaction,
                "Use the exact direct reference URL, including the actual attachment URL; a generic filename or attachment label cannot authorize a different recording.",
            )
            return
        if str(self.affirmation).strip() != "I HAVE PERMISSION":
            await _send(
                interaction,
                "No authorization recorded. Only use a voice you own or whose speaker explicitly authorized cloning.",
            )
            return
        await _acknowledge(interaction, thinking=True)
        await asyncio.to_thread(
            self.panel.store.consents.record,
            self.panel.user_id,
            voice_policy(str(self.reference)),
            VOICE_VERSION,
            "voice_authorization",
            affirmative=True,
        )
        await _send(
            interaction,
            "Authorization recorded for this reference. Its audio and generated speech are sent to the configured TTS provider. Retry your request. You can withdraw this authorization in Privacy. A public recording alone is not permission.",
        )


class PrivacySupport(discord.ui.Modal, title="Send private privacy request"):
    request_text = discord.ui.TextInput(
        label="Request to the bot operator (no secrets)",
        style=discord.TextStyle.paragraph,
        max_length=1000,
        placeholder="Access, export, correction, backup or workspace deletion. This will be sent privately to the configured operator.",
    )

    def __init__(self, panel):
        super().__init__()
        self.panel = panel

    async def on_submit(self, interaction):
        if not await self.panel.authorized(interaction):
            return
        await _acknowledge(interaction, thinking=True)
        from tooling.helpers import notify_owner

        result = await notify_owner(
            self.panel.bot,
            kind="privacy",
            title="User-requested private privacy support",
            details=f"Discord user {self.panel.user_id} explicitly requested operator support: {str(self.request_text)}",
        )
        await _send(
            interaction,
            result
            + " Your request is sent only to the configured operator. Any delivered Discord support copy needs operator deletion.",
        )


class PrivacyButton(discord.ui.Button):
    def __init__(self, panel, action, label, *, row=2, danger=False):
        super().__init__(
            label=label,
            row=row,
            style=discord.ButtonStyle.danger
            if danger
            else discord.ButtonStyle.secondary,
        )
        self.panel, self.action = panel, action

    async def callback(self, interaction):
        if not await self.panel.authorized(interaction):
            return
        if self.action == "support":
            await interaction.response.send_modal(PrivacySupport(self.panel))
            return
        if self.action == "voice":
            await interaction.response.send_modal(VoiceAuthorization(self.panel))
            return
        if self.action == "delete":
            view = discord.ui.View(timeout=120)
            view.add_item(
                PrivacyButton(
                    self.panel,
                    "confirm_delete",
                    "Delete my active data",
                    row=0,
                    danger=True,
                )
            )
            await interaction.response.send_message(
                "Delete saved conversations, personal settings, optional consents, BYOK keys and supported active records. "
                "History stays off. Published files/workspaces, provider copies and backups require operator review. "
                "Current requests will be cancelled where possible. This cannot be undone.",
                view=view,
                ephemeral=True,
            )
            return
        await _acknowledge(interaction, thinking=True)
        if self.action == "confirm_delete":
            result = await delete_live_data(self.panel.bot, self.panel.user_id)
            await _send(interaction, deletion_text(result))
            return
        if self.action == "withdraw":
            await asyncio.to_thread(
                self.panel.store.set_default, self.panel.user_id, "tracking", "off"
            )
            await asyncio.to_thread(
                self.panel.store.consents.withdraw, self.panel.user_id
            )
            await _send(
                interaction,
                "Optional authorizations withdrawn; saved history is off. Use Off or Delete my data to remove existing content. Current AI requests still need provider processing.",
            )
            return
        rows = await asyncio.to_thread(
            self.panel.store.consents.review, self.panel.user_id
        )
        text = "Your optional choices (not blanket ToS acceptance):\n" + "\n".join(
            f"• {r['kind']} / {r['policy'][:24]} / {r['version']}: {r['status']} (UTC timestamp {r['recorded_at']:.0f})"
            for r in rows[:12]
        )
        await _send(
            interaction,
            text
            if rows
            else "No optional consent or agreement records are stored for you.",
        )


def add_privacy_controls(panel):
    panel.add_item(PrivacyButton(panel, "review", "Review choices"))
    panel.add_item(PrivacyButton(panel, "support", "Request private support"))
    panel.add_item(PrivacyButton(panel, "withdraw", "Withdraw optional choices"))
    panel.add_item(PrivacyButton(panel, "voice", "Authorize voice reference", row=3))
    panel.add_item(PrivacyButton(panel, "delete", "Delete my data", row=3, danger=True))
    for name, url in policy_urls(getattr(panel.bot, "config", None)).items():
        panel.add_item(discord.ui.Button(label=name.title(), url=url, row=4))
