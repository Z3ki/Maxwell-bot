"""Missed-message recovery, inbound retries, watermarks and state persistence.

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

class RecoveryMixin:

    def _missed_message_scan_limit(self) -> int:
        """Per-room page size; unfinished pages continue on the next tick."""
        raw = (getattr(self, "_control", None) or {}).get(
            "gap_recovery_max_messages", 20
        )
        try:
            return max(0, min(int(raw), 100))
        except (TypeError, ValueError):
            return 20

    def _capture_recovery_snapshot(self) -> None:
        cursors = getattr(self, "_recovery_cursors", None)
        if cursors is None:
            cursors = self._recovery_cursors = {}
        for cid, mid in self._watermarks.channels():
            cursors[cid] = min(cursors.get(cid, mid), mid)
        self._load_failed_receipt_floors()
        for cid, mid in self._failed_receipt_floors.items():
            cursors[cid] = min(cursors.get(cid, mid), mid)

    def _load_failed_receipt_floors(self) -> None:
        if hasattr(self, "_failed_receipt_floors"):
            return
        path = Path(self._watermarks.path).with_name("inbound_recovery_floors.json")
        try:
            floors = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            floors = {}
        if not isinstance(floors, dict) or any(
            not str(cid).isdigit() or not isinstance(mid, int) or mid < 0
            for cid, mid in floors.items()
        ):
            raise ValueError("Invalid inbound recovery floors")
        self._failed_receipt_floors = floors

    def _save_failed_receipt_floors(self) -> None:
        _atomic_json_write_sync(
            Path(self._watermarks.path).with_name("inbound_recovery_floors.json"),
            self._failed_receipt_floors,
        )

    def _freeze_failed_receipt(self, channel_id: str, message_id: str) -> None:
        """Persist the gap even if the receipt itself could not be journaled."""
        if not channel_id.isdigit() or not message_id.isdigit():
            return
        self._load_failed_receipt_floors()
        floor = max(0, int(message_id) - 1)
        prior = self._watermarks.get(channel_id)
        if prior is not None:
            floor = min(floor, prior)
        floors = self._failed_receipt_floors
        floors[channel_id] = min(floors.get(channel_id, floor), floor)
        cursors = getattr(self, "_recovery_cursors", None)
        if cursors is None:
            cursors = self._recovery_cursors = {}
        cursors[channel_id] = min(cursors.get(channel_id, floor), floors[channel_id])
        versions = getattr(self, "_receipt_failure_versions", None)
        if versions is None:
            versions = self._receipt_failure_versions = {}
        versions[channel_id] = versions.get(channel_id, 0) + 1
        self._save_failed_receipt_floors()

    async def _fetch_inbound_channel(self, channel_id):
        channel = self.get_channel(int(channel_id))
        if channel is None:
            channel = await self.fetch_channel(int(channel_id))
        return channel

    def _abandon_offline_inbound(self) -> int:
        """Drop journaled work from before this process started. No replies."""
        journal = getattr(self, "_request_journal", None)
        boot = float(getattr(self, "_process_started_at", 0) or 0)
        if journal is None or boot <= 0:
            return 0
        dropped = 0
        for row in journal.pending(limit=10000):
            created = float(row.get("created_at") or 0)
            if created >= boot:
                continue
            with contextlib.suppress(Exception):
                journal.update(row["message_id"], "suppressed", reason="stale_offline")
                dropped += 1
        return dropped

    async def _latest_channel_message_id(self, channel) -> int | None:
        latest = getattr(channel, "last_message_id", None)
        if latest:
            with contextlib.suppress(TypeError, ValueError):
                return int(latest)
        history = getattr(channel, "history", None)
        if not callable(history):
            return None
        try:
            async for msg in history(limit=1):
                mid = getattr(msg, "id", None)
                if mid is not None:
                    return int(mid)
        except TypeError:
            async for msg in history():
                mid = getattr(msg, "id", None)
                if mid is not None:
                    return int(mid)
                break
        except Exception:
            return None
        return None

    async def _recover_missed_messages(self, *, settle=True) -> None:
        """Skip the offline backlog. Do not answer messages from while we were down."""
        if not hasattr(self, "_recovery_lock"):
            self._recovery_lock = asyncio.Lock()
        if self._recovery_lock.locked():
            return
        if not hasattr(self, "_recovery_cursors"):
            self._capture_recovery_snapshot()
        async with self._recovery_lock:
            if settle:
                await asyncio.sleep(0)
            skipped = 0
            for channel_id in list(self._recovery_cursors):
                if not str(channel_id).isdigit():
                    self._recovery_cursors.pop(channel_id, None)
                    continue
                if getattr(getattr(self, "config", None), "MAXWELL_DEV_MODE", False):
                    # Recovery may contain stale channel IDs. Never fetch an
                    # unknown channel or a channel outside the Dev guild.
                    channel = self.get_channel(int(channel_id))
                    if not MaxwellBot._dev_scope_allows(self, channel):
                        self._recovery_cursors.pop(channel_id, None)
                        continue
                try:
                    channel = await self._fetch_inbound_channel(channel_id)
                    latest = await self._latest_channel_message_id(channel)
                    if latest:
                        self._watermarks.note(channel_id, latest)
                        skipped += 1
                except (discord.Forbidden, discord.NotFound):
                    pass
                except Exception as exc:
                    logger.warning(
                        "inbound cid=%s stage=recovery_skip_error error=%s",
                        channel_id,
                        type(exc).__name__,
                    )
                self._recovery_cursors.pop(channel_id, None)
            floors = getattr(self, "_failed_receipt_floors", None)
            if floors:
                floors.clear()
                with contextlib.suppress(Exception):
                    self._save_failed_receipt_floors()
            self._watermarks.save()
            if skipped:
                logger.info(
                    "Skipped offline backlog in %s channel(s); not answering old messages",
                    skipped,
                )

    async def _retry_pending_inbound(self) -> None:
        journal = getattr(self, "_request_journal", None)
        if journal is None:
            return
        if self._reply_queue.full:
            # Restart keyset paging when capacity returns so a long deferred
            # prefix does not make older requests wait for a full sweep.
            self._inbound_retry_before = None
            self._inbound_retry_after = None
            return
        delay = self._inbound_setting("inbound_retry_delay_seconds", 5, 1, 300)
        if getattr(self, "_inbound_retry_before", None) is None:
            self._inbound_retry_before = time.time()
            self._inbound_retry_after = None
        rows = (await journal.submit(journal.pending, limit=1000,
        after=self._inbound_retry_after,
        before=self._inbound_retry_before,))
        boot = float(getattr(self, "_process_started_at", 0) or 0)
        handled = 0
        for row in rows:
            if handled >= 20 or self._reply_queue.full:
                break
            mid, cid = row["message_id"], row["channel_id"]
            interaction_message = (getattr(self, "_interaction_requests", None) or {}).get(mid)
            self._inbound_retry_after = (row["created_at"], mid)
            if getattr(getattr(self, "config", None), "MAXWELL_DEV_MODE", False):
                channel = interaction_message or (self.get_channel(int(cid)) if str(cid).isdigit() else None)
                if not MaxwellBot._dev_scope_allows(self, channel):
                    with contextlib.suppress(Exception):
                        await journal.submit(journal.update, mid, "suppressed", reason="outside_dev_guild")
                    continue
            if boot and float(row.get("created_at") or 0) < boot:
                with contextlib.suppress(Exception):
                    await journal.submit(journal.update, mid, "suppressed", reason="stale_offline")
                continue
            if self._reply_queue.contains(cid, mid) or mid in self._inbound_processing:
                continue
            if (
                self._reply_queue.max_directed > 0
                and self._reply_queue.depth(cid) >= self._reply_queue.max_directed
            ):
                continue
            if time.time() - float(row["updated_at"]) < delay * max(1, row["attempts"]):
                continue
            handled += 1
            message = interaction_message or SimpleNamespace(id=mid, channel=SimpleNamespace(id=cid))
            try:
                if interaction_message is None:
                    channel = await asyncio.wait_for(
                        self._fetch_inbound_channel(cid), timeout=10
                    )
                    message = await asyncio.wait_for(
                        channel.fetch_message(int(mid)), timeout=10
                    )
                elif time.time() - row["created_at"] >= 900:
                    await self._record_request_outcome(message, "failed", "interaction_expired")
                    continue
                # Admissions run the normal policy gates but not receipt dedup.
                await self.on_message(message)
            except Exception as exc:
                await self._pending_fetch_failed(row, message, exc)
        else:
            if len(rows) < 1000:
                # Finish a fixed receipt-time snapshot before including new
                # arrivals; sustained traffic cannot extend a sweep forever.
                self._inbound_retry_before = None
                self._inbound_retry_after = None

    async def _pending_fetch_failed(self, receipt, message, exc) -> None:
        mid, cid = receipt["message_id"], receipt["channel_id"]
        if self._reply_queue.contains(cid, mid) or mid in self._inbound_processing:
            return
        current = (await self._request_journal.submit(self._request_journal.get, mid))
        if (
            not current
            or current["status"] in _REQUEST_TERMINAL
            or current["effects_started"]
            or current["updated_at"] != receipt["updated_at"]
        ):
            return
        # The HTTP fetch can race a fresh gateway admission. Claim only after
        # rechecking its receipt, then retain ownership through error delivery.
        self._inbound_processing.add(mid)
        try:
            if isinstance(exc, (discord.Forbidden, discord.NotFound)):
                await self._record_request_outcome(message, "failed", type(exc).__name__)
            else:
                await self._request_journal.submit(self._request_journal.begin, mid)
                await self._request_failure(
                    message, f"fetch:{type(exc).__name__}", retryable=True
                )
        finally:
            self._inbound_processing.discard(mid)

    async def _inbound_retry_loop(self):
        await self.wait_until_ready()
        while not self.is_closed():
            if getattr(self, "_draining", False):
                return
            await asyncio.sleep(5)
            try:
                await self._retry_pending_inbound()
                if self._recovery_cursors:
                    await self._recover_missed_messages(settle=False)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "inbound stage=retry_loop_error error=%s", type(exc).__name__
                )

    async def _discord_state_loop(self):
        while True:
            await asyncio.sleep(60)
            try:
                if self.is_ready():
                    await self._save_discord_state()
            except Exception as e:
                logger.warning(f"Discord state snapshot error: {e}")

    def _dispatch_plugin_event(self, event: str, *args: Any, **kwargs: Any) -> None:
        if event != "on_ready" and getattr(
            getattr(self, "config", None), "MAXWELL_DEV_MODE", False
        ):
            context = args[0] if args else None
            guild_id = getattr(context, "id", None) if event in {
                "on_guild_join", "on_guild_remove"
            } else None
            if not MaxwellBot._dev_scope_allows(self, context, guild_id=guild_id):
                return
        pm = getattr(self, "plugin_manager", None)
        if pm is None:
            return
        listeners = getattr(pm, "_listeners", None)
        if not listeners or event not in listeners or not listeners[event]:
            return
        spawn = getattr(self, "_spawn_detached", None)
        if callable(spawn):
            spawn(pm.dispatch_event(event, *args, **kwargs))
        else:
            MaxwellBot._spawn_detached(self, pm.dispatch_event(event, *args, **kwargs))

    async def _watermark_save_loop(self):
        """Persist read positions periodically.

        The most common gateway gap is a process restart, which is precisely
        when in-memory watermarks are lost — so they have to be on disk before
        the crash, not written during shutdown.
        """
        while True:
            await asyncio.sleep(30)
            try:
                await asyncio.to_thread(self._watermarks.save)
            except Exception as e:
                logger.debug("watermark save loop: %s", e)

    async def _save_discord_state(self):
        guilds = []
        for guild in self.guilds:
            if not MaxwellBot._dev_scope_allows(self, guild, guild_id=guild.id):
                continue
            channels = [
                {
                    "id": str(channel.id),
                    "name": channel.name,
                    "category": getattr(getattr(channel, "category", None), "name", "")
                    or "",
                    "position": getattr(channel, "position", 0),
                }
                for channel in getattr(guild, "text_channels", [])[:200]
            ]
            guilds.append(
                {
                    "id": str(guild.id),
                    "name": guild.name,
                    "member_count": getattr(guild, "member_count", None),
                    "channels": channels,
                }
            )
        dms = []
        for channel in (
            [] if getattr(self.config, "MAXWELL_DEV_MODE", False)
            else getattr(self, "private_channels", [])[:100]
        ):
            recipient = getattr(channel, "recipient", None)
            recipients = getattr(channel, "recipients", None)
            name = (
                getattr(recipient, "display_name", None)
                or getattr(recipient, "name", None)
                or getattr(channel, "name", None)
            )
            if not name and recipients:
                name = ", ".join(
                    getattr(user, "display_name", getattr(user, "name", "unknown"))
                    for user in recipients[:5]
                )
            dms.append(
                {
                    "id": str(getattr(channel, "id", "")),
                    "name": name or "DM",
                    "recipient_id": str(getattr(recipient, "id", ""))
                    if recipient
                    else "",
                    "type": channel.__class__.__name__,
                }
            )
        payload = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "user": {"id": str(self.user.id), "name": self.user.display_name}
            if self.user
            else {},
            "guilds": guilds,
            "dms": dms,
            "friends": self._friends_snapshot(),
        }
        await asyncio.to_thread(
            _atomic_json_write_sync,
            Path(self.config.DATA_DIR) / "discord_state.json",
            payload,
        )

    async def _append_inbox_dynamic(self, dynamic_parts: list[str], *, message=None) -> None:
        message = message or _current_inbound.get()
        uid = getattr(getattr(message, "author", None), "id", None)
        if not uid or not getattr(self, "_is_admin", lambda _uid: False)(uid):
            return
        private_reply = str(getattr(message, "response_visibility", "public")) == "private"
        source_kind = getattr(message, "user_install_source_kind", "")
        direct_dm = isinstance(getattr(message, "channel", None), discord.DMChannel) or source_kind == "DM"
        if not private_reply and not direct_dm:
            return
        store = getattr(self, "inbox", None)
        if store is None:
            return
        try:
            items = await store.load_items()
            pending = store.planner_items(items, exclude_announced=True)
            text = store.render_planner(items)
        except Exception:
            return
        if not text:
            return
        dynamic_parts.append(text)
        # Remember which notices this prompt carried, so they can be marked
        # read once he actually says something. Marking them here instead
        # would burn a notice on a turn he stayed silent for, and *not*
        # marking them at all is what made the same email get announced on
        # every turn until someone dismissed it by hand.
        _current_inbox_notices.set(tuple(
            str(i.get("id"))
            for i in pending
            if not inbox_needs_decision(i) and i.get("state") == "unread"
        ))

    async def _mark_inbox_announced(self) -> None:
        """Mark the notices the last prompt carried as read. Called after a send.

        Only notices — anything still awaiting an accept/decline keeps showing
        until it is actually answered. Never raises: a reply has already gone
        out by the time this runs, and failing to update the inbox is not
        worth turning a delivered message into an error.
        """
        shown = _current_inbox_notices.get()
        if not shown:
            return
        _current_inbox_notices.set(())
        store = getattr(self, "inbox", None)
        if store is None:
            return
        for item_id in shown:
            with contextlib.suppress(Exception):
                await store.mark(item_id, "read")
