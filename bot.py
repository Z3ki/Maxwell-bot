"""Maxwell Bot - Main entry point"""

from bot_common import *  # noqa: F401,F403

from bot_parts import (
    CoreMixin,
    TypingPresenceMixin,
    AIMixin,
    MemoryMixin,
    RequestPipelineMixin,
    AddressingMixin,
    ReactionsMixin,
    WatchMixin,
    GatewayEventsMixin,
    RecoveryMixin,
    GuildEventsMixin,
    MessageUpdatesMixin,
    MessagesMixin,
    SleepMixin,
    SettingsMixin,
    SitesMixin,
    SendingMixin,
    MediaMixin,
    MediaContextMixin,
    ToolCallsMixin,
    PromptingMixin,
)


class MaxwellBot(
    CoreMixin,
    TypingPresenceMixin,
    AIMixin,
    MemoryMixin,
    RequestPipelineMixin,
    AddressingMixin,
    ReactionsMixin,
    WatchMixin,
    GatewayEventsMixin,
    RecoveryMixin,
    GuildEventsMixin,
    MessageUpdatesMixin,
    MessagesMixin,
    SleepMixin,
    SettingsMixin,
    SitesMixin,
    SendingMixin,
    MediaMixin,
    MediaContextMixin,
    ToolCallsMixin,
    PromptingMixin,
    _BotBase,
):
    """AI-powered Discord bot."""

    def _setup_tools(self):
        """Load bundled and third-party plugins. Tools register themselves."""
        self.hooks = getattr(self.plugin_manager, "hooks", None)
        self.prompts = getattr(self.plugin_manager, "prompts", None)
        self.tool_registry = getattr(self.plugin_manager, "tool_registry", None)
        self._install_core_prompt_components()
        self._publish_core_services()
        try:
            self.plugin_manager.load_plugins()
        except Exception as e:
            logger.error("Failed to load plugins: %s", e)
        # Reasoning now rides inside every tool call. Keep a backfill instance
        # off the model-facing map so a turn with no reasoning still records
        # a stub for the dashboard.
        self._reasoning_backfill = ReasoningLogTool(self)
        _registered = sorted(self.tools.keys())
        logger.info(
            "Registered %d LLM tools via plugins (ENABLE_* gates respected): %s",
            len(_registered),
            ", ".join(_registered),
        )
        errors = getattr(self.plugin_manager, "load_errors", None) or {}
        if errors:
            logger.warning("Plugin load errors: %s", errors)



