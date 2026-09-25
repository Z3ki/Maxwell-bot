"""Discord extras plugin: rich messages, reminders, media inspection, and progress UI."""


def setup(bot, ctx):
    from .admin_commands import install_admin_commands
    from .command_suite import install_command_suite
    from .interaction_progress import install_interaction_progress
    from .maxwell_embed_output import install_maxwell_embed_output
    from .rich_interactions import install_rich_interactions
    from .user_install_features import (
        install_user_install_features,
    )

    from .tools import (
        InspectMediaUrlTool,
        RecallCrossServerMemoryTool,
        ReminderStore,
        ReminderTool,
        SendRichMessageTool,
        deliver_due_reminders,
        patch_image_generators,
    )
    from .user_preferences import UserPreferenceStore

    store = ReminderStore(ctx.store_path("reminders.json"))

    async def _reminder_tick():
        await deliver_due_reminders(bot, store)

    ctx.every(5.0, _reminder_tick, run_immediately=True)

    # Upgrade the personal-app command definitions before on_ready syncs them to
    # Discord, and teach its webhook transport to preserve embeds/views/files.
    preferences = UserPreferenceStore(ctx.store_path("user_preferences.json"))
    bot._user_preferences = preferences
    install_user_install_features(bot)
    install_command_suite(bot, preferences)

    # Official-bot bio cleanup, retired `,confirm` command, plugin runtime
    # guards, style-freedom, autonomy routing, and visible-output suppression
    # live in the host. Do not wrap those methods here.

    # Image tools remain the same implementations/providers, but their outgoing
    # generated files are intercepted and returned to the model instead of posted.
    patch_image_generators(bot, ctx)

    # Discord app commands keep fast answers in the deferred interaction. Tool
    # use or >10s latency promotes it to a stable "working on it…" status and
    # sends the eventual answer as a separate follow-up.
    install_interaction_progress(bot, ctx)

    # /maxwell must wrap the progress layer, not sit underneath it. Fast answers
    # are edited into Discord's deferred original response by interaction_progress;
    # converting text to an embed first keeps those fast replies rich too.
    install_maxwell_embed_output(bot)

    # Restricted diagnostics and maintenance are purpose-specific commands;
    # each handler checks the configured Maxwell developer allowlist itself.
    install_admin_commands(bot)

    # Rich messages now support stateful callback buttons. The action registry
    # is persistent, clicks are routed through on_interaction back into Maxwell,
    # and the same layer exposes managed persistent views/dynamic items to plugins.
    rich_tool = SendRichMessageTool(bot)
    install_rich_interactions(bot, ctx, rich_tool)

    return [
        ReminderTool(bot, store),
        rich_tool,
        RecallCrossServerMemoryTool(bot),
        InspectMediaUrlTool(bot),
    ]


def teardown(bot):
    import user_install as ui

    ui.unwrap_session_send("maxwell_embed")
    ui.unwrap_session_send("interaction_progress")
    from .admin_commands import DIAGNOSTICS_COMMAND_NAME, MAINTENANCE_COMMAND_NAME
    from .command_suite import uninstall_command_suite

    ui.unregister_command(DIAGNOSTICS_COMMAND_NAME)
    ui.unregister_command(MAINTENANCE_COMMAND_NAME)
    ui.unregister_interaction_handler("admin_commands")
    uninstall_command_suite(bot)
    ui.unregister_interaction_handler("interaction_progress")
    del bot
