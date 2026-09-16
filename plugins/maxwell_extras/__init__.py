"""Discord extras plugin: rich messages, reminders, media inspection, plugin IDE, and audit UI."""

from .audit_progress_fix import install_progress_audit_fix
from .audit_ui import install_tool_audit
from .interaction_progress import install_interaction_progress
from .plugin_runtime import install_plugin_runtime_guards
from .user_install_features import (
    install_user_install_features,
    install_user_install_tool_disclosure,
)
from .workbench_v2 import PluginWorkbenchTool
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

    # Upgrade the personal-app command definitions before on_ready syncs them to
    # Discord, and teach its webhook transport to preserve embeds/views/files.
    install_user_install_features(bot)

    # Strengthen every plugin, not just this one: bounded event/job callbacks,
    # runtime health counters, and tracked background tasks cancelled on reload.
    install_plugin_runtime_guards(bot)

    # Image tools remain the same implementations/providers, but their outgoing
    # generated files are intercepted and returned to the model instead of posted.
    patch_image_generators(bot)

    # Record tools that actually executed and attach a persistent disclosure
    # button to normal Discord and personal-app follow-up replies.
    install_tool_audit(bot, ctx)
    install_user_install_tool_disclosure(bot)
    install_progress_audit_fix(bot)

    # Discord app commands keep fast answers in the deferred interaction. Tool
    # use or >5s latency promotes it to a stable "working on it…" status and
    # sends the eventual answer as a separate follow-up.
    install_interaction_progress(bot)

    workbench = PluginWorkbenchTool(bot, ctx)
    return [
        ReminderTool(bot, store),
        SendRichMessageTool(bot),
        RecallCrossServerMemoryTool(bot),
        InspectMediaUrlTool(bot),
        workbench,
    ]
