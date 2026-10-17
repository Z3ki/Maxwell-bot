"""Gateway lifecycle events: ready/resume/disconnect and slash-command sync.

Split out of bot.py; methods keep their original bodies.
"""

import bot_common as _common

# Mechanical split: the methods below were written against bot.py's
# module globals. Bind every shared name so the bodies run unchanged.
for _name in dir(_common):
    if _name.startswith("__"):
        continue
    globals().setdefault(_name, getattr(_common, _name))
del _name

class GatewayEventsMixin:

    async def on_error(self, event, *args, **kwargs):
        logger.exception("discord event %s failed", event)
        exc = sys.exc_info()[1]
        msg = None
        for arg in args:
            if getattr(arg, "channel", None) is not None and getattr(
                arg, "author", None
            ) is not None:
                msg = arg
                break
        with contextlib.suppress(Exception):
            await notify_owner(
                self,
                kind="error",
                title=f"discord event {event} failed",
                message=msg,
                exc=exc if isinstance(exc, BaseException) else None,
            )

    def _schedule_recovery(self) -> None:
        """One missed-message scan at a time, including after a shard resume."""
        current = getattr(self, "_recovery_task", None)
        if isinstance(current, asyncio.Task) and not current.done():
            return
        try:
            task = asyncio.create_task(
                self._recover_missed_messages(), name="missed-recovery"
            )
        except RuntimeError:
            return
        self._recovery_task = task
        if getattr(self, "_tasks", None) is not None:
            self._track_task(task)

    def _schedule_shard_resync(self, shard_id: int) -> None:
        tasks = getattr(self, "_shard_resync_tasks", None)
        if not isinstance(tasks, dict):
            tasks = {}
            self._shard_resync_tasks = tasks
        current = tasks.get(int(shard_id))
        if isinstance(current, asyncio.Task) and not current.done():
            return
        try:
            task = asyncio.create_task(
                self._resync_shard_state(int(shard_id)),
                name=f"shard-resync-{int(shard_id)}",
            )
        except RuntimeError:
            return
        tasks[int(shard_id)] = task
        if getattr(self, "_tasks", None) is not None:
            self._track_task(task)

    async def _resync_shard_state(self, shard_id: int) -> None:
        """Refresh saved guild state for one shard without starting workers."""
        logger.info("Resyncing Discord state for shard %s", shard_id)
        await self._save_discord_state()

    async def on_disconnect(self):
        self._capture_recovery_snapshot()
        logger.warning("discord gateway disconnected")
        self._gateway_last_disconnect = time.monotonic()

    async def on_resumed(self):
        """Reconcile tracked history after resume without assuming replay failed."""
        self._capture_recovery_snapshot()
        logger.info("discord gateway resumed; checking for missed messages")
        self._gateway_last_disconnect = None
        self._gateway_last_ok = time.monotonic()
        self._schedule_recovery()
        ws = getattr(self, "ws", None)
        shard_id = getattr(ws, "shard_id", None) if ws is not None else None
        self._schedule_shard_resync(0 if shard_id is None else int(shard_id))

    async def on_shard_ready(self, shard_id: int):
        logger.info("discord shard %s ready", shard_id)
        self._gateway_last_ok = time.monotonic()
        self._schedule_shard_resync(int(shard_id))

    async def on_shard_resumed(self, shard_id: int):
        logger.info("discord shard %s resumed", shard_id)
        self._gateway_last_ok = time.monotonic()
        self._gateway_last_disconnect = None
        self._schedule_recovery()
        self._schedule_shard_resync(int(shard_id))

    async def on_ready(self):
        self._capture_recovery_snapshot()
        self._gateway_last_ok = time.monotonic()
        self._gateway_last_disconnect = None
        if self.user:
            # BOT_NAME is the identity. Do not replace it with the Discord
            # account's global display name.
            configured = str(getattr(self.config, "BOT_NAME", "") or "").strip()
            self.bot_name = configured or self.user.display_name
            self._register_self_user(self.user)
            ident = getattr(self, "_identity", None)
            if isinstance(ident, dict):
                uid = str(self.user.id)
                ident["self_id"] = uid
                ident["self_id_paren"] = f" (ID {uid})" if uid else ""
                self._base_knowledge = fill_identity(MAXWELL_BASE_KNOWLEDGE, ident)
                self._discord_chat_protocol = fill_identity(DISCORD_CHAT_PROTOCOL, ident)
            if not bool(getattr(self.user, "bot", False)):
                logger.error(
                    "This token belongs to a user account. Maxwell is an official "
                    "Discord bot only — create an application at "
                    "https://discord.com/developers/applications and set "
                    "DISCORD_BOT_TOKEN."
                )
                await self.close()
                return
            logger.info(
                "Logged in as %s (%s) [bot account]",
                self.bot_name,
                self.user.id,
            )
        logger.info(f"Connected to {len(self.guilds)} guilds")
        self._load_emojis()
        from discord_expressions import start_expression_refresh
        start_expression_refresh(self)
        await self._save_discord_state()
        if self._sleep_window_active():
            await self._apply_sleep_presence(asleep=True)
        else:
            current = getattr(self, "status", None)
            if current is not None:
                self._current_status = current
        # A fresh ready event can follow a restart or non-resumable session.
        # Reconcile history to find any messages missing from durable receipt.
        # Plugin on_ready and slash sync run once, not on every shard.
        self._schedule_recovery()
        await self._arm_background_workers()

    async def on_interaction(self, interaction):
        if getattr(self, "_draining", False):
            return
        if not MaxwellBot._dev_scope_allows(self, interaction):
            return
        self._dispatch_plugin_event("on_interaction", interaction)
        try:
            if await handle_user_install_interaction(self, interaction):
                return
        except Exception:
            logger.exception("user-install interaction failed")
        parent = getattr(super(), "on_interaction", None)
        if parent is not None:
            await parent(interaction)

    async def _on_tree_error(self, interaction, error) -> None:
        """Keep expected CommandNotFound routing noise out of the error log.

        User-install commands ('Ask Maxwell', /maxwell, /config, ...) sync
        via raw REST and dispatch through on_interaction. The parallel
        CommandTree dispatch raises CommandNotFound for every one of them;
        that is routing, not a fault. Real command failures still log.
        """
        from discord import app_commands

        if isinstance(error, app_commands.CommandNotFound):
            logger.debug(
                "Command tree skipped %r (dispatched via on_interaction)",
                getattr(error, "command_name", None) or getattr(error, "name", None),
            )
            return
        logger.error("Ignoring exception in command tree", exc_info=error)

    async def _sync_slash_commands(self) -> None:
        """Register slash commands globally, or only in configured Dev guilds."""
        token = (
            getattr(getattr(self, "http", None), "token", None)
            or getattr(getattr(self, "config", None), "DISCORD_BOT_TOKEN", "")
            or getattr(getattr(self, "config", None), "DISCORD_TOKEN", "")
        )
        if not token:
            return
        dev_mode = bool(getattr(self.config, "MAXWELL_DEV_MODE", False))
        guild_ids = (
            sorted(int(gid) for gid in self.config.MAXWELL_DEV_GUILD_IDS)
            if dev_mode
            else [
                int(guild.id)
                for guild in (getattr(self, "guilds", []) or [])
                if getattr(guild, "id", None) is not None
            ]
        )
        app_id = getattr(self, "application_id", None) or getattr(
            getattr(self, "user", None), "id", None
        )
        try:
            if not dev_mode:
                await ensure_user_install_context(str(token))
            synced = await sync_application_commands(
                str(token),
                USER_INSTALL_COMMANDS,
                application_id=app_id,
                guild_ids=guild_ids,
                guild_only=dev_mode,
            )
        except Exception:
            logger.exception("Failed to sync slash commands")
            return
        logger.info(
            "Slash commands synced: global=%s guild=%s",
            synced.get("global", 0),
            synced.get("guild", 0),
        )
