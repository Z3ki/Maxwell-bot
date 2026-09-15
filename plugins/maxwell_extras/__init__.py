"""Discord extras plugin: rich messages, reminders, media inspection, and explicit recall."""

from .tools import (
    InspectMediaUrlTool,
    RecallCrossServerMemoryTool,
    ReminderStore,
    ReminderTool,
    SendRichMessageTool,
    deliver_due_reminders,
    patch_image_generators,
)


def setup(bot, ctx):
    store = ReminderStore(ctx.store_path("reminders.json"))

    async def _reminder_tick():
        await deliver_due_reminders(bot, store)

    ctx.every(5.0, _reminder_tick, run_immediately=True)

    # Image tools remain the same implementations/providers, but their outgoing
    # generated files are intercepted and returned to the model instead of posted.
    patch_image_generators(bot)

    return [
        ReminderTool(bot, store),
        SendRichMessageTool(bot),
        RecallCrossServerMemoryTool(bot),
        InspectMediaUrlTool(bot),
    ]