async def main():
    loop = asyncio.get_running_loop()
    _owner_bot = {"bot": None}

    def _loop_exception_handler(loop_, ctx):
        msg = ctx.get("message", "")
        exc = ctx.get("exception")
        if exc is not None:
            logger.error(
                "unhandled loop exception %s: %s",
                msg,
                exc,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
        else:
            logger.error("unhandled loop error: %s %s", msg, ctx)
        bot = _owner_bot.get("bot")
        if bot is None:
            return
        with contextlib.suppress(Exception):
            bot._track_task(
                asyncio.create_task(
                    notify_owner(
                        bot,
                        kind="error",
                        title="unhandled loop exception",
                        details=str(msg or ""),
                        exc=exc if isinstance(exc, BaseException) else None,
                    ),
                    name="owner-report-loop",
                )
            )

    try:
        loop.set_exception_handler(_loop_exception_handler)
    except Exception:
        pass
    token = configured_bot_token(
        getattr(Config, "DISCORD_BOT_TOKEN", "") or "",
        getattr(Config, "DISCORD_TOKEN", "") or "",
    )
    if not token:
        logger.error(
            "No Discord bot token. Set DISCORD_BOT_TOKEN in .env "
            "(Developer Portal bot token) and restart. "
            "Sleeping 30s so a process manager cannot 401-flood Discord."
        )
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.sleep(30)
        raise SystemExit(2)
    if (getattr(Config, "DISCORD_TOKEN", "") or "").strip() and not (
        getattr(Config, "DISCORD_BOT_TOKEN", "") or ""
    ).strip():
        logger.warning(
            "DISCORD_TOKEN is deprecated; put the official bot token in "
            "DISCORD_BOT_TOKEN. User (self-bot) tokens are not supported."
        )
    bot = MaxwellBot()
    _owner_bot["bot"] = bot
    logger.info("Discord bot token loaded")
    bot._gateway_last_ok = time.monotonic()
    bot._gateway_last_disconnect = None
    _shutdown_called = False
    watchdog_task = asyncio.create_task(
        loop_watchdog(interval=0.1, warning=0.5), name="loop-watchdog"
    )

    async def _gateway_watchdog():
        threshold = float(os.getenv("MAXWELL_GATEWAY_TIMEOUT", "90"))
        interval = 15
        while True:
            await asyncio.sleep(interval)
            if bot.is_closed() or _shutdown_called:
                return
            last_disc = getattr(bot, "_gateway_last_disconnect", None)
            if last_disc is not None and not bot.is_connected():
                down = time.monotonic() - last_disc
                if down > threshold:
                    logger.error(
                        f"gateway down {down:.0f}s > {threshold}s — forcing restart for PM2"
                    )
                    with contextlib.suppress(Exception):
                        await bot.close()
                    await asyncio.sleep(1)
                    with contextlib.suppress(Exception):
                        import os as _os

                        _os._exit(1)
                    return

    gateway_watchdog_task = asyncio.create_task(
        _gateway_watchdog(), name="gateway-watchdog"
    )

    def _request_shutdown(sig):
        nonlocal _shutdown_called
        if _shutdown_called:
            logger.warning(f"Received signal {sig}; shutdown already in progress")
            return
        _shutdown_called = True
        bot._draining = True
        pause = getattr(getattr(bot, "_reply_queue", None), "pause_new", None)
        if callable(pause):
            pause()
        logger.info(
            "Received signal %s, draining in-flight work for up to %.0fs",
            sig,
            drain_timeout_seconds(),
        )
        with contextlib.suppress(RuntimeError):
            _spawn_background(bot._drain_then_close())

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _request_shutdown, sig)

    try:
        await bot.start(token)
    except discord.LoginFailure:
        logger.error(
            "Discord rejected the bot token. Not retrying in a tight "
            "loop — set DISCORD_BOT_TOKEN to an official Developer Portal bot "
            "token (self-bot user tokens are not supported) and restart. "
            "Sleeping 30s so a process manager cannot 401-flood Discord."
        )
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.sleep(30)
        raise SystemExit(2) from None
    except KeyboardInterrupt:
        pass
    finally:
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)
        logger.info("Shutting down Maxwell...")
        # Stop the event-loop watchdog and drain the channel work queues so no
        # handler keeps mutating shared state after the process starts exiting.
        if not watchdog_task.done():
            cancel_once(watchdog_task)
            with contextlib.suppress(asyncio.CancelledError):
                await watchdog_task
        if not gateway_watchdog_task.done():
            cancel_once(gateway_watchdog_task)
            with contextlib.suppress(asyncio.CancelledError):
                await gateway_watchdog_task
        with contextlib.suppress(Exception):
            await bot.channel_queues.close()
        # Flush read positions before the process goes away — a restart is the
        # most common gateway gap, and an unsaved watermark means the messages
        # from the outage are unrecoverable.
        with contextlib.suppress(Exception):
            bot._watermarks.save()
        with contextlib.suppress(Exception):
            await bot._reply_queue.close()
        with contextlib.suppress(Exception):
            pm = getattr(bot, "plugin_manager", None)
            if pm is not None:
                await pm.teardown()
        try:
            await bot.autonomy_engine.stop()
        except Exception as e:
            logger.error(f"Failed to stop autonomy engine: {e}")
        for task in getattr(bot, "_tasks", []):
            cancel_once(task)
            with contextlib.suppress(asyncio.CancelledError):
                await task
        # Cancel in-flight reply tasks so a restart does not leak them.
        def _iter_tasks(task_dict):
            for v in list(task_dict.values()):
                if isinstance(v, asyncio.Task):
                    yield v

        active_requests = getattr(bot, "_active_requests", {}) or {}
        for task in _iter_tasks(active_requests):
            if not task.done():
                cancel_once(task)
        with contextlib.suppress(Exception):
            await asyncio.gather(
                *_iter_tasks(active_requests), return_exceptions=True
            )
        active_requests.clear()
        try:
            await bot.memory.flush()
        except Exception as e:
            logger.error(f"Failed to flush memory on shutdown: {e}")
        try:
            await bot.ai_provider.close()
        except Exception as e:
            logger.error(f"Failed to close AI provider: {e}")
        # Close the separately-built autonomy provider too (it owns its own
        # aiohttp session). Guarded so a missing/never-built provider is fine.
        try:
            ap = getattr(bot, "autonomy_provider", None)
            if ap is not None and hasattr(ap, "close") and ap is not bot.ai_provider:
                await ap.close()
        except Exception as e:
            logger.error(f"Failed to close autonomy provider: {e}")
        try:
            await close_shared_session()
        except Exception as e:
            logger.error(f"Failed to close shared session: {e}")
        try:
            peer = getattr(bot, "_peer", None)
            if peer is not None:
                await peer.close()
        except Exception as e:
            logger.error(f"Failed to close companion Discord client: {e}")
        try:
            await bot.close()
        except Exception as e:
            logger.error(f"Failed to close bot: {e}")
        try:
            await asyncio.to_thread(bot._request_journal.close)
        except Exception as e:
            logger.error(f"Failed to close request journal: {e}")


if __name__ == "__main__":
    asyncio.run(main())
