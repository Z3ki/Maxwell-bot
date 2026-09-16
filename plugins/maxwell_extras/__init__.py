"""Discord extras plugin: rich messages, reminders, media inspection, audit UI, and live plugin development."""

from .audit_progress_fix import install_progress_audit_fix
from .audit_ui import install_tool_audit
from .developer import PluginWorkbenchTool
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

    # Record tools that actually executed and attach a persistent disclosure
    # button to normal Discord replies. Auto web-searches are tagged separately.
    install_tool_audit(bot, ctx)
    install_progress_audit_fix(bot)

    workbench = PluginWorkbenchTool(bot, ctx)
    return [
        ReminderTool(bot, store),
        SendRichMessageTool(bot),
        RecallCrossServerMemoryTool(bot),
        InspectMediaUrlTool(bot),
        workbench,
    ]
