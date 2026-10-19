"""Persistent settings: admins, blacklists, per-server toggles and control reload.

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

class SettingsMixin:

    def _json_path(self, name):
        return Path(self.config.DATA_DIR) / name

    def _try_load_str_set(self, name, *, require_list=True):
        path = self._json_path(name)
        if not path.exists():
            return None
        data = _read_json(path)
        if require_list and not isinstance(data, list):
            return None
        return _str_set(data)

    def _save_str_set(self, name, values, err, *, sort=False):
        try:
            _atomic_json_write_sync(
                self._json_path(name), sorted(values) if sort else list(values)
            )
        except Exception as e:
            logger.error(f"{err}: {e}")

    def _load_sites(self, quiet: bool = False):
        try:
            path = self._json_path("sites.json")
            mtime = path.stat().st_mtime if path.exists() else 0.0
            if mtime == self._sites_mtime:
                return
            data = _read_json(path) if path.exists() else {}
            self._sites = (
                {k: v for k, v in data.items() if isinstance(v, dict)}
                if isinstance(data, dict)
                else {}
            )
            self._sites_mtime = mtime
            if not quiet:
                logger.info(f"Loaded {len(self._sites)} tracked sites from disk")
        except Exception as e:
            # Keep previous in-memory map. Resetting to {} after one corrupt read
            # turns recoverable disk damage into deleted sites.
            logger.error(f"Failed to load sites: {e}")

    def _load_auto_channels(self, quiet: bool = False):
        try:
            ids = self._try_load_str_set("auto_channels.json")
            if ids is not None:
                self._auto_channels = ids
            if not quiet:
                logger.info(f"Loaded {len(self._auto_channels)} auto-channels")
        except Exception as e:
            logger.error(f"Failed to load auto channels: {e}")
            self._auto_channels = set()

    def _save_auto_channels(self):
        self._save_str_set(
            "auto_channels.json", self._auto_channels, "Failed to save auto channels"
        )

    def _load_ticket_greeting_servers(self, quiet: bool = False):
        try:
            ids = self._try_load_str_set("ticket_greeting_servers.json")
            if ids is not None:
                self._ticket_greeting_servers = ids
            if not quiet:
                logger.info(
                    "Loaded %s ticket-greeting server(s)",
                    len(self._ticket_greeting_servers),
                )
        except Exception as e:
            logger.error(f"Failed to load ticket greeting servers: {e}")
            self._ticket_greeting_servers = set()

    def _save_ticket_greeting_servers(self):
        self._save_str_set(
            "ticket_greeting_servers.json",
            self._ticket_greeting_servers,
            "Failed to save ticket greeting servers",
            sort=True,
        )

    def _ticket_greeting_enabled(self, server_id: str) -> bool:
        """Auto-hello in new ticket channels. OFF unless enabled for the server."""
        if not server_id or server_id == "DM":
            return False
        return server_id in self._ticket_greeting_servers

    def _load_progress_servers(self, quiet: bool = False):
        try:
            ids = self._try_load_str_set("progress_servers.json")
            if ids is not None:
                self._progress_servers = ids
            off = self._try_load_str_set("progress_servers_off.json")
            if off is not None:
                self._progress_servers_off = off
            if not quiet:
                logger.info(
                    f"Loaded {len(self._progress_servers)} progress-enabled servers, "
                    f"{len(self._progress_servers_off)} explicit-off servers"
                )
        except Exception as e:
            logger.error(f"Failed to load progress servers: {e}")
            self._progress_servers = set()
            self._progress_servers_off = set()

    def _save_progress_servers(self):
        try:
            _atomic_json_write_sync(
                self._json_path("progress_servers.json"),
                sorted(self._progress_servers),
            )
            _atomic_json_write_sync(
                self._json_path("progress_servers_off.json"),
                sorted(self._progress_servers_off),
            )
        except Exception as e:
            logger.error(f"Failed to save progress servers: {e}")

    def _progress_enabled(self, server_id: str) -> bool:
        """Live tool progress defaults on; admins can silence a server with
        `/progress arguments:off` (persisted to progress_servers_off.json). DMs never get
        progress messages. MAXWELL_PROGRESS_MESSAGES=false disables the
        baseline globally, while a server-level `/progress arguments:on` still opts in."""
        if not server_id or server_id == "DM":
            return False
        if server_id in self._progress_servers:
            return True
        if server_id in self._progress_servers_off:
            return False
        return bool(self.config.PROGRESS_MESSAGES)

    def _load_blacklist(self, quiet: bool = False):
        try:
            ids = self._try_load_str_set("blacklist.json")
            if ids is not None:
                self._blacklist = ids
            if not quiet:
                logger.info(f"Loaded {len(self._blacklist)} blacklisted users")
        except Exception as e:
            # Keep the previous in-memory blacklist. Resetting to empty after
            # one bad read would unblock every blocked user until it is fixed.
            logger.error(f"Failed to load blacklist: {e}")

    def _load_shell_whitelist(self, quiet: bool = False):
        try:
            ids = self._try_load_str_set("shell_whitelist.json", require_list=False)
            if ids is not None:
                self._shell_whitelist = ids
            if not quiet:
                logger.info(
                    f"Loaded {len(self._shell_whitelist)} whitelisted shell users"
                )
        except Exception as e:
            logger.error(f"Failed to load shell whitelist: {e}")
            self._shell_whitelist = set()

    def _save_shell_whitelist(self):
        self._save_str_set(
            "shell_whitelist.json",
            self._shell_whitelist,
            "Failed to save shell whitelist",
        )

    def _save_blacklist(self):
        self._save_str_set(
            "blacklist.json", self._blacklist, "Failed to save blacklist"
        )

    def _load_admins(self, quiet: bool = False):
        extra: set[str] = set()
        try:
            path = self._json_path("admins.json")
            if path.exists():
                data = _read_json(path)
                if isinstance(data, list):
                    extra.update(_str_set(data))
                elif isinstance(data, dict):
                    for key in ("admins", "owners", "user_ids"):
                        values = data.get(key)
                        if isinstance(values, list):
                            extra.update(_str_set(values))
            self._admins = configured_admin_ids(self.config, extra=extra)
            if not quiet:
                logger.info(f"Loaded {len(self._admins)} admin user(s)")
        except Exception as e:
            logger.error(f"Failed to load admins: {e}")
            self._admins = configured_admin_ids(self.config)

    def _is_admin(self, user_id) -> bool:
        """True if user_id is in the configured admin set (owners + admins.json)."""
        return str(user_id) in self._admins

    def _dm_replies_allowed(self, message) -> bool:
        """Inbound DMs follow reply_dms. Off blocks everyone, including admins."""
        if not isinstance(getattr(message, "channel", None), discord.DMChannel):
            return True
        return bool((getattr(self, "_control", None) or {}).get("reply_dms", False))

    def _save_admins(self):
        self._save_str_set(
            "admins.json", self._admins, "Failed to save admins", sort=True
        )


    def _load_control(self, force: bool = False):
        path = Path(self.config.DATA_DIR) / "bot_control.json"
        try:
            mtime = path.stat().st_mtime if path.exists() else 0
            if not force and mtime == self._control_mtime:
                return
            loaded = {}
            if path.exists():
                with open(path, encoding="utf-8") as f:
                    loaded = json.load(f)
                if not isinstance(loaded, dict):
                    loaded = {}
            control = dict(DEFAULT_CONTROL)
            control.update(loaded)
            # The shared Maxwell persona is code-owned. Ignore values written by
            # old tools, dashboard versions, or a stale bot_control.json.
            control["base_personality"] = DEFAULT_CONTROL["base_personality"]
            for dead_key in DEAD_CONTROL_KEYS:
                control.pop(dead_key, None)
            for key, default in DEFAULT_CONTROL.items():
                if isinstance(default, bool):
                    control[key] = parse_bool(control.get(key), default)
            control["max_response_chars"] = max(
                80,
                min(
                    parse_int(control.get("max_response_chars", 4000) or 4000, 4000),
                    8000,
                ),
            )
            control["tool_history_messages"] = max(
                0, min(parse_int(control.get("tool_history_messages", 10) or 0, 0), 30)
            )
            default_budget = DEFAULT_CONTROL["prompt_context_budget"]
            control["prompt_context_budget"] = max(
                10000,
                min(
                    parse_int(
                        control.get("prompt_context_budget", default_budget)
                        or default_budget,
                        default_budget,
                    ),
                    2000000,
                ),
            )
            control["message_quota_limit"] = max(
                1,
                min(
                    _safe_int(control.get("message_quota_limit"), FREE_MESSAGE_LIMIT),
                    100_000,
                ),
            )
            control["message_quota_window_seconds"] = max(
                60,
                min(_safe_int(control.get("message_quota_window_seconds"), FREE_WINDOW_SECONDS), 7 * 24 * 3600),
            )
            control["live_max_output_tokens"] = max(
                256, min(parse_int(control.get("live_max_output_tokens"), 4096), 32768)
            )
            self._control = control
            self._sync_audio_input_flags()
            # 2026-07-22: the old global progress_messages re-apply is gone —
            # progress is now per-server via _progress_servers / the env
            # baseline. bot_control.json may still contain a stale
            # 'progress_messages' key from older installs; it's ignored by all
            # read sites now (they call _progress_enabled(server_id)).
            self._control_mtime = mtime
            logger.info("Loaded dashboard control settings")
            if not self._conversation_watch_enabled():
                self._drop_watches_for_sleep()
        except Exception as e:
            logger.error(f"Failed to load control settings: {e}")

    def _sync_audio_input_flags(self) -> None:
        """Keep every provider's audio flag in lockstep with process_audio."""
        enabled = _owner_audio_input_enabled(self)
        for attr in ("ai_provider",):
            provider = getattr(self, attr, None)
            if provider is not None:
                provider.enable_audio_input = enabled

    async def _control_reload_loop(self):
        while True:
            await asyncio.sleep(5)
            try:
                self._load_admins(quiet=True)
                self._load_auto_channels(quiet=True)
                self._load_progress_servers(quiet=True)
                self._load_ticket_greeting_servers(quiet=True)
                self._load_blacklist(quiet=True)
                self._load_sites(quiet=True)
                self._load_control()
            except asyncio.CancelledError as _exc:
                raise
            except Exception as e:
                logger.error(f"Control reload loop error: {e}")

    async def _site_cleanup_loop(self):
        # Site backend containers carry --restart unless-stopped, so docker
        # brings them back on its own after a reboot. This pass only fixes the
        # registry when one went away for good (prune, manual rm, a site
        # deleted while the bot was down).
        with contextlib.suppress(Exception):
            await site_server.reconcile(self.config.DATA_DIR)
        while True:
            await asyncio.sleep(300)
            try:
                await self._cleanup_sites()
            except Exception as e:
                logger.error(f"Site cleanup error: {e}")
            # A site_test whose probe was killed (SIGKILL, OOM, container stop)
            # never reaches its own cleanup, so its browser profile stays on
            # disk forever. This is the only thing that reclaims those.
            try:
                await asyncio.to_thread(site_test.sweep_browser_profiles)
            except Exception as e:
                logger.debug("Browser profile sweep failed: %s", e)

    def _site_expired(self, entry: dict, now: float) -> bool:
        """Per-site lifetime: permanent flag, then per-site ttl, then control."""
        if entry.get("permanent"):
            return False
        ttl_hours = entry.get("ttl_hours")
        if ttl_hours is None:
            ttl_hours = self._control.get("site_ttl_hours", 24)
        try:
            ttl = float(ttl_hours or 0) * 3600.0
        except (TypeError, ValueError):
            ttl = 86400.0
        if ttl <= 0:
            return False
        return now - float(entry.get("created_at", 0) or 0) > ttl

    async def _cleanup_sites(self):
        self._load_sites(quiet=True)
        base = Path(self.config.MAXWELL_SITE_DIR).resolve()
        now = datetime.now(timezone.utc).timestamp()
        expired = []
        for slug, data in list(self._sites.items()):
            if not self._site_expired(data, now):
                continue
            try:
                if not re.fullmatch(r"[a-z0-9-]{2,30}", slug):
                    expired.append(slug)
                    continue
                path = (base / slug).resolve()
                if (path == base or base in path.parents) and path.exists():
                    await asyncio.to_thread(shutil.rmtree, path)
                    logger.info(f"Deleted expired site {slug}")
                # The site's server-side store goes with it, so a later site
                # on the same slug never inherits the old one's data.
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(
                        site_backend.destroy, self.config.DATA_DIR, slug
                    )
                # And its backend container, code, database, and secrets — an
                # expired site must not leave a server running.
                with contextlib.suppress(Exception):
                    await site_server.destroy(self.config.DATA_DIR, slug)
            except Exception as e:
                logger.error(f"Failed to delete site {slug}: {e}")
            expired.append(slug)
        if expired:
            for slug in expired:
                self._sites.pop(slug, None)
            sites_path = Path(self.config.DATA_DIR) / "sites.json"

            # Cross-process lock so cleanup's removal can't lose a concurrent
            # create_site/API site_update commit (and vice versa).
            def _locked_cleanup_write():
                with FileLock(sites_path, timeout=15.0):
                    # Reload fresh inside the lock so we don't resurrect entries
                    # the API just added, and drop only our expired set.
                    fresh = {}
                    try:
                        if sites_path.exists():
                            data = json.loads(sites_path.read_text(encoding="utf-8"))
                            if isinstance(data, dict):
                                fresh = {
                                    k: v for k, v in data.items() if isinstance(v, dict)
                                }
                    except (json.JSONDecodeError, OSError, ValueError):
                        fresh = dict(self._sites)
                    for slug in expired:
                        fresh.pop(slug, None)
                    _atomic_json_write_sync(sites_path, fresh)
                    self._sites = fresh
                    return sites_path.stat().st_mtime if sites_path.exists() else 0.0

            try:
                self._sites_mtime = await asyncio.to_thread(_locked_cleanup_write)
            except OSError:
                self._sites_mtime = 0.0
