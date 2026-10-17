"""Private job review and cancellation without relying on an old webhook token."""

import discord

from .config_ui import _acknowledge, _send
from .task_jobs import format_job


def context_ids(interaction):
    cid = str(
        getattr(interaction, "channel_id", None)
        or getattr(getattr(interaction, "channel", None), "id", "")
    )
    gid = str(
        getattr(interaction, "guild_id", None)
        or getattr(getattr(interaction, "guild", None), "id", "")
        or ""
    )
    return cid, gid


class JobButton(discord.ui.Button):
    def __init__(self, manager, uid, action, job_id=None):
        super().__init__(
            label=f"Cancel {job_id}" if action == "cancel" else "Refresh tasks",
            style=discord.ButtonStyle.danger
            if action == "cancel"
            else discord.ButtonStyle.secondary,
        )
        self.manager, self.uid, self.action, self.job_id = (
            manager,
            str(uid),
            action,
            job_id,
        )

    async def callback(self, interaction):
        if str(interaction.user.id) != self.uid:
            await _send(
                interaction,
                "Only the person who opened these tasks can use this control.",
            )
            return
        await _acknowledge(interaction, thinking=True)
        if self.action == "cancel":
            count = await self.manager.cancel(self.uid, self.job_id)
            await _send(
                interaction,
                f"Stopped {count} worker job(s). Partial changes may remain; inspect before retrying.",
            )
        else:
            await show_tasks(self.manager, interaction, self.uid)


async def show_tasks(manager, interaction, uid, job_id=None):
    rows = await manager.inspect(uid, job_id=job_id)
    text = (
        "\n\n".join(format_job(r, limit=1400 if job_id else 350) for r in rows[:5])
        or "You have no saved worker job metadata. Detailed reports expire from memory after one hour or restart."
    )
    view = discord.ui.View(timeout=300)
    view.add_item(JobButton(manager, uid, "status"))
    for row in rows[:4]:
        if row["state"] in {"queued", "running", "cancelling"}:
            view.add_item(JobButton(manager, uid, "cancel", row["id"]))
    await interaction.followup.send(
        content=text[:1900],
        view=view,
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


async def handle_tasks(bot, interaction):
    import user_install as ui

    data = ui._interaction_data(interaction)
    if data.get("name") != "tasks":
        return False
    await _acknowledge(interaction, thinking=True)
    manager = getattr(bot, "_task_jobs", None)
    if manager is None:
        await _send(interaction, "Worker task management is unavailable.")
        return True
    uid = str(interaction.user.id)
    options = dict(ui._option_pairs(data.get("options")))
    if options.get("action") == "cancel":
        job_id = str(options.get("job_id") or "")
        if not job_id:
            await _send(
                interaction,
                "Choose a job ID to cancel, or use /cancel to stop your jobs in this conversation.",
            )
            return True
        count = await manager.cancel(uid, job_id)
        await _send(
            interaction, f"Stopped {count} worker job(s). Partial changes may remain."
        )
    else:
        await show_tasks(manager, interaction, uid, options.get("job_id"))
    return True
