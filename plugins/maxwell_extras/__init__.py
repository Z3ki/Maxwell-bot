"""Discord extras plugin: rich messages, reminders, media inspection, plugin IDE, and audit UI."""


def setup(bot, ctx):
    from .audit_chunk_tail import install_audit_chunk_tail
    from .audit_progress_fix import install_progress_audit_fix
    from .audit_ui import install_tool_audit
    from .autonomy_routing import install_autonomy_routing_guards
    from .interaction_progress import install_interaction_progress
    from .maxwell_embed_output import install_maxwell_embed_output
    from .owner_control import install_owner_control
    from .rich_interactions import install_rich_interactions
    from .user_install_features import (
        install_user_install_features,
        install_user_install_tool_disclosure,
    )
    from .visible_output_guard import install_visible_output_guard
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

    store = ReminderStore(ctx.store_path("reminders.json"))

    async def _reminder_tick():
        await deliver_due_reminders(bot, store)

    ctx.every(5.0, _reminder_tick, run_immediately=True)

    # Upgrade the personal-app command definitions before on_ready syncs them to
    # Discord, and teach its webhook transport to preserve embeds/views/files.
    install_user_install_features(bot)

    # Official-bot bio cleanup, taint-gate `,confirm` removal, plugin runtime
    # guards, and style-freedom personality wrapping were promoted into the
    # core plugin manager / host. Calling the old installers would wrap the
    # same methods twice.

    # Keep autonomy observation broad but make speech/tool routing fail closed:
    # only configured auto channels are valid guild targets, reply ids must stay
    # in their source room, and visible tools never guess a fallback channel.
    install_autonomy_routing_guards(bot)

    # Image tools remain the same implementations/providers, but their outgoing
    # generated files are intercepted and returned to the model instead of posted.
    patch_image_generators(bot)

    # Record tools that actually executed and attach a persistent disclosure
    # button to normal Discord and personal-app follow-up replies.
    install_tool_audit(bot, ctx)
    install_audit_chunk_tail(bot)
    install_user_install_tool_disclosure(bot)
    install_progress_audit_fix(bot)

    # Discord app commands keep fast answers in the deferred interaction. Tool
    # use or >10s latency promotes it to a stable "working on it…" status and
    # sends the eventual answer as a separate follow-up.
    install_interaction_progress(bot)

    # /maxwell must wrap the progress layer, not sit underneath it. Fast answers
    # are edited into Discord's deferred original response by interaction_progress;
    # converting text to an embed first keeps those fast replies rich too.
    install_maxwell_embed_output(bot)

    # Register the owner panel after the generic interaction-progress wrapper so
    # /owner is intercepted before it starts an AI turn or a slow-response timer.
    install_owner_control(bot)

    # Tool-call turns should not double-post "here's/done/fixed" prose after a
    # file, media, poll, TTS, or rich message already delivered the real result.
    install_visible_output_guard(bot)

    # Rich messages now support stateful callback buttons. The action registry
    # is persistent, clicks are routed through on_interaction back into Maxwell,
    # and the same layer exposes managed persistent views/dynamic items to plugins.
    rich_tool = SendRichMessageTool(bot)
    install_rich_interactions(bot, ctx, rich_tool)

    workbench = PluginWorkbenchTool(bot, ctx)
    return [
        ReminderTool(bot, store),
        rich_tool,
        RecallCrossServerMemoryTool(bot),
        InspectMediaUrlTool(bot),
        workbench,
    ]
