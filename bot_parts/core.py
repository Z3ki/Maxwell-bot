"""Conversation memory storage, memory cleanup loops and emoji helpers.

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

class CoreMixin:
    def __init__(self, *, account_kind: str = "bot", planned_kinds: set[str] | None = None):
        self.account_kind = "bot"
        self._planned_kinds = {"bot"}
        if planned_kinds:
            self._planned_kinds.update(
                str(x).strip().lower() for x in planned_kinds if str(x).strip()
            )
        self._peer = None
        self._peer_token = None
        self._peer_kind = None
        self._account_ids: set[int] = set()
        init_kwargs = {
            "command_prefix": ",",
            "help_command": None,
            "chunk_guilds_at_startup": True,
            "allowed_mentions": discord.AllowedMentions(
                everyone=False, roles=False, users=False, replied_user=True
            ),
        }
        intents = bot_intents()
        if intents is not None:
            init_kwargs["intents"] = intents
        if _BotBase is not commands.Bot:
            shard_kwargs = shard_launch_kwargs()
            init_kwargs.update(shard_kwargs)
            if shard_kwargs:
                logger.info("Discord sharding: %s", shard_kwargs)
            else:
                logger.info("Discord sharding: automatic")
        try:
            super().__init__(**init_kwargs)
        except TypeError:
            init_kwargs.pop("chunk_guilds_at_startup", None)
            try:
                super().__init__(**init_kwargs)
            except TypeError:
                init_kwargs.pop("intents", None)
                super().__init__(**init_kwargs)
        self.config = Config()
        ident = identity_values(self.config)
        self._identity = ident
        self.command_prefix = ident["command_prefix"] or self.config.COMMAND_PREFIX
        self._base_knowledge = fill_identity(MAXWELL_BASE_KNOWLEDGE, ident)
        self._discord_chat_protocol = fill_identity(DISCORD_CHAT_PROTOCOL, ident)
        self._BIRTHDAY = parse_birthday(getattr(self.config, "BOT_BIRTHDAY", None))
        self.config.validate()
        self.bot_name = ident["bot_name"]
        self.ai_provider: Any = None
        self.memory: Any = None
        self.tools = {}
        self.plugin_manager = PluginManager(self)
        # Bounded: a plain dict here kept one Lock alive per channel the bot
        # had ever seen, which across a few hundred servers only ever grows.
        #
        # The channel lock now guards ONLY the short memory/bookkeeping
        # section of on_message. Reply generation is serialized by
        # self._reply_queue instead. Holding this lock across the whole turn
        # is what produced 586 dropped messages in four days: _handle_message
        # legitimately runs for minutes on an image or a site build, so the
        # 15s acquire in on_message timed out constantly and the message was
        # thrown away.
        self._channel_locks = KeyedLocks(max_idle=256)
        # Conversations retain their order; unrelated channels start immediately.
        # There is no process-wide admission cap or shared reply waiting list.
        self._reply_queue = ReplyQueue(
            max_directed=0,
            max_outstanding=0,
            max_age=300.0,
            on_drop=self._on_reply_queue_drop,
        )
        # One reply per message id. Discord redelivers MESSAGE_CREATE after a
        # gateway resume; without this that is a second full reply, which
        # looks exactly like the bot spamming.
        self._inbound_dedup = InboundDedup(capacity=4096)
        # Per-channel high-water message id so a reconnect can fetch what the
        # gateway never delivered. discord.py does not surface a gap.
        self._watermarks = Watermarks(
            os.path.join(self.config.DATA_DIR, "watermarks.json")
            if getattr(self.config, "DATA_DIR", "")
            else "data/watermarks.json"
        )
        self._request_journal = RequestJournal(
            os.path.join(self.config.DATA_DIR or "data", "inbound_requests.sqlite3")
        )
        self._inbound_processing: set[str] = set()
        self._recovery_cursors: dict[str, int] = {}
        self._recovery_lock = asyncio.Lock()
        # Rows journaled before this process started are leftover from an
        # outage. They are not live conversation and must not get a reply.
        self._process_started_at = time.time()
        # Channels the bot is currently generating a reply for (in-flight).
        # Autonomy reads this to avoid posting into a channel mid-reply, which
        # would race the real reply and produce a duplicate/odd message.
        self._replying_channels: set[str] = set()
        # Channels the bot most recently replied in -> timestamp. Autonomy reads
        # this to avoid re-engaging a conversation the bot already answered (the
        # "bot sees its own 15-min-old reply and posts again" loop).
        self._last_bot_reply: dict[str, float] = {}
        # Channel id -> monotonic timestamp of the bot's most recent successful
        # send (or reply). The slowmode handler uses this to compute how long
        # to wait before posting so we don't race Discord's per-channel
        # slowmode timer and get rate-limited (429) on a busy channel.
        self._last_bot_send: dict[str, float] = {}
        # Track calls for diagnostics without limiting or queueing inference.
        # Active calls: asyncio.Task -> "user" | "background"
        self._ai_call_kind: dict[asyncio.Task, str] = {}
        # Cache of recent users seen in each channel's conversation, so we can
        # resolve mentions/IDs for pinging even if not in current message.mentions
        # or guild cache (common for self-bots in larger servers).
        self._recent_users: dict[
            str, dict[str, str]
        ] = {}  # channel_id -> {user_id: name}
        self._custom_status = None
        self._current_game = None
        # Intended Discord status (online/idle/dnd/invisible). Sleep
        # overlays discord.Status.idle on top of this without clobbering
        # it, then change_presence restores _current_status on wake.
        self._current_status = discord.Status.online
        self._sleep_wake_task: asyncio.Task | None = None
        self._sleep_presence_overlay = False
        self._cooldowns: dict[str, float] = {}
        # channel_id -> monotonic expiry. After a real exchange, the whole
        # room stays on watch so a follow-up does not need another @.
        self._conversation_watch: dict[str, float] = {}
        # channel_id -> watch_policy.WatchState. How each room is actually
        # going: its pace, when he last spoke, whether anyone is answering
        # him. The watch window and the debounce are derived from this rather
        # than being the same two constants for every room.
        self._watch_states: dict[str, Any] = {}
        # channel_id -> pending watch follow-up. Wait a beat so a burst of
        # lines becomes one reply instead of one LLM turn per message.
        self._watch_debounce: dict[str, dict] = {}
        # channel_id -> user_id -> {name, expires_at}. Discord TYPING_START
        # lasts ~10s and refreshes while they keep composing. Live replies
        # and autonomy read this so Maxwell can wait instead of talking over
        # someone mid-sentence.
        self._typing_users: dict[str, dict[str, dict]] = {}
        self._active_requests: dict[str, asyncio.Task] = {}
        self._active_request_user: dict[str, str] = {}
        # Shutdown stops new work, then waits for turns already running.
        self._draining = False
        self._background_workers_armed = False
        self._inflight_counts: dict[str, int] = {}
        self._shard_resync_tasks: dict[int, asyncio.Task] = {}
        self._recovery_task: asyncio.Task | None = None
        self._hydration: dict[str, dict] = {}
        # discord.Message uses slots, so per-message admission flags must live
        # on the bot rather than being attached to Discord model objects.
        self._same_user_interrupts: dict[str, None] = {}
        self._same_user_latest: dict[str, int] = {}
        # Per-channel current progress. Under load many channels can
        # have tool batches in flight concurrently; a single bot-wide
        # attribute would let channel B's run_one clobber channel A's,
        # causing the wrong progress to be marked streaming or stopped
        # when channel A's tool posts output. Old code used a single
        # ``self._current_progress``; see _process_native_tool_calls
        # ``run_one`` for the per-channel keying.
        self._current_progress_by_channel: dict[str, Any] = {}
        # One live `$ cmd` Discord message per user-message turn. Edited in
        # place for later shell calls on that turn; left in the channel when
        # the turn ends. The next user message posts a new one.
        self._stop_until: dict[str, float] = {}
        self._drugged_until: dict[str, float] = {}
        # Global sleep state. The bot is one entity — at most one sleep
        # window at a time. _sleep_until is the wake-at monotonic
        # timestamp; 0 means not sleeping. Set by the `sleep` tool or
        # the restricted sleep slash command, max 60 minutes.
        # 2026-07-19: added because the bot kept spamming goodbye/goodnight
        # in chat; a real sleep window gives the model an actual off-switch
        # and a way to communicate 'not now' without it being a one-off
        # signoff that confuses the next conversation.
        self._sleep_until: float = 0.0
        # Per-user dedup so the same person pinging during a sleep window
        # only gets ONE 'max is sleeping' notification, not one per message.
        # user_id -> monotonic timestamp of the last notification (used
        # to re-notify if sleep is long enough that 30 min have passed).
        self._sleep_notified_at: dict[str, float] = {}
        self._sites: dict[str, dict] = {}
        self._sites_mtime = 0.0
        self._auto_channels: set[str] = set()
        # Per-server progress-message setting. A server id in this set means live
        # 'thinking: …' tool-progress messages are shown in that server's
        # channels. The global default is configured by MAXWELL_PROGRESS_MESSAGES.
        # DMs never get progress messages. The MAXWELL_PROGRESS_MESSAGES
        # env var, when true, enables the feature for ALL servers as a
        # baseline so a fresh install can opt in globally without running
        # `/progress arguments:on` in every server; `/progress arguments:off` wins per
        # server (tracked in _progress_servers_off) so an admin can quiet
        # a noisy server even under the env baseline.
        self._progress_servers: set[str] = set()
        self._progress_servers_off: set[str] = set()
        # Per-server opt-in for the auto line in new ticket/support channels.
        # Off everywhere until a server admin enables ticket greetings.
        self._ticket_greeting_servers: set[str] = set()
        self._blacklist: set[str] = set()
        self._shell_whitelist: set[str] = set()
        self._admins: set[str] = configured_admin_ids(self.config)
        self._guild_emojis: dict[str, dict[str, str]] = {}
        self._guild_stickers: dict[str, dict[str, str]] = {}
        self._application_emojis: dict[str, str] = {}
        self._standard_stickers: dict[str, Any] = {}
        self._expression_refresh_task: asyncio.Task | None = None
        self._media_context: dict[str, list[dict]] = {}
        # Latest Discord message snapshots keyed by message id. MESSAGE_UPDATE
        # can arrive after the original turn has started, so keep the newest
        # object around for reply-parent/context refreshes without starting a
        # second answer.
        self._message_snapshots: dict[str, Any] = {}
        # Per-message edit fingerprints suppress duplicate cached+raw update
        # events and repeated identical embed mutations. The value is
        # (message_fingerprint, media_fingerprint, monotonic_timestamp).
        self._message_update_state: dict[str, tuple[str, str, float]] = {}
        # In-flight turns use this to notice an embed/media refresh before the
        # provider request is sent. Updates after generation starts are still
        # retained in _message_snapshots and visual memory for the next turn.
        self._inflight_context: dict[str, dict[str, Any]] = {}
        # channel_id -> emoji-grid cache key already shown there. Keyed by the
        # grid's content hash, so a guild adding an emoji re-shows the sheet
        # instead of Maxwell running on a stale one.
        self._emoji_grid_shown: dict[str, str] = {}
        # Indirect-prompt-injection defense. When the model has just read
        # content from a less-trusted source (fetch_url, web_search, a URL in
        # a user message, etc.), we mark the current message as "tainted" so
        # the destructive shell tool requires explicit user
        # confirmation before running. Taint is cleared on every new user
        # message so it's strictly per-turn: a clean follow-up resets the flag.
        # `message_id -> bool` lets us be precise when multiple replies are
        # in flight on different channels.
        # message id -> when it was tainted. Bounded: nothing ever removed an
        # id (a turn clears its own message, which was never in here), so a
        # process that ran for months grew one entry per tainted turn forever.
        self._tainted_messages: dict[str, float] = {}
        # Out-of-band user confirmation for destructive tools on tainted turns.
        # author_id -> monotonic timestamp of a confirmation event. Consumed
        # (one-shot) by the destructive-tool gate in _execute_tool_by_name, and
        # expired after _CONFIRM_TTL_SECONDS. This is the ONLY legitimate source
        # of `_confirmed=True` — model-supplied `_confirmed` is stripped in the
        # dispatcher so the model can no longer self-confirm.
        self._destructive_confirm: dict[str, float] = {}
        self._control = dict(DEFAULT_CONTROL)
        # 2026-07-22: progress messages are now per-server (see
        # self._progress_servers + _progress_enabled). The old global
        # self._control["progress_messages"] flag is gone — keeping a stale
        # value here would have made every read site below default to the
        # global state instead of the per-server set.
        self._control_mtime = 0
        self._reaction_seen: set[str] = (
            set()
        )  # unused leftover; reactions are context now
        self._reaction_seen_order: list[str] = []
        self._message_reactions: dict[str, list[dict]] = {}
        self._message_reactions_order: list[str] = []
        # Fire-and-forget presence/housekeeping tasks. The loop keeps only a
        # weak reference to a running task, so anything not held here can be
        # garbage-collected mid-await and silently never finish.
        self._detached_tasks: set[asyncio.Task] = set()
        self._trace_lock = asyncio.Lock()
        self._tasks: list[Any] = []
        # Last time we swept the task list for completed entries. Without this
        # the list grew unboundedly with every provider churn / reinit.
        self._last_task_sweep: float = 0.0
        self._tool_breaker = ToolCircuitBreaker(
            failure_threshold=5, recovery_seconds=30
        )
        self._message_quota = MessageQuota(Path(self.config.DATA_DIR) / "message_quota.sqlite3")
        install_usage_commands(self)
        # Per-conversation work ordering and the event-loop watchdog remain
        # independent of global request admission.
        self.channel_queues: ChannelWorkQueues = ChannelWorkQueues()
        self._setup_ai()
        self._setup_memory()
        self._setup_tools()

    def _update_recent_users(self, channel_id: str, user: Any):
        """Track users seen in this channel's conversation so render can resolve
        mentions for pinging (guild.get_member often misses them in self-bots).
        """
        if not user:
            return
        cid = str(channel_id)
        uid = str(getattr(user, "id", ""))
        if not uid:
            return
        name = getattr(user, "display_name", None) or getattr(user, "name", uid)
        if cid not in self._recent_users:
            self._recent_users[cid] = {}
        self._recent_users[cid][uid] = name

    def _cached_user_display_name(self, uid: str, *, guild=None) -> str | None:
        """Best local name for a user id: guild nick, cache, then recent rooms."""
        uid = str(uid or "").strip()
        if not uid:
            return None
        if guild is not None:
            with contextlib.suppress(Exception):
                member = guild.get_member(int(uid))
                if member is not None:
                    name = getattr(member, "display_name", None) or getattr(
                        member, "name", None
                    )
                    if name:
                        return str(name)
        with contextlib.suppress(Exception):
            user = self.get_user(int(uid))
            if user is not None:
                name = getattr(user, "display_name", None) or getattr(
                    user, "name", None
                )
                if name:
                    return str(name)
        for names in (getattr(self, "_recent_users", None) or {}).values():
            if not isinstance(names, dict):
                continue
            name = names.get(uid)
            if name:
                return str(name)
        return None

    _TYPING_TTL_SECONDS = 10.0

    def _track_task(self, task: Any) -> Any:
        """Add a fire-and-forget task to self._tasks, periodically sweeping
        completed entries to keep the list bounded.

        The naive pattern (always append) leaks one slot per provider churn /
        reinit / config toggle. Sweep at most every 60s to amortize the cost.
        """
        import time as _time

        self._tasks.append(task)
        now = _time.monotonic()
        if now - self._last_task_sweep > 60:
            self._last_task_sweep = now
            self._tasks = [t for t in self._tasks if not t.done()]
        return task

    def _sweep_tasks(self) -> None:
        """Drop completed task handles. Called on a soft cadence; cheap."""
        self._tasks = [t for t in self._tasks if not t.done()]

    def _active_chat_model_line(self) -> str:
        """Name the model that will answer this turn, including a personal one."""
        provider = _current_request_provider.get()
        source = "personal connection" if provider is not None else "Maxwell's default model"
        model = ""
        if provider is not None:
            model = str(getattr(provider, "model", "") or "").strip()
        if not model:
            model = str(getattr(getattr(self, "config", None), "AI_MODEL", "") or "").strip()
        if not model:
            model = str(getattr(getattr(self, "ai_provider", None), "model", "") or "").strip()
        if not model:
            return ""
        return (
            f"Chat model for this turn: {model} ({source}). "
            "If asked which model you are using, answer with this id. "
            "Do not say the model name is hidden or that you cannot tell."
        )

    def _make_chat_provider(self, *, name: str = "primary", **kwargs):
        """Assemble providers without coupling Discord handling to a protocol."""
        from maxwell_core.providers.factory import openai_compat_provider

        return openai_compat_provider(name=name, **kwargs)

    def _setup_ai(self):
        self.ai_provider = self._make_chat_provider(
            name="primary",
            base_url=self.config.AI_BASE_URL,
            model=self.config.AI_MODEL,
            max_tokens=self.config.AI_MAX_OUTPUT_TOKENS,
            api_key=self.config.AI_API_KEY,
            disable_reasoning=self.config.AI_DISABLE_REASONING,
            reasoning_effort=getattr(self.config, "AI_REASONING_EFFORT", "") or "",
            fallback_base_url=self.config.AI_FALLBACK_BASE_URL,
            fallback_model=self.config.AI_FALLBACK_MODEL,
            fallback_api_key=self.config.AI_FALLBACK_API_KEY,
            fallback_disable_reasoning=self.config.AI_FALLBACK_DISABLE_REASONING,
            fallback_reasoning_effort=getattr(
                self.config, "AI_FALLBACK_REASONING_EFFORT", ""
            )
            or "",
            retry_attempts=self.config.AI_RETRY_ATTEMPTS,
            empty_response_retries=getattr(
                self.config, "AI_EMPTY_RESPONSE_RETRIES", None
            ),
            enable_audio_input=_owner_audio_input_enabled(self),
            vision_base_url=self.config.AI_VISION_BASE_URL,
            vision_model=self.config.AI_VISION_MODEL,
            vision_api_key=self.config.AI_VISION_API_KEY,
            vision_disable_reasoning=self.config.AI_VISION_DISABLE_REASONING,
        )

    def _wants_premium_model(self, quota_user_id: str, message) -> bool:
        """Premium-plan grant, or the experimental agentic routing switch."""
        plan_for = getattr(getattr(self, "_message_quota", None), "plan_for", None)
        if quota_user_id and callable(plan_for):
            try:
                if plan_for(quota_user_id) == "premium":
                    return True
            except Exception:
                logger.debug("premium model lookup failed", exc_info=True)
        # Experimental: requests that want something DONE (build, run, deploy,
        # debug …) get the stronger model even on the free plan, because the
        # small model is what usually fails them.
        if self._control.get("experimental_premium_model", False):
            content = str(getattr(message, "content", "") or "")
            return bool(content and self._ACTION_TOOL_HINT_RE.search(content))
        return False

    def _setup_memory(self):
        self.memory = ConversationMemoryManager(
            data_dir=self.config.DATA_DIR, max_messages=self.config.MEMORY_MESSAGE_LIMIT
        )

        # Privacy remains available even if the optional UI plugin fails to load.
        self._user_preferences = self.memory.preferences
        self.inbox = InboxStore(self.config.DATA_DIR)
        self.thread_store = ThreadStore(self.config.DATA_DIR)
        self.thread_store.load()

    def _install_core_prompt_components(self) -> None:
        """Core prompt slices that are not owned by a feature plugin."""
        prompts = getattr(self, "prompts", None)
        if prompts is None or not hasattr(prompts, "register"):
            return
        from maxwell_core.prompts.component import PromptComponent

        try:
            prompts.register(
                PromptComponent(
                    id="core.identity",
                    plugin="core",
                    text=MAXWELL_BASE_KNOWLEDGE,
                    position="identity",
                    priority=10,
                    scope="always",
                )
            )
            prompts.register(
                PromptComponent(
                    id="core.protocol",
                    plugin="core",
                    text=TOOL_PROTOCOL,
                    position="protocol",
                    priority=20,
                    scope="always",
                )
            )
        except ValueError:
            # Registration rejects ownership collisions, including core IDs.
            pass

    def _publish_core_services(self) -> None:
        pm = getattr(self, "plugin_manager", None)
        if pm is None:
            return
        memory = getattr(self, "memory", None)
        if memory is not None:
            pm.services.register("memory", memory, owner="core")
        provider = getattr(self, "ai_provider", None)
        if provider is not None:
            pm.services.register("provider:primary", provider, owner="core")

    def _build_activities(self):
        activities = []
        if self._current_game:
            activities.append(self._current_game)
        if self._custom_status:
            activities.append(self._custom_status)
        return activities

    def discord_user_client(self):
        return None

    def discord_bot_client(self):
        return self

    def _self_ids(self) -> set[int]:
        return _discord_account_ids(self)

    def _register_self_user(self, user) -> None:
        uid = getattr(user, "id", None)
        if uid is None:
            return
        self._account_ids.add(int(uid))

    def get_channel(self, channel_id, /):
        ch = super().get_channel(channel_id)
        if ch is None:
            peer = getattr(self, "_peer", None)
            if peer is not None:
                with contextlib.suppress(Exception):
                    ch = peer.get_channel(channel_id)
        return ch

    def get_guild(self, guild_id, /):
        guild = super().get_guild(guild_id)
        if guild is None:
            peer = getattr(self, "_peer", None)
            if peer is not None:
                with contextlib.suppress(Exception):
                    guild = peer.get_guild(guild_id)
        return guild

    def get_user(self, user_id, /):
        user = super().get_user(user_id)
        if user is None:
            peer = getattr(self, "_peer", None)
            if peer is not None:
                with contextlib.suppress(Exception):
                    user = peer.get_user(user_id)
        return user

    @property
    def guilds(self):
        seen: dict[int, Any] = {}
        for guild in super().guilds:
            gid = getattr(guild, "id", None)
            if gid is not None:
                seen[int(gid)] = guild
        peer = getattr(self, "_peer", None)
        if peer is not None:
            for guild in getattr(peer, "guilds", []) or []:
                gid = getattr(guild, "id", None)
                if gid is not None:
                    seen.setdefault(int(gid), guild)
        return list(seen.values())

    async def _push_presence(self, **kwargs):
        """Send presence to Discord. Tests replace this to avoid super()."""
        allowed = {}
        if "activity" in kwargs and kwargs["activity"] is not MISSING:
            allowed["activity"] = kwargs["activity"]
        if "status" in kwargs and kwargs["status"] is not MISSING:
            allowed["status"] = kwargs["status"]
        return await super().change_presence(**allowed)

    async def fetch_user(self, user_id, /):
        """Prefer the gateway cache. Bare fetch_user always hits REST."""
        cached = None
        with contextlib.suppress(Exception):
            cached = self.get_user(int(user_id))
        if cached is not None:
            return cached
        try:
            return await super().fetch_user(user_id)
        except Exception:
            peer = getattr(self, "_peer", None)
            if peer is not None:
                return await peer.fetch_user(user_id)
            raise

    async def fetch_channel(self, channel_id, /):
        """Prefer the gateway cache. Bare fetch_channel always hits REST."""
        cached = None
        with contextlib.suppress(Exception):
            cached = self.get_channel(int(channel_id))
        if cached is not None:
            return cached
        try:
            return await super().fetch_channel(channel_id)
        except Exception:
            peer = getattr(self, "_peer", None)
            if peer is not None:
                return await peer.fetch_channel(channel_id)
            raise

    async def change_presence(
        self,
        *,
        activity=MISSING,
        status=MISSING,
        **_ignored,
    ):
        overlay = bool(getattr(self, "_sleep_presence_overlay", False))
        if status is not MISSING and status is not None and not overlay:
            self._current_status = status
        if not overlay and self._sleep_window_active():
            status = discord.Status.idle
        kwargs = {}
        if activity is not MISSING:
            kwargs["activity"] = activity
        if status is not MISSING:
            kwargs["status"] = status
        pusher = getattr(self, "_push_presence", None)
        if callable(pusher):
            return await pusher(**kwargs)
        return await super().change_presence(**kwargs)

    _MAX_REPLY_CHAIN = 6

    _MAX_REACTION_MESSAGES = 500

    _MAX_REACTORS_PER_MESSAGE = 40

    async def setup_hook(self):
        tree = getattr(self, "tree", None)
        if tree is not None:
            tree.on_error = self._on_tree_error
        await self.plugin_manager.complete_pending_setups()
        await self.ai_provider.initialize()
        self.memory.load_from_disk()
        from privacy_data import cleanup_data
        try:
            await asyncio.to_thread(cleanup_data, self.config.DATA_DIR, self._user_preferences)
        except Exception as exc:
            logger.warning("Startup privacy cleanup failed (%s); periodic cleanup will retry", type(exc).__name__)
        self._load_sites()
        self._load_admins()
        self._load_auto_channels()
        self._load_progress_servers()
        self._load_ticket_greeting_servers()
        self._load_blacklist()
        self._load_shell_whitelist()
        self._load_control(force=True)
        # The reply queue is the single serialization point for generating a
        # reply. Bound here (not at construction) because it needs the running
        # loop's task factory for tracking.
        self._reply_queue.bind(self._run_reliable_turn, task_factory=self._track_task)
        self._request_journal.recover()
        abandoned = self._abandon_offline_inbound()
        self._watermarks.load()
        self._capture_recovery_snapshot()
        logger.info(
            "Reply queue bound; %d channel watermark(s) restored%s",
            len(self._watermarks),
            f"; abandoned {abandoned} offline inbound request(s)" if abandoned else "",
        )
        with contextlib.suppress(Exception):
            started = self.plugin_manager.start_jobs()
            if started:
                logger.info("Plugin jobs started: %d", started)
        self._tasks = [
            asyncio.create_task(self._site_cleanup_loop()),
            asyncio.create_task(self._memory_cleanup_loop()),
            asyncio.create_task(self._control_reload_loop()),
            asyncio.create_task(self._command_queue_loop()),
            asyncio.create_task(self._discord_state_loop()),
            asyncio.create_task(self._watermark_save_loop(), name="watermark-save"),
            asyncio.create_task(self._inbound_retry_loop(), name="inbound-retry"),
        ]
        logger.info("Bot setup complete")

    def is_connected(self) -> bool:
        """True while any Discord gateway websocket is open.

        AutoShardedBot keeps sockets on ``shards``, not ``self.ws``. A single
        closed shard does not make the whole process look disconnected.
        """
        if self.is_closed():
            return False
        shards = None
        try:
            shards = getattr(self, "shards", None)
        except Exception:
            shards = None
        if isinstance(shards, dict) and shards:
            for info in shards.values():
                ws = getattr(info, "ws", None)
                try:
                    if ws is not None and bool(getattr(ws, "open", False)):
                        return True
                except Exception:
                    continue
            return False
        ws = getattr(self, "ws", None)
        if ws is None:
            return False
        try:
            return bool(ws.open)
        except Exception:
            return False

    async def _arm_background_workers(self) -> None:
        """Start slash sync and plugin on_ready once per process."""
        if getattr(self, "_background_workers_armed", False):
            return
        self._background_workers_armed = True
        if getattr(self, "user", None) is not None:
            self._spawn_detached(self._sync_slash_commands())
        if not getattr(self.config, "MAXWELL_DEV_MODE", False):
            self._dispatch_plugin_event("on_ready")

    async def _drain_then_close(self) -> None:
        """Let running turns finish, then close the gateway."""
        timeout = drain_timeout_seconds()
        logger.info("Draining in-flight work for up to %.0fs", timeout)
        await drain_inflight(self, timeout)
        with contextlib.suppress(Exception):
            await self.close()

    def _dev_scope_allows(self, context, *, guild_id=None) -> bool:
        """Fail closed outside configured Dev guilds, including DMs."""
        cfg = getattr(self, "config", None)
        if not getattr(cfg, "MAXWELL_DEV_MODE", False):
            return True
        if guild_id is None:
            guild_id = getattr(context, "guild_id", None)
        if guild_id is None:
            guild_id = getattr(getattr(context, "guild", None), "id", None)
        if guild_id is None:
            guild_id = getattr(
                getattr(getattr(context, "channel", None), "guild", None),
                "id",
                None,
            )
        if guild_id is None:
            guild_id = getattr(
                getattr(getattr(context, "message", None), "guild", None),
                "id",
                None,
            )
        return str(guild_id or "") in getattr(cfg, "MAXWELL_DEV_GUILD_IDS", frozenset())

    def _spawn_detached(self, coro) -> None:
        """Fire-and-forget with a strong reference so it cannot be GC'd."""
        try:
            task = asyncio.create_task(coro)
        except RuntimeError:
            with contextlib.suppress(Exception):
                coro.close()
            return
        detached = getattr(self, "_detached_tasks", None)
        if detached is None:
            detached = self._detached_tasks = set()
        detached.add(task)

        def _on_done(t: asyncio.Task) -> None:
            detached.discard(t)
            if not t.cancelled():
                exc = t.exception()
                if exc:
                    logger.error("Detached task failed: %s", exc, exc_info=exc)

        task.add_done_callback(_on_done)

    _SITE_REQUEST_RE = re.compile(
        r"\b(make|build|create|code|design|generate|spin\s*up|throw\s*together|cobble|craft|put\s*together)\b"
        r"[^\.!?\n]{0,40}\b(site|website|web\s*page|page|landing\s*page|landing|portfolio|webapp|web\s*app|dashboard|storefront|homepage|home\s*page|webview)\b",
        re.IGNORECASE,
    )

    _HTML_DOC_HINTS = (
        "<!doctype html",
        "<html",
        "<head",
        "<body",
        "<style",
        "<script",
        "<canvas",
    )

    _GRID_CELL = 72  # icon box, px

    _GRID_LABEL_H = 14  # label strip under each icon, px

    _GRID_COLS = 8

    _GRID_MAX_EMOJIS = 48

    _GRID_MAX_STICKERS = 12

    _EMOJI_MARKUP_RE = re.compile(r"<(a)?:([A-Za-z0-9_]{2,32}):(\d{15,25})>")

    _SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_-]+")

    _MAX_INLINE_EMOJIS = 3

    _MAX_INLINE_STICKERS = 3

    _REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

    _MAX_MEDIA_REDIRECTS = 3

    _META_TAG_RE = re.compile(r"<meta\s+[^>]*>", re.I)

    _META_ATTR_RE = re.compile(r"""([^\s=]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""")

    _LINK_IMAGE_EXTS = frozenset(
        {
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
            ".webp",
            ".bmp",
            ".tif",
            ".tiff",
            ".heic",
            ".heif",
            ".avif",
            ".apng",
        }
    )

    _LINK_AUDIO_EXTS = frozenset(
        {".mp3", ".wav", ".ogg", ".oga", ".opus", ".m4a", ".flac", ".aac", ".wma"}
    )

    _LINK_VIDEO_EXTS = frozenset(
        {".mp4", ".webm", ".mov", ".mkv", ".avi", ".m4v", ".mpeg", ".mpg", ".3gp"}
    )

    _ACTION_TOOL_HINT_RE = re.compile(
        r"(?i)\b("
        r"make|build|create|generate|draw|render|design|code|write|program|"
        r"deploy|publish|host|ship|"
        r"site|website|webpage|web\s*page|page|html|css|landing|portfolio|"
        r"dashboard|app|backend|api|server|"
        r"edit|update|change|fix|patch|rewrite|redo|tweak|delete|remove|"
        r"rename|move|clear|purge|wipe|reset|"
        r"send|post|upload|attach|share|forward|dm|email|mail|inbox|"
        r"ban|kick|mute|unmute|timeout|unban|warn|jail|"
        r"role|roles|perm|perms|permission|channel|category|thread|invite|"
        r"server|guild|nick|nickname|avatar|status|presence|activity|playing|"
        r"emoji|sticker|poll|pin|pinned|"
        r"run|exec|execute|shell|bash|script|command|install|"
        r"file|files|attachment|download|"
        r"sleep|nap|wake|"
        r"remember|forget|memory|personality|prompt|"
        r"tool|tools"
        r")\b"
    )

    _ECHO_OPENER_WORDS = 4

    _ECHO_LOOKBACK = 8

    _ECHO_MIN_REPEATS = 3

