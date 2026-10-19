"""on_message entrypoint, command queue and reply delivery.

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

from bot_parts._compat import (  # noqa: F401
    _get_shared_session,
    _read_response_limited,
)

class MessagesMixin:

    async def on_message(self, message):
        """Error boundary + dedup around the real handler.

        Everything below used to live in one function with no outer
        try/except, so any exception past the memory write escaped to
        discord.py's default ``on_error``: logged, and the message silently
        never answered. It also had no dedup, so a gateway resume that
        redelivered MESSAGE_CREATE produced a second full reply.
        """
        if getattr(self, "_draining", False):
            return
        if not MaxwellBot._dev_scope_allows(self, message):
            return
        message_id = str(getattr(message, "id", "") or "")
        channel_id = str(getattr(getattr(message, "channel", None), "id", "") or "")
        journal = getattr(self, "_request_journal", None)
        processing = getattr(self, "_inbound_processing", None)
        if processing is None:
            processing = self._inbound_processing = set()
        started = time.monotonic()
        acquired = False
        try:
            # Own this receipt before the first await; gateway duplicates must
            # not concurrently reset a request whose turn is already starting.
            if message_id in processing:
                return
            processing.add(message_id)
            acquired = True
            directed = self._directly_addressed(message)
            if journal is not None:
                try:
                    await journal.submit(journal.accept, message_id,
                    channel_id,
                    getattr(getattr(message, "author", None), "id", ""),
                    directed=directed,)
                except Exception:
                    self._freeze_failed_receipt(channel_id, message_id)
                    raise
                row = (await journal.submit(journal.get, message_id))
                if row and row["status"] in _REQUEST_TERMINAL:
                    return
                if self._reply_queue.contains(channel_id, message_id):
                    return
                if is_user_install_message(message):
                    requests = getattr(self, "_interaction_requests", None)
                    if requests is None:
                        requests = self._interaction_requests = {}
                    # Interaction IDs are not fetchable Discord message IDs.
                    # Retain the webhook adapter for bounded, in-process retries.
                    for mid in list(requests):
                        saved = (await journal.submit(journal.get, mid))
                        if not saved or saved["status"] in _REQUEST_TERMINAL:
                            requests.pop(mid, None)
                        elif time.time() - saved["created_at"] >= 900:
                            await self._record_request_outcome(requests[mid], "failed", "interaction_expired")
                    if (
                        self._reply_queue.max_outstanding > 0
                        and message_id not in requests
                        and len(requests) >= self._reply_queue.max_outstanding
                    ):
                        await self._request_failure(message, "interaction_capacity")
                        return
                    requests[message_id] = message
                # Persist the receipt, then defer an addressed request before
                # attachment parsing, memory writes, or prompt construction.
                # The retry loop fetches the Discord message when capacity is
                # available; no per-event wait task is left behind.
                if directed and self._reply_queue.full:
                    await MaxwellBot._record_request_outcome(self, message, "deferred", "global_queue_capacity")
                    logger.info(
                        "inbound mid=%s cid=%s stage=admission_deferred reason=global_capacity",
                        message_id,
                        channel_id,
                    )
                    return
            elif not self._inbound_dedup.check_and_add(message_id):
                return
            # Journal first. Keep an outage cursor frozen while newer live
            # receipts arrive; they are independently durable in the journal.
            if not is_user_install_message(message) and channel_id not in (getattr(self, "_recovery_cursors", None) or {}):
                self._watermarks.note(
                    channel_id,
                    message_id,
                )
            if journal is not None:
                await journal.submit(journal.update, message_id, "received")
            self._dispatch_plugin_event("on_message", message)
            async with asyncio.timeout(
                MaxwellBot._inbound_setting(
                    self,
                    "live_turn_timeout_seconds",
                    DEFAULT_CONTROL["live_turn_timeout_seconds"],
                    1,
                    14400,
                )
            ):
                reason = await self._on_message_impl(message)
            row = (await MaxwellBot._request_state(self, message))
            if row.get("status") == "received":
                await MaxwellBot._record_request_outcome(self, message, "suppressed", reason or "not_addressed")
        except asyncio.CancelledError:
            await MaxwellBot._record_request_outcome(self, message, "deferred", "admission_cancelled")
            raise
        except Exception as exc:
            logger.warning(
                "inbound mid=%s cid=%s stage=admission_error error=%s",
                message_id,
                channel_id,
                type(exc).__name__,
            )
            with contextlib.suppress(Exception):
                await notify_owner(
                    self,
                    kind="error",
                    title=f"inbound {type(exc).__name__}",
                    details=f"message_id={message_id} channel_id={channel_id}",
                    message=message,
                    exc=exc,
                )
            row = (await journal.submit(journal.get, message_id)) if journal is not None else None
            if row:
                if (
                    row["status"] not in _REQUEST_TERMINAL
                    and not row["effects_started"]
                ):
                    await journal.submit(journal.begin, message_id)
                await self._request_failure(
                    message,
                    f"admission:{type(exc).__name__}",
                    retryable=isinstance(
                        exc, (TimeoutError, ConnectionError, aiohttp.ClientError)
                    ),
                )
        finally:
            if acquired:
                processing.discard(message_id)
            logger.info(
                "inbound mid=%s cid=%s stage=admission_finished duration_ms=%d",
                message_id,
                channel_id,
                (time.monotonic() - started) * 1000,
            )

    async def _on_message_impl(self, message):
        if getattr(self, "_draining", False):
            return "draining"
        try:
            self._load_control()
        except Exception as e:
            # A transient file-read hiccup used to eat the message entirely.
            # The last known-good control is still in self._control, so
            # degrade to that instead of dropping real traffic.
            logger.error(
                "Failed to load control in on_message (%s); continuing with "
                "the previous snapshot",
                e,
            )
        preferences = getattr(self, "_user_preferences", None)
        if preferences is not None:
            try:
                stat = preferences.path.stat()
                revision = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
            except OSError:
                revision = None
            if getattr(self, "_privacy_preferences_revision", None) != revision:
                # Dashboard deletion is a separate process. Discard old content
                # caches before the next request can reuse them.
                for name in ("_media_context", "_message_snapshots", "_message_update_state", "_recent_users"):
                    cache = getattr(self, name, None)
                    if isinstance(cache, dict):
                        cache.clear()
                self._privacy_preferences_revision = revision
        with contextlib.suppress(Exception):
            self._clear_typing(
                getattr(getattr(message, "channel", None), "id", ""),
                getattr(getattr(message, "author", None), "id", ""),
            )
        # Each fresh user turn starts un-tainted. The taint flag is set by
        # fetch_url / web_search when they return untrusted content, and is
        # consulted by the destructive shell tool to gate execution.
        self.clear_message_taint(message)
        author = getattr(message, "author", None)
        if author is None:
            return "missing_author"
        if not author.bot:
            logger.info(
                "inbound mid=%s cid=%s stage=received author_id=%s",
                getattr(message, "id", ""),
                message.channel.id,
                author.id,
            )

        # BUG FIX: blacklist/ignore must be checked BEFORE command handling.
        # Previously, blacklisted users could still run text-prefix commands
        # because the blacklist check was after the command check.
        # Admins bypass so they can manage the blacklist.
        if (
            str(message.author.id) in self._blacklist
            or str(message.author.id)
            in set(self._control.get("ignore_users", []) or [])
        ) and not self._is_admin(message.author.id):
            return "ignored_author"

        # DMs are off unless reply_dms is on. When on, anyone can DM; the
        # turn still hides Discord mod/server tools (see DM_BLOCKED_TOOLS).
        if isinstance(message.channel, discord.DMChannel) and not self._dm_replies_allowed(
            message
        ):
            return "dm_replies_disabled"

        if (
            not getattr(author, "bot", False)
            and self._should_interrupt_inflight(message)
            and self._note_same_user_latest(message)
        ):
            self._interrupt_same_user(message)
            self._mark_same_user_interrupt(message)

        if (
            not is_user_install_message(message)
            and message.content
            and message.content.startswith(self.command_prefix)
            and not message.author.bot
        ):
            # Public text-prefix commands have moved to Discord application
            # commands. Unknown prefix-like text remains ordinary chat.
            command_name = self._retired_prefix_command_name(message)
            if command_name:
                await self._mark_request_effect(message)
                await message.channel.send(
                    "Text-prefix commands are retired. Use `/help` to see Maxwell's slash commands."
                )
                return "command"

        if not self._control.get("bot_enabled", True):
            return "bot_disabled"

        channel_id = str(message.channel.id)
        now = asyncio.get_running_loop().time()
        user_install = is_user_install_message(message)
        if not user_install:
            if now < self._stop_until.get(channel_id, 0):
                return "stop_active"
            if channel_id in set(self._control.get("blocked_channels", []) or []):
                return "blocked_channel"
            allowed = set(self._control.get("allowed_channels", []) or [])
            if allowed and channel_id not in allowed:
                return "channel_not_allowed"
            # /solo: this server is locked to one channel. Slash commands already
            # returned above, so an admin can still run `/solo arguments:off` anywhere.
            if self._solo_blocks(message):
                return "solo_restriction"

        payloads = iter_message_payloads(message)
        has_attachment = any(getattr(src, "attachments", None) for src in payloads)
        has_embed = any(getattr(src, "embeds", None) for src in payloads)
        has_sticker = any(getattr(src, "stickers", None) for src in payloads)

        try:
            cooldown = float(self._control.get("per_user_cooldown_seconds", 0.0) or 0)
        except (TypeError, ValueError):
            cooldown = 0.0
        last = self._cooldowns.get(str(message.author.id))
        is_direct = self._directly_addressed(message)
        is_owner = (
            self._is_admin(message.author.id) if hasattr(self, "_is_admin") else False
        )
        cooldown_for_reply = (
            math.isfinite(cooldown)
            and cooldown > 0
            and last is not None
            and now - last < cooldown
            and not is_direct
            and not is_owner
        )
        self._cooldowns[str(message.author.id)] = now
        if len(self._cooldowns) > 1000:
            cutoff = now - 60
            self._cooldowns = {k: v for k, v in self._cooldowns.items() if v > cutoff}

        # Update user cache for this conversation early (system messages may
        # carry no author object — welcome/join events have None).
        if getattr(message, "author", None) is not None:
            self._update_recent_users(channel_id, message.author)
        for u in getattr(message, "mentions", []) or []:
            self._update_recent_users(channel_id, u)

        if self.user and message.author.id == self.user.id:
            if (
                message.content or has_attachment or has_embed or has_sticker
            ) and self._control.get("store_memory", True):
                # Dedup contract: memory.add_to_channel_memory dedups by message_id,
                # so an autonomy-force-recorded post (same message_id) only merges
                # metadata here — its autonomy tag/reason are preserved.
                try:
                    await self.add_message_to_memory(
                        channel_id,
                        self._message_memory_item(message),
                        message,
                    )
                    self._update_recent_users(channel_id, self.user)
                except Exception as e:
                    logger.warning(f"Self-message memory write failed: {type(e).__name__}")
            try:
                await self._arm_watch_from_own_message(message)
            except Exception as e:
                logger.debug("Conversation watch arm from own message failed: %s", e)
            # 2026-07-21: even with reply_to_bots on, never generate a
            # reply to a self-message. Once the bot starts replying to
            # its own posts the channel turns into a self-monologue
            # (transcript grows unbounded, every turn sees N-1 assistant
            # turns, model degrades to single-character outputs like
            # '.' or '?' because there's no real human content to react
            # to). The bot's reply is already on the wire; the user
            # doesn't need a second one. The bot-self branch above
            # already records the message so the next human message
            # sees it as context.
            return "self_message"

        pointer_only = is_pointer_only(message_text_without_mentions(message, self))
        reference = getattr(message, "reference", None)
        has_reference = bool(
            reference
            and getattr(reference, "message_id", None)
            and not message_reference_is_forward(message)
        )
        # A bare Discord reply has no text yet. Fetch it before treating the
        # event as empty. Pointer-only lines are visible and resolved below.
        if not message_has_visible_payload(message) and not has_reference:
            return "empty_payload"

        # Resolve the referenced message before acquiring the channel lock so
        # the same-user interrupt below can tell whether this is a reply to
        # Maxwell, and so the lock isn't held during the fetch.
        if (
            message.reference
            and not message.reference.resolved
            and message.reference.message_id
            and not message_reference_is_forward(message)
        ):
            try:
                message.reference.resolved = await asyncio.wait_for(
                    message.channel.fetch_message(message.reference.message_id),
                    timeout=10,
                )
            except Exception as e:
                # A caret or "this" can still use the previous channel message.
                # Other unpinged replies keep the bounded unresolved retry.
                if not pointer_only and not self._directly_addressed(message):
                    journal = getattr(self, "_request_journal", None)
                    if journal is not None:
                        await journal.submit(journal.begin, message.id)
                        await self._request_failure(
                            message,
                            f"unresolved_reply:{type(e).__name__}",
                            retryable=not isinstance(
                                e, (discord.Forbidden, discord.NotFound)
                            ),
                        )
                    return "unresolved_reply"
        with contextlib.suppress(Exception):
            await self._ensure_reply_chain_resolved(message)
        with contextlib.suppress(Exception):
            await hydrate_turn_context(self, message)
        hydrated = str(hydration_value(self, message, "hydrated_context", "") or "")
        parent_ok = reference_payload_usable(self._reply_parent(message))
        if (
            not message_has_visible_payload(message)
            and not hydrated
            and not parent_ok
        ):
            return "empty_payload"

        # Serialize only the memory/bookkeeping write for this room. Reply
        # generation happens AFTER the lock is released, through the reply
        # queue — holding this across a provider call is what made the
        # acquire below time out routinely and throw the message away.
        _lock = self._get_channel_lock(channel_id)
        _lock_acquired = False
        try:
            await asyncio.wait_for(
                _lock.acquire(), timeout=self._channel_lock_timeout()
            )
            _lock_acquired = True
        except asyncio.TimeoutError:
            # Now genuinely anomalous: the only thing under this lock is a
            # short SQLite write. Store best-effort and keep going — the
            # reply is no longer forfeited by a slow memory write.
            logger.warning(
                "Channel lock timeout for %s; storing memory unsynchronized "
                "and continuing to the reply path",
                channel_id,
            )
        try:
            if self._control.get("store_memory", True):
                memory_item = self._message_memory_item(message)
                try:
                    await self.add_message_to_memory(channel_id, memory_item, message)
                except asyncio.CancelledError:
                    # 2026-07-31: CancelledError is BaseException, not Exception.
                    # The previous `except Exception` block silently dropped the
                    # write on same-user-interrupt cancels — the channel-lock
                    # releaser in the `finally` raced the await and killed the
                    # coroutine mid-INSERT. Log it explicitly so silent message
                    # drops become visible.
                    logger.warning(
                        f"Memory write cancelled in on_message "
                        f"(msg_id={memory_item.get('message_id')} channel={channel_id})"
                    )
                    raise  # surface cancellation up to the caller
                except Exception as e:
                    logger.warning(f"Memory write failed in on_message: {type(e).__name__}")
        finally:
            # The critical section ends here. Everything below — media
            # downloads, gating, and the reply itself — runs unlocked, so a
            # slow turn in this room can no longer make the NEXT message's
            # lock acquire time out and lose it.
            if _lock_acquired:
                _lock.release()

        # Cache media context for EVERY message in an allowed channel,
        # not just pinged ones. Without this, an image posted without a
        # ping never enters visual memory, so a later ping about "this"
        # or "the image above" has nothing to attach. This is the fix for
        # "the bot can't see sent media in channels if it's not pinged".
        channel_obj = getattr(message, "channel", None)
        reply_path_enabled = (
            (
                isinstance(channel_obj, discord.DMChannel)
                and self._dm_replies_allowed(message)
            )
            or (
                isinstance(channel_obj, discord.GroupChannel)
                and self._control.get("reply_groups", True)
            )
            or (
                getattr(message, "guild", None) is not None
                and self._control.get("reply_mentions", True)
            )
        )
        if (
            MaxwellBot._image_input_enabled(self)
            and not (reply_path_enabled and self._should_live_reply(message))
            and self._message_carries_media(message)
        ):
            try:
                _imgs, bg_media = await self._extract_media(message)
                bg_media.extend(await self._extract_embeds(message))
                bg_media.extend(
                    await self._extract_linked_media(
                        message,
                        skip_urls={
                            str(item.get("url")) for item in bg_media if item.get("url")
                        },
                    )
                )
                if bg_media:
                    self._cache_media_context(channel_id, bg_media)
            except Exception as e:
                logger.warning(f"Background media cache failed: {type(e).__name__}")

        if message.author.bot:
            if not self._control.get("reply_to_bots", True):
                return "bot_author"

        # Every human line updates the room's pace and engagement, even
        # the ones that never become a turn — deliberately above the
        # cooldown return, so a room's rhythm is already measured by the
        # time he first speaks there and arms the watch.
        with contextlib.suppress(Exception):
            self._note_watch_message(message)

        # 2026-07-31: per-user cooldown was previously an early-return at
        # bot.py:2823 that ate ~2/3 of channel traffic before it could
        # reach memory. Now it's applied here, AFTER storage but BEFORE
        # an LLM turn — so RAG keeps every message but the bot doesn't
        # burn provider calls replying to every rapid-fire text.
        if cooldown_for_reply and not message.author.bot:
            if not self._should_live_reply(message) and not self._is_same_user_interrupt(message):
                logger.info(
                    f"Cooldown skip reply for user {message.author.id} in {channel_id} (still stored to memory)"
                )
                return "cooldown"

        if is_user_install_message(message):
            await self._dispatch_reply(message,
            self._content_without_self_mention(message.content, message),
            directed=True,)
            return

        if isinstance(message.channel, discord.DMChannel):
            if not self._dm_replies_allowed(message):
                return "dm_replies_disabled"
            await self._dispatch_reply(message,
            self._content_without_self_mention(message.content, message),
            directed=True,)
            return

        if isinstance(message.channel, discord.GroupChannel):
            if not self._control.get("reply_groups", True):
                self._touch_watch_debounce(message)
                return "group_replies_disabled"
            await self._maybe_live_reply(
                message, self._content_without_self_mention(message.content, message)
            )
            return

        if message.guild:
            if not self._control.get("reply_mentions", True):
                self._touch_watch_debounce(message)
                return "mention_replies_disabled"
            clean = self._content_without_self_mention(message.content, message)
            # Bare @Maxwell with no extra text: still a turn. Do not
            # invent "look at this" — he should read the room (and any
            # reply-parent) and answer from that.
            await self._maybe_live_reply(message, clean)

    async def on_typing(self, channel, user, when):
        """Discord TYPING_START — someone is composing in a room we can see."""
        if not MaxwellBot._dev_scope_allows(self, channel):
            return
        self._dispatch_plugin_event("on_typing", channel, user, when)
        try:
            self._note_typing(channel, user)
        except Exception:
            logger.debug("Failed recording typing", exc_info=True)

    def _retired_prefix_command_name(self, message) -> str:
        prefix = str(getattr(self, "command_prefix", None) or ",")
        raw = str(getattr(message, "content", "") or "")
        if not prefix or not raw.startswith(prefix):
            return ""
        parts = raw[len(prefix) :].strip().split(maxsplit=1)
        name = parts[0].lower() if parts else ""
        return name if name in _RETIRED_PREFIX_COMMANDS else ""

    async def _handle_solo_command(self, message, args):
        """`/solo` — lock a server to one channel, or unlock it.

        One command each way. Setting it silences every other channel in this
        server AND stops autonomy from starting anything here; clearing it puts
        the server back exactly as it was. Nothing about other servers changes,
        which is why this is its own per-guild map rather than the global
        allowed_channels list.
        """
        if message.guild is None:
            await message.channel.send("`/solo` only makes sense in a server.")
            return
        gid = str(message.guild.id)
        arg = (args or "").strip()
        mapping = dict(self._control.get("guild_solo_channel") or {})
        current = str(mapping.get(gid) or "")

        if arg.lower() in {"status", "?"}:
            if not current:
                await message.channel.send(
                    "Not locked — I reply anywhere in this server I'm allowed to. "
                    "`/solo` here to lock me to this channel."
                )
            else:
                await message.channel.send(
                    f"Locked to <#{current}>. Everywhere else in this server is "
                    "silent and autonomy is off. `/solo arguments:off` to unlock."
                )
            return

        if arg.lower() in {"off", "clear", "none", "unlock", "reset"}:
            if not current:
                await message.channel.send("Wasn't locked.")
                return
            mapping.pop(gid, None)
            await self._save_solo(mapping, gid, unblock_autonomy=True)
            await message.channel.send(
                "Unlocked. I'll reply anywhere in this server I'm allowed to "
                "again, and autonomy is back on here."
            )
            return

        # `/solo` with no arguments locks to the channel it was run in;
        # `/solo arguments:#channel` (or a raw ID) locks to that one.
        target = str(message.channel.id)
        if arg:
            match = re.search(r"\d{5,}", arg)
            if not match:
                await message.channel.send(
                    "Usage: `/solo` (lock to this channel), `/solo arguments:#channel`, "
                    "`/solo arguments:off`, `/solo arguments:status`."
                )
                return
            target = match.group(0)
            found = message.guild.get_channel(int(target))
            if found is None:
                await message.channel.send(
                    "That channel isn't in this server — pick one here."
                )
                return

        mapping[gid] = target
        await self._save_solo(mapping, gid, unblock_autonomy=False)
        where = "this channel" if target == str(message.channel.id) else f"<#{target}>"
        await message.channel.send(
            f"Locked to {where}. Every other channel in **{message.guild.name}** "
            "is silent for me now, and I won't start anything on my own here. "
            "`/solo arguments:off` undoes it."
        )

    async def _save_solo(self, mapping: dict, gid: str, *, unblock_autonomy: bool):
        """Persist the solo map, and keep autonomy's server blacklist in step.

        'No autonomy' is half the point of the command, so locking a server
        also stops the autonomy engine picking it — otherwise Maxwell would go
        quiet on people and still start conversations by himself.
        """
        from plugins.maxwell_extras.dashboard_settings import save_server_settings

        self._control = await asyncio.to_thread(
            save_server_settings, Path(self.config.DATA_DIR), str(gid),
            {"channel": str(mapping.get(gid) or "")}, fallback=self._control,
        )
        loader = getattr(self, "_load_control", None)
        if callable(loader):
            loader(force=True)

    def _get_reply_context(self, message) -> str:
        if not message.reference or not isinstance(
            message.reference, discord.MessageReference
        ):
            return ""
        if message_reference_is_forward(message):
            return ""
        ref = cast(Any, message.reference.resolved)
        if not ref or not hasattr(ref, "author"):
            return ""
        ch_id = str(
            getattr(
                message,
                "channel_id",
                getattr(getattr(message, "channel", None), "id", "") or "",
            )
        )
        ref_content = render_discord_context_text(
            ref, ref.content or "", known_users=self._recent_users.get(ch_id, {})
        )
        if ref.attachments:
            ref_content = (ref_content + " [media attached]").strip()
        if not ref_content:
            return ""
        ref_author_id = str(getattr(ref.author, "id", "unknown"))
        if self.user and ref.author.id == self.user.id:
            ref_label = f"you/Maxwell({ref_author_id})"
        else:
            ref_label = f"{ref.author.display_name}({ref_author_id})"
        return f"\n[Latest message replies to {ref_label}: {ref_content[:500]}]"

    def _get_music_context(self, message) -> str:
        parts = [
            f"[Spotify {match.group(1)}: open.spotify.com/{match.group(1)}/{match.group(2)}]"
            for match in re.finditer(
                r"https?://open\.spotify\.com/(track|album|playlist|artist)/([a-zA-Z0-9]+)",
                message_combined_content(message) or "",
            )
        ]
        author = getattr(message, "author", None)
        if author is None:
            return "\n".join(parts)
        presence_bits: list[str] = []
        status = getattr(author, "status", None) or getattr(author, "raw_status", None)
        status_name = str(getattr(status, "name", status) or "").strip().lower()
        if status_name and status_name not in {"offline", "invisible", ""}:
            presence_bits.append(status_name)
        for activity in list(getattr(author, "activities", None) or []):
            bit = self._format_presence_activity(activity)
            if bit and bit not in presence_bits:
                presence_bits.append(bit)
        voice = getattr(author, "voice", None)
        vch = getattr(voice, "channel", None) if voice is not None else None
        if vch is not None:
            vname = (
                str(getattr(vch, "name", "") or "").strip()
                or str(getattr(vch, "id", "") or "voice")
            )
            extras = []
            if getattr(voice, "self_stream", False) or getattr(voice, "self_video", False):
                extras.append("streaming")
            if getattr(voice, "self_mute", False) or getattr(voice, "mute", False):
                extras.append("muted")
            if getattr(voice, "self_deaf", False) or getattr(voice, "deaf", False):
                extras.append("deafened")
            extra = f" ({', '.join(extras)})" if extras else ""
            presence_bits.append(f"in voice #{vname}{extra}")
        timed = getattr(author, "timed_out_until", None)
        if timed:
            presence_bits.append(f"timed out until {timed}")
        if presence_bits:
            parts.append("[presence: " + " | ".join(presence_bits) + "]")
        return "\n".join(parts)



    def _visible_event_content(self, message, content: str | None = None) -> str:
        text = render_discord_context_text(
            message,
            content if content is not None else (getattr(message, "content", "") or ""),
            known_users=self._recent_users.get(
                str(getattr(getattr(message, "channel", None), "id", "") or ""), {}
            ),
        )
        text = re.sub(
            r"<think\b[^>]*>.*?</think>", "", str(text), flags=re.IGNORECASE | re.DOTALL
        ).strip()
        parts = [text] if text else []
        for attachment in self._payload_attr_list(message, "attachments", 5):
            content_type = getattr(attachment, "content_type", "") or ""
            if content_type.startswith("image/"):
                kind = "image"
            elif content_type.startswith("audio/"):
                kind = "audio"
            elif content_type.startswith("video/"):
                kind = "video"
            else:
                kind = "file"
            parts.append(f"[{kind}]")
        if any(getattr(src, "embeds", None) for src in iter_message_payloads(message)):
            parts.append("[embed]")
        return " ".join(p for p in parts if p).strip()


    def _channel_turn_active(self, channel_id: str) -> bool:
        """True when a reply/tool turn is in-flight for this channel."""
        active = (getattr(self, "_active_requests", None) or {}).get(channel_id)
        if active is not None and not active.done():
            return True
        return channel_id in (getattr(self, "_replying_channels", None) or set())

    async def _command_queue_loop(self):
        path = Path(self.config.DATA_DIR) / "bot_commands.json"
        while True:
            if getattr(self, "_draining", False):
                return
            await asyncio.sleep(2)
            if getattr(self, "_draining", False):
                return
            begin_inflight(self, "command")
            try:
                if not path.exists():
                    continue
                try:
                    raw = await asyncio.to_thread(path.read_text, encoding="utf-8")
                    commands_data = json.loads(raw)
                except Exception as read_err:
                    # Corrupt command queue: back it up (don't lose potential data) and reset so
                    # dashboard commands can flow again. Matches the "refuse to clobber corrupt"
                    # spirit but for the consumer side we must recover to keep the system alive.
                    try:
                        backup = path.with_suffix(
                            path.suffix + ".corrupt-" + str(_safe_int(time.time(), 0))
                        )
                        path.rename(backup)
                        logger.error(
                            f"Corrupt bot_commands.json backed up to {backup}: {read_err}"
                        )
                    except Exception:
                        logger.error(
                            f"Corrupt bot_commands.json and failed to backup: {read_err}"
                        )
                    commands_data = []
                    # Recreate a clean empty queue file so future dashboard commands work immediately.
                    try:
                        await asyncio.to_thread(_atomic_json_write_sync, path, [])
                    except Exception as werr:
                        logger.error(
                            f"Failed to reset clean bot_commands.json after corrupt: {werr}"
                        )
                if not isinstance(commands_data, list):
                    continue
                changed = False
                for cmd in commands_data:
                    if cmd.get("status") != "pending":
                        continue
                    changed = True
                    try:
                        typ = cmd.get("type", "")
                        if typ == "send_message":
                            ch = cast(
                                Any,
                                self.get_channel(_safe_int(cmd["channel_id"]))
                                or await self.fetch_channel(
                                    _safe_int(cmd["channel_id"])
                                ),
                            )
                            await ch.send(cmd["content"])
                            cmd["result"] = "sent"
                        elif typ == "send_dm":
                            uid = _safe_int(cmd.get("user_id"))
                            user = self.get_user(uid) if uid else None
                            if user is None and uid:
                                try:
                                    user = await self.fetch_user(uid)
                                except Exception as e:
                                    logger.warning(
                                        "send_dm failed to fetch user %s: %s", uid, e
                                    )
                                    user = None
                            if user is None:
                                cmd["result"] = (
                                    f"error: user {cmd.get('user_id')} not found"
                                )
                                cmd["status"] = "failed"
                            else:
                                try:
                                    dm_channel = getattr(user, "dm_channel", None)
                                    if dm_channel is None:
                                        dm_channel = await user.create_dm()
                                    await dm_channel.send(cmd["content"])
                                    cmd["result"] = "dm sent"
                                    cmd["status"] = "done"
                                except discord.Forbidden as f_err:
                                    cmd["result"] = (
                                        f"error: forbidden (user has DMs disabled or blocked bot): {f_err}"
                                    )
                                    cmd["status"] = "failed"
                                except Exception as dm_err:
                                    cmd["result"] = f"error: {dm_err}"
                                    cmd["status"] = "failed"
                        elif typ == "set_presence":
                            status_map = {
                                "online": discord.Status.online,
                                "idle": discord.Status.idle,
                                "dnd": discord.Status.dnd,
                                "invisible": discord.Status.invisible,
                            }
                            presence_status = (
                                cmd.get("presence_status")
                                or cmd.get("discord_status")
                                or cmd.get("presence")
                                or "online"
                            )
                            acts = self._build_activities()
                            await self.change_presence(
                                status=status_map.get(
                                    presence_status, discord.Status.online
                                ),
                                activity=acts[0] if acts else None,
                            )
                            cmd["result"] = "presence updated"
                        elif typ == "set_custom_status":
                            text = cmd.get("text", "")
                            self._custom_status = (
                                discord.CustomActivity(name=text, state=text)
                                if text
                                else None
                            )
                            acts = self._build_activities()
                            await self.change_presence(
                                activity=acts[0] if acts else None,
                            )
                            cmd["result"] = "custom status updated"
                        elif typ == "change_avatar":
                            url = cmd.get("url", "")
                            if url:
                                if not _is_safe_url(url):
                                    cmd["result"] = "error: unsafe avatar URL"
                                else:
                                    session = await _get_shared_session()
                                    async with session.get(
                                        url,
                                        timeout=aiohttp.ClientTimeout(total=30),
                                        allow_redirects=False,
                                    ) as resp:
                                        if resp.status == 200:
                                            content_type = resp.headers.get(
                                                "Content-Type", ""
                                            )
                                            if not content_type.startswith("image/"):
                                                cmd["result"] = (
                                                    "error: avatar URL did not return an image"
                                                )
                                            else:
                                                avatar = await _read_response_limited(
                                                    resp, 10 * 1024 * 1024
                                                )
                                                if self.user is not None:
                                                    await self.user.edit(avatar=avatar)
                                                cmd["result"] = "avatar changed"
                                        else:
                                            cmd["result"] = f"HTTP {resp.status}"
                        elif typ == "clear_memory":
                            if cmd.get("channel_id"):
                                cid = str(cmd["channel_id"])
                                await self.memory.clear_channel_memory(cid)
                                self._media_context.pop(cid, None)
                                self._stop_until.pop(cid, None)
                                self._drugged_until.pop(cid, None)
                                cmd["result"] = "memory cleared"
                        elif typ == "reload_controls":
                            self._load_control(force=True)
                            self._load_admins()
                            self._load_auto_channels()
                            self._load_blacklist()
                            self._load_shell_whitelist()
                            cmd["result"] = "controls reloaded"
                        elif typ == "plugin_reload_state":
                            pm = getattr(self, "plugin_manager", None)
                            if pm is None:
                                cmd["result"] = "plugin manager missing"
                            else:
                                pm._load_state()
                                pm.sync_bot_tools()
                                cmd["result"] = "plugin state reloaded"
                        elif typ == "plugin_reload":
                            pm = getattr(self, "plugin_manager", None)
                            if pm is None:
                                cmd["result"] = "plugin manager missing"
                            else:
                                cmd["result"] = await pm.reload_plugins_async()
                                self.hooks = getattr(pm, "hooks", None)
                                self.prompts = getattr(pm, "prompts", None)
                                self.tool_registry = getattr(pm, "tool_registry", None)
                                installer = getattr(self, "_install_core_prompt_components", None)
                                if callable(installer):
                                    installer()
                                publisher = getattr(self, "_publish_core_services", None)
                                if callable(publisher):
                                    publisher()
                        elif typ in ("plugin_enable", "plugin_disable"):
                            pm = getattr(self, "plugin_manager", None)
                            if pm is None:
                                cmd["result"] = "plugin manager missing"
                            else:
                                name = str(cmd.get("plugin") or "").strip()
                                is_global = bool(cmd.get("is_global", True))
                                user_id = cmd.get("user_id")
                                if typ == "plugin_enable":
                                    cmd["result"] = pm.enable_plugin(
                                        name, user_id=user_id, is_global=is_global
                                    )
                                else:
                                    cmd["result"] = pm.disable_plugin(
                                        name, user_id=user_id, is_global=is_global
                                    )
                        elif typ == "plugin_config":
                            pm = getattr(self, "plugin_manager", None)
                            if pm is None:
                                cmd["result"] = "plugin manager missing"
                            else:
                                cmd["result"] = pm.set_plugin_config(
                                    str(cmd.get("plugin") or ""),
                                    cmd.get("config") or {},
                                )
                        elif typ == "plugin_install":
                            pm = getattr(self, "plugin_manager", None)
                            if pm is None:
                                cmd["result"] = "plugin manager missing"
                            else:
                                cmd["result"] = pm.install_from_path(
                                    str(cmd.get("path") or ""),
                                    replace=bool(cmd.get("replace")),
                                )
                        elif typ == "plugin_uninstall":
                            pm = getattr(self, "plugin_manager", None)
                            if pm is None:
                                cmd["result"] = "plugin manager missing"
                            else:
                                cmd["result"] = pm.uninstall_plugin(
                                    str(cmd.get("plugin") or "")
                                )
                        elif typ in {"rem_run", "rem_enable", "rem_disable"}:
                            cmd["result"] = "REM and background memory agents were removed"
                        elif str(typ).startswith("autonomy_"):
                            cmd["result"] = "Autonomous background activity has been removed"
                        elif typ == "context_cleanup_run" or typ in (
                            "context_cleanup_enable",
                            "context_cleanup_disable",
                            "context_cleanup_interval",
                        ):
                            cmd["result"] = (
                                "context cleanup engine removed (conversation history only)"
                            )
                        elif typ == "inbox_act":
                            cmd["result"] = await apply_inbox_action(
                                self,
                                action=str(cmd.get("action") or ""),
                                item_id=str(cmd.get("item_id") or ""),
                                user_id=str(cmd.get("user_id") or ""),
                            )
                        else:
                            cmd["result"] = "unknown command"
                    except Exception as e:
                        cmd["result"] = f"error: {e}"
                    cmd["status"] = "done"
                if changed:
                    # Race mitigation: re-load fresh list (API may have appended during our long work)
                    # and overlay our "done" results so we don't clobber new pending commands.
                    # Additionally hold a cross-process FileLock around the read+merge+write
                    # to reduce (but not eliminate) window where concurrent appends are lost.
                    snapshot = list(commands_data)  # the ones we just marked done

                    def _merge_and_write(snapshot=snapshot):
                        try:
                            fresh_raw = path.read_text(encoding="utf-8")
                            fresh = json.loads(fresh_raw) if fresh_raw.strip() else []
                        except Exception:
                            fresh = []
                        if isinstance(fresh, list):
                            # Match completed work by stable command id only.
                            done_by_id = {
                                str(our.get("id") or ""): our
                                for our in snapshot
                                if our.get("status") == "done" and our.get("id")
                            }
                            for fc in fresh:
                                cid = str(fc.get("id") or "")
                                if cid and cid in done_by_id:
                                    our = done_by_id[cid]
                                    fc["status"] = "done"
                                    fc["result"] = our.get("result")
                            to_write = fresh
                        else:
                            to_write = snapshot
                        _atomic_json_write_sync(path, to_write)
                        return to_write

                    try:
                        with FileLock(path, timeout=10.0):
                            await asyncio.to_thread(_merge_and_write)
                    except Exception as lock_err:
                        # Fail closed on lock timeout: keep pending so the next loop
                        # retries instead of rewriting a stale snapshot that drops API
                        # appends. Log and continue.
                        logger.warning(
                            "Command queue merge deferred (lock/write failed): %s",
                            lock_err,
                        )
            except Exception as e:
                logger.error(f"Command queue error: {type(e).__name__}")
            finally:
                end_inflight(self, "command")

    async def _acknowledge_read_only_channel(self, message) -> bool:
        from discord_expressions import cannot_send
        if not cannot_send(message, self):
            return False
        # No model call, typing/progress post, retry or DM for an unsendable ping.
        handler = (getattr(self, "tools", None) or {}).get("react")
        if handler is not None and self._directly_addressed(message):
            emoji = "🔇"
            for reaction in getattr(message, "reactions", ()):
                candidate = str(getattr(reaction, "emoji", ""))
                if candidate and "<" not in candidate:
                    emoji = candidate
                    break
            try:
                result = await MaxwellBot._invoke_request_tool(
                    self, message, "react", handler, emoji=emoji, finish=True
                )
                if str(result).startswith("__REACTION_SENT__"):
                    return True
            except (PermissionError, discord.HTTPException):
                pass
        await MaxwellBot._record_request_outcome(self, message, "suppressed", "channel_send_unavailable")
        return True

    async def _keep_work_draft_inside_subagent(self, message, content, response: str) -> str:
        """Long working notes stay with the subagent. The channel gets a short line."""
        text = str(response or "")
        lowered = text.lower()
        draft = len(text) >= 1200 and (
            text.count("```") >= 2
            or ("```" in text and any(cue in lowered for cue in ("let me write", "i'll use", "now write")))
        )
        if not draft:
            return text
        names = MaxwellBot._turn_tool_names(
            self, self._message_tool_platform(message), message
        )
        tool = (getattr(self, "tools", None) or {}).get("delegate_task")
        if tool is not None and "delegate_task" in names:
            task = str(content or getattr(message, "content", "") or "").strip()[:4000]
            if task:
                try:
                    await tool.execute(message, tasks=[{"task": task}])
                except Exception:
                    logger.warning("Could not hand a work draft to the subagent", exc_info=True)
            return "Handed that to a subagent. The working notes stay there."
        return "That working draft stays off this chat. Building it needs a subagent, which needs Premium."

    async def _deliver_normal_reply(self, message, response, active_progresses):
        """Deliver final text and native stickers with a single first-chunk owner."""
        response = _auto_format_discord(response)
        response = self._render_custom_emojis(response, message.guild)
        response, send_stickers = self._extract_stickers_from_text(
            response, message.guild
        )
        response = ensure_web_references(response)
        chunks = self._split_response(response, limit=1900)
        if not chunks and send_stickers:
            chunks = [""]
        transitioned = False
        if send_stickers:
            # Progress transitions deliver text only, and their edit fallback
            # cannot attach native stickers. Retire progress and keep delivery
            # (including Discord errors) in the normal awaited send path.
            for progress in active_progresses:
                if progress is not None:
                    with contextlib.suppress(Exception):
                        await progress.stop()
        elif chunks and chunks[0] and not (await MaxwellBot._request_state(self, message)):
            for progress in reversed(active_progresses):
                if progress is None:
                    continue
                with contextlib.suppress(Exception):
                    if await progress.transition_to_final(chunks[0]):
                        transitioned = True
                        break
            if transitioned:
                await typing_suspend(self, message.channel)
        reply_delivered = bool(transitioned)
        async with self._reply_typing(message.channel, response, message=message):
            for i, chunk in enumerate(chunks):
                if i == 0 and transitioned:
                    continue
                elif i == 0:
                    try:
                        sent = await self._send_with_slowmode(
                            message.channel,
                            content=chunk,
                            reply_to=message,
                            stickers=send_stickers,
                        )
                    except (discord.NotFound, discord.HTTPException) as exc:
                        # Keep the deleted-parent fallback for reply shims that
                        # bypass the sender's own unknown-reference handling.
                        if not _is_unknown_reference_error(exc):
                            raise
                        logger.warning(
                            "message.reply parent is gone, falling back to channel.send in channel %s",
                            getattr(message.channel, "id", "?"),
                        )
                        sent = await self._send_with_slowmode(
                            message.channel, content=chunk, stickers=send_stickers
                        )
                    if sent is None:
                        break
                    reply_delivered = True
                else:
                    sent = await self._send_with_slowmode(
                        message.channel, content=chunk
                    )
                    if sent is None:
                        break
                    reply_delivered = True
        return response, reply_delivered

    async def _handle_message(self, message, content: str | None = None):
        content = content or message.content
        channel_id = str(message.channel.id)
        if await MaxwellBot._acknowledge_read_only_channel(self, message):
            return
        # Sleep gate: when the bot is in a sleep window, abort the
        # dispatch, send a one-shot notice in the triggering channel
        # saying "Max is sleeping, back in Xm", and return. Never DM.
        # Dedups per user so a 30-min sleep doesn't spam 40 lines
        # when someone pings 40 times. The 2026-07-19 user report:
        # the bot kept spamming goodnight/goodbye in chat; a real
        # sleep window is the structural fix.
        if not await self._check_sleep_gate(message):
            return
        turn_context = self._begin_inflight_context(message, content)
        live_typing = await self._enter_live_typing(message)
        turn_context["live_typing"] = live_typing
        author = getattr(message, "author", None)
        if (
            author is not None
            and not getattr(author, "bot", False)
            and self._directly_addressed(message)
            and str(getattr(message, "response_visibility", "public") or "public") != "private"
        ):
            self._arm_conversation_watch(channel_id)
        normal_reply_sent = False
        # Mark this channel as in-flight (bot is generating a reply) so autonomy
        # can skip posting into it and avoid racing the real reply.
        self._replying_channels.add(channel_id)
        current_task = asyncio.current_task()
        from provider_limits import request_timeout_seconds, turn_timeout_seconds
        ai_timeout = request_timeout_seconds(self, _current_request_provider.get())
        max_out_tokens = min(
            getattr(self.config, "AI_MAX_OUTPUT_TOKENS", 16384) or 16384,
            max(256, _safe_int(self._control.get("live_max_output_tokens", 4096), 4096)),
        )
        byok_provider = _current_request_provider.get()
        byok_tokens = getattr(byok_provider, "max_tokens", None) if byok_provider is not None else None
        if isinstance(byok_tokens, int) and byok_tokens > 0:
            max_out_tokens = max(16, min(byok_tokens, 16384))
        elif self._is_short_live_turn(message, content):
            # Banter does not need a 16k output budget.
            max_out_tokens = min(int(max_out_tokens), 4096)
        try:
            # MESSAGE_CREATE can precede Discord's unfurl by a few hundred
            # milliseconds. Refresh once before extracting so a direct ping
            # gets the thumbnail/embed in its first provider request.
            refreshed_message = await self._wait_for_late_embeds(message, content)
            if refreshed_message is not message:
                message = refreshed_message
                content = str(getattr(message, "content", "") or "")
                turn_context["message"] = message
                turn_context["content"] = content
                turn_context["message_ids"].add(str(getattr(message, "id", "") or ""))
                if (
                    not turn_context.get("version")
                    and self._control.get("store_memory", True)
                    and getattr(self, "memory", None)
                ):
                    with contextlib.suppress(Exception):
                        await self.add_message_to_memory(
                            str(getattr(message.channel, "id", channel_id)),
                            self._message_memory_item(message, edited=True),
                            message,
                        )
            _images, media = await self._extract_media(message)
            # Embed text remains useful even when image processing is off;
            # _extract_embeds gates only the binary downloads.
            media.extend(await self._extract_embeds(message))
            # Linked media gates images and audio separately, so it runs
            # outside the process_images check — a linked clip should still
            # land when only audio input is on. Embed downloads already
            # carry their source URL; skip those so an unfurled image is
            # not fetched and attached twice.
            media.extend(
                await self._extract_linked_media(
                    message,
                    skip_urls={
                        str(item.get("url")) for item in media if item.get("url")
                    },
                )
            )
            if MaxwellBot._image_input_enabled(self):
                grid = await self._maybe_emoji_grid(message, channel_id)
                if grid is not None:
                    media.append(grid)
            with contextlib.suppress(Exception):
                await self._ensure_reply_chain_resolved(message)
            chain = list(self._iter_resolved_reply_chain(message))
            first = chain[0] if chain else None
            walk_chain = (
                first is not None
                and self._directly_addressed(message)
                and not self._author_is_self(first)
            )
            targets = []
            if first is not None and (
                self._directly_addressed(message)
                or self._replying_to_own_message(message)
            ):
                targets = chain if walk_chain else [first]
            for ancestor in targets:
                _pimgs, parent_media = await self._extract_media(ancestor)
                parent_media.extend(await self._extract_embeds(ancestor))
                parent_media.extend(
                    await self._extract_linked_media(
                        ancestor,
                        skip_urls={
                            str(item.get("url"))
                            for item in media + parent_media
                            if item.get("url")
                        },
                    )
                )
                if parent_media:
                    media.extend(parent_media)
                    logger.info(
                        "Attached %s item(s) from replied-to message %s",
                        len(parent_media),
                        getattr(ancestor, "id", "?"),
                    )
                parent_id = str(getattr(ancestor, "id", "") or "")
                if parent_id:
                    turn_context["message_ids"].add(parent_id)
        except Exception as e:
            logger.warning(f"Media extraction failed: {type(e).__name__}")
            media = []
        current_images = [item for item in media if item.get("is_image")]
        cached_media = []
        reply_media_id = self._reply_media_message_id(
            message, getattr(self, "_message_snapshots", None)
        )
        is_dm = isinstance(getattr(message, "channel", None), discord.DMChannel)
        if reply_media_id is not None:
            cached_media = self._get_media_context(
                channel_id, message_id=reply_media_id
            )
        elif self._should_use_cached_media_context(message, content) and (
            not current_images or self._should_mix_cached_with_current(content)
        ):
            cached_media = self._get_media_context(channel_id)
        # DM INTERRUPT FIX: In DMs, if user sends image -> then another image/text quickly,
        # the second turn must see BOTH. The gated logic above drops cached in DMs unless
        # the text says "previous". Force-merge for DMs when there's recent cached media.
        if not cached_media and is_dm:
            dm_cached = self._get_media_context(channel_id)
            if dm_cached:
                # Dedupe by message_id+filename so we don't double-count the current image
                current_keys = {
                    (c.get("message_id"), c.get("filename")) for c in current_images
                }
                filtered = [
                    c
                    for c in dm_cached
                    if (c.get("message_id"), c.get("filename")) not in current_keys
                ]
                # Only merge if we have current images to compare against, OR if the DM has
                # no current image but user is clearly continuing (previous image within uses_left)
                # - image+image: current_images non-empty -> merge previous
                # - image then "what is this?": current_images empty but content likely refers to image -> merge
                if filtered:
                    # For image+image burst, always merge. For text follow-up, merge if we have any cached.
                    if current_images or len(filtered) > 0:
                        cached_media = filtered
                        logger.info(
                            f"DM merge: {len(current_images)} current + {len(cached_media)} cached (interrupt)"
                        )
        # Current attachments always go through. Cached images are gated above;
        # otherwise normal chat gets polluted by yesterday's meme/screenshot.
        active_media = current_images + cached_media + self._current_binary_media(media)
        media_summary = self._format_media_summary(media, active_media)
        # If the message carries attachable media but nothing made it into the
        # turn (a failed/partial extraction, an embed that didn't download, a
        # forward snapshot), don't leave the model blind — hand it the raw
        # attachment URLs so it can fetch them itself with see_image, and log
        # the miss so we can see it in the logs.
        if not active_media and self._message_carries_media(message):
            raw_urls = [
                str(getattr(a, "url", "") or "").strip()
                for a in self._payload_attr_list(message, "attachments", 5)
            ]
            raw_urls = [u for u in raw_urls if u.startswith("http")]
            if raw_urls:
                media_summary = (
                    media_summary
                    + "\nAttached media did not auto-attach; fetch it with "
                    + "see_image (one URL at a time): "
                    + ", ".join(raw_urls)
                ).strip()
                logger.warning(
                    "Media auto-attach miss; handing URLs to see_image: %s", raw_urls
                )
        # If MESSAGE_UPDATE refreshed this turn while its original extraction
        # was in flight, that extraction is stale. Let the edit handler's
        # replacement remain authoritative for subsequent turns.
        if not turn_context.get("version"):
            self._cache_media_context(channel_id, media)
        message_id = str(getattr(message, "id", "") or "")
        if message_id and not turn_context.get("version"):
            self._message_snapshots[message_id] = message
            self._message_update_state[message_id] = (
                self._message_update_fingerprint(message),
                self._message_media_fingerprint(message),
                time.monotonic(),
            )
            if len(self._message_update_state) > 1000:
                cutoff = time.monotonic() - 3600
                self._message_update_state = {
                    key: value
                    for key, value in self._message_update_state.items()
                    if value[2] > cutoff
                }
                self._message_snapshots = {
                    key: value
                    for key, value in self._message_snapshots.items()
                    if key in self._message_update_state
                }
        turn_context["message"] = message
        turn_context["content"] = content
        turn_context["media"] = list(media)
        turn_context["active_media"] = list(active_media)

        try:
            messages = await self._build_messages(
                message,
                content,
                has_media=bool(active_media),
                media_summary=media_summary,
            )
            seen_version_before = turn_context.get("seen_version", 0)
            (
                message,
                content,
                media,
                active_media,
                media_summary,
                messages,
            ) = await self._apply_inflight_refresh(
                turn_context,
                message,
                content,
                media,
                active_media,
                media_summary,
                messages,
            )
            if turn_context.get("seen_version", 0) != seen_version_before:
                # Rebuild so an edit that arrived mid-prep is what the model sees.
                messages = await self._build_messages(
                    message,
                    content,
                    has_media=bool(active_media),
                    media_summary=media_summary,
                )
            turn_context["message"] = message
            turn_context["content"] = content
            turn_context["media"] = list(media)
            turn_context["active_media"] = list(active_media)
            if turn_context.get("version") and message_id:
                # The refresh may have raced the initial MESSAGE_CREATE path.
                # Persist the post-refresh object so a later reply-parent
                # lookup cannot regress to the stale create snapshot.
                self._message_snapshots[message_id] = message
                self._message_update_state[message_id] = (
                    self._message_update_fingerprint(message),
                    self._message_media_fingerprint(message),
                    time.monotonic(),
                )
        except Exception as e:
            logger.error(f"Failed to build messages: {type(e).__name__}\n[traceback detail omitted]")
            await self._release_turn_context(turn_context)
            MaxwellBot._clear_turn_flags(self, channel_id, current_task)
            return
        if self._control.get(
            "require_direct_response", True
        ) and self._directly_addressed(message):
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "This message addresses you. Answer substantive questions/requests with send_message "
                        "or completed media. For a casual ping, joke, thanks or acknowledgement, "
                        "react(finish=true) or send_gif can be the whole response. "
                        "Use no_response if the user requests silence or sending is unavailable. "
                        "If you cannot complete it, briefly say so; do not promise later work "
                        "unless you have actually scheduled it."
                    ),
                }
            )
        logger.info(
            "inbound mid=%s cid=%s stage=prepared",
            getattr(message, "id", ""),
            channel_id,
        )
        # Mark as in-flight only once we are about to do real LLM work (after
        # expensive pre-work like memory building). This
        # makes the same-user interrupt target actual generations instead of
        # blocking on prep work or causing spurious cancels.
        if current_task:
            self._active_requests[channel_id] = current_task
            self._active_request_user[channel_id] = str(message.author.id)

        # Post a progress message BEFORE the LLM generation starts so the user
        # sees liveness during the (potentially long) generation phase. Without
        # this, the only feedback during generation is the typing indicator, and
        # the tool-progress message only appears AFTER generation finishes —
        # for fast-executing tools like create_site (which just writes a file)
        # the progress message flashes by in under a second and the user never
        # sees it.  This is especially critical for create_site where the model
        # may spend 20+ seconds generating a full HTML document in the tool call
        # arguments, but the tool itself executes in milliseconds.
        #
        # Fire-and-forget via start_defer(): the actual post waits 800ms in
        # the background. If the LLM generation finishes in <800ms with a
        # tool call (create_site, send_message, memory lookup) the deferred
        # post never lands — no flash, no delete, no flicker. If generation
        # runs longer, progress waits for meaningful content or a tool name. The
        # awaitable form (start()) would block the LLM call for 800ms which
        # defeats the point.
        gen_progress = None
        # DMs keep only the final response.
        if message.guild and self._progress_enabled(str(message.guild.id)):
            gen_progress = _make_tool_progress(message)
            with contextlib.suppress(Exception):
                await gen_progress.start_defer()

        # Every progress object created in this turn — the pre-gen progress
        # plus any followup-gen progress for later iterations. The safety
        # net in finally() walks this list and calls stop() on anything
        # still alive, so a stray "thinking: …" or "tool: …" message can
        # never outlive the bot's reply.
        active_progresses: list[Any] = []
        if gen_progress is not None:
            active_progresses.append(gen_progress)

        # Callback fired by the SSE stream reader the moment a tool_call name
        # arrives mid-generation. Updates the progress message from
        # an announced tool status to "tool_name: generating…" so the user sees WHAT
        # the model is building while it's still generating the arguments
        # (e.g. the full HTML body for create_site).
        async def _on_tool_call_name(tool_name: str, reasoning: str = ""):
            logger.debug(
                f"[PROGRESS] mid-stream callback fired: tool_name={tool_name!r} reasoning=[reasoning omitted] gen_progress={gen_progress}"
            )
            if gen_progress is not None:
                with contextlib.suppress(Exception):
                    # 2026-07-21: when the JSON opener is seen mid-
                    # stream, the buffer is full of raw JSON content
                    # from the tick() deltas (e.g. "name create_site,
                    # arguments ..."). Clear it now so the visible
                    # line switches to 'using <tool>…' and the
                    # subsequent run_one() update() with the real
                    # reasoning will land clean. The bot's prompt
                    # already told the model to put its natural-
                    # language reasoning in the tool's 'reasoning'
                    # field; that will arrive via the update() call
                    # in run_one() at line 7534.
                    if hasattr(gen_progress, "_reasoning_buffer"):
                        gen_progress._reasoning_buffer = ""
                    await gen_progress.update(tool_name, reasoning or "generating…")
                    logger.debug(
                        f"[PROGRESS] update() returned, last_content=[content omitted] posted={gen_progress.posted}"
                    )
            else:
                logger.debug(
                    "[PROGRESS] callback fired but gen_progress is None (gen already finalized)"
                )

        # Per-token callback. Fires on EVERY reasoning/content delta so the
        # progress message can show the model's own thoughts streaming by.
        # Critical for long generations: without this, the user stares at
        # a static tool label for the entire 10-30s the model takes to think
        # before the final tool_call delta arrives (which is the only thing
        # the legacy _on_tool_call_name path catches). Tick rate-limits
        # internally to stay under Discord's 5/5s edit limit.
        def _on_token(tok: dict) -> None:
            if gen_progress is None:
                return
            with contextlib.suppress(RuntimeError):
                # Schedule the coroutine on the running loop. tick() itself
                # is async because it may need to await a Discord edit, but
                # the SSE reader must NOT be blocked on a slow edit (it would
                # back-pressure the upstream provider). Fire-and-forget.
                _spawn_background(
                    gen_progress.tick(
                        reasoning_delta=tok.get("reasoning", "")
                        or tok.get("content", ""),
                        tool_name=tok.get("tool_name"),
                    )
                )

        try:
            turn_context["generation_started"] = True
            platform = MaxwellBot._message_tool_platform(self, message)
            openai_tools = self._build_openai_tools(
                platform, message=message, content=content
            )
            # Native OpenAI tools= always wins when native_tool_calls is on.
            # MAXWELL_CUSTOM_TOOL_CALLS is a workaround for providers that
            # cannot stream native tool_calls (historically Ollama minimax-m3);
            # it must not drop the tools= payload on a native-capable endpoint
            # (OpenCode Zen Go / GLM-5.2).
            custom_tool_calls, provider_tools = self._select_tool_protocol(openai_tools)
            logger.info(
                "Tool protocol native=%s custom=%s tool_count=%s",
                bool(provider_tools) and not custom_tool_calls,
                custom_tool_calls,
                len(openai_tools or []),
            )
            if logger.isEnabledFor(logging.DEBUG):
                # LOG_LEVEL=debug shows the result contract the model is
                # actually being handed this turn, so a mislabeled tool is
                # visible in the log instead of only in the model's behavior.
                _groups = contract_groups(
                    [t["function"]["name"] for t in (openai_tools or [])]
                )
                logger.debug(
                    "Tool result contract: returns_output=%s silent=%s ends_turn=%s",
                    _groups["result"],
                    _groups["silent"],
                    _groups["ending"],
                )
            # When the custom protocol is on, instruct the model to emit the
            # tool call as a single-line bare JSON object. The provider parses
            # it from the text stream incrementally, so the bot's progress
            # message can switch to "<tool>: …" as soon as the name appears
            # (early in the stream) rather than at the very end.
            if custom_tool_calls:
                snip = (
                    "Custom tool protocol: one bare JSON object per line, no fences, "
                    "no XML, no native function-call format.\n"
                    '{"name":"<tool>","arguments":{...}}\n'
                    "`reasoning` may be passed as an arguments key (~280 chars, plain text). "
                    "Visible replies still go through send_message / no_response. "
                    "Do not also write the reply as raw assistant text."
                )
                messages = list(messages)
                # Append to the first system message if present, else add one.
                for _m in messages:
                    if _m.get("role") == "system":
                        _m["content"] = (_m["content"] or "") + "\n\n" + snip
                        break
                else:
                    messages.insert(0, {"role": "system", "content": snip})
            await self._acquire_ai_slot(
                timeout=ai_timeout, priority="user", key=channel_id
            )
            try:
                response = await self._generate_response(
                    messages,
                    provider=_current_request_provider.get(),
                    media=active_media,
                    timeout=ai_timeout,
                    max_tokens=max_out_tokens,
                    charge_message=True,
                    tools=provider_tools,
                    on_tool_call_name=_on_tool_call_name,
                    on_token=_on_token,
                    custom_tool_calls=custom_tool_calls,
                )
            finally:
                await self._release_ai_slot()
            native_calls = self._native_calls_from(response)
            # No native tool_calls, but the model may have written the call
            # into the visible text instead. Recover it so it actually runs
            # instead of being posted to the channel as raw markup.
            if not native_calls:
                native_calls, response = self._recover_text_tool_calls(response)
            # If the model returned tool calls, hand the generation progress off
            # to the tool dispatch so the same Discord message transitions from
            # tool status to "tool_name: reasoning" and gets deleted when
            # tools finish. If no tool calls (plain text reply), stop the
            # progress now (fire-and-forget delete) so the caller can post the
            # reply immediately without waiting on a Discord round-trip.
            first_dispatch_progress = gen_progress if native_calls else None
            if gen_progress is not None and not native_calls:
                with contextlib.suppress(Exception):
                    await gen_progress.stop()
                gen_progress = None
            if (not response or not str(response).strip()) and not native_calls:
                logger.warning(f"Empty response from provider for channel {channel_id}")
                if (await MaxwellBot._request_state(self, message)):
                    return
                if self._control.get("error_replies", True):
                    try:
                        await message.channel.send(
                            "couldn't generate a response — try rephrasing or try again."
                        )
                        normal_reply_sent = True
                    except discord.Forbidden as _exc:
                        pass
                return
            response = response or ""
            byok_active = _current_request_provider.get() is not None
            max_iters = max(
                0,
                min(
                    _safe_int(
                        self._control.get(
                            "max_tool_iterations", DEFAULT_CONTROL["max_tool_iterations"]
                        )
                        or 0,
                        0,
                    ),
                    BYOK_TOOL_CALL_LIMIT if byok_active else 100,
                ),
            )
            tool_timeout = float(
                self._control.get("tool_iteration_timeout_seconds", 3600) or 3600
            )
            if byok_active:
                tool_timeout = min(tool_timeout, turn_timeout_seconds(self, _current_request_provider.get()))
            tool_deadline = time.monotonic() + max(1.0, tool_timeout)
            all_tool_results = []
            all_tool_images = []
            all_tool_media = []
            # Accumulate multi-iteration history so intermediate tool results
            # are not discarded on the next follow-up turn.
            conversation_tail: list[dict] = []
            pending_native = native_calls
            # Set when the model produced a SECOND-turn response after a
            # send_message already published a placeholder. We use it
            # below to skip the "send_message is terminal, no plain-text
            # reply" early-return when the LLM reconsidered its first
            # "checking…" placeholder and emitted a real answer on the
            # followup turn. Without this, the placeholder (e.g. "checking…")
            # is the only thing the user ever sees — the substantive 300+
            # char answer is silently dropped. Z3ki observed this in
            # #maxwell-the-bot 2026-08-02 with "Mat Dickie" / "you a fan"
            # — see PM2 out.log 01:25:17→28 for the canonical reproduction.
            followup_turn_ran = False
            tools_expanded = False
            promise_followups = 0
            site_loop_strikes = 0
            tool_results: list[str] = []
            for _iteration in range(max_iters):
                if time.monotonic() > tool_deadline:
                    logger.info("Tool iteration time budget exceeded, breaking")
                    break
                response, tool_results, iter_images = await self._dispatch_tool_calls(
                    message,
                    response,
                    native_tool_calls=pending_native or None,
                    include_images=True,
                    existing_progress=first_dispatch_progress,
                )
                first_dispatch_progress = None
                pending_native = None
                # more_tools sets _tools_expanded on the message. Rebuild the
                # payload once so the follow-up turn actually carries the full
                # catalog instead of the lean set this turn started with.
                if getattr(message, "_tools_expanded", False) and not tools_expanded:
                    tools_expanded = True
                    openai_tools = self._build_openai_tools(
                        platform, message=message, content=content
                    )
                    custom_tool_calls, provider_tools = self._select_tool_protocol(
                        openai_tools
                    )
                    max_out_tokens = min(
                        getattr(self.config, "AI_MAX_OUTPUT_TOKENS", 16384) or 16384,
                        max(256, _safe_int(self._control.get("live_max_output_tokens", 4096), 4096)),
                    )
                    byok_provider = _current_request_provider.get()
                    byok_tokens = (
                        getattr(byok_provider, "max_tokens", None)
                        if byok_provider is not None
                        else None
                    )
                    if isinstance(byok_tokens, int) and byok_tokens > 0:
                        max_out_tokens = max(16, min(byok_tokens, 16384))
                    logger.info(
                        "more_tools: reattached %d tools for follow-up",
                        len(openai_tools or []),
                    )
                native_followup = list(
                    getattr(self, "_last_native_followup_messages", None) or []
                )
                all_tool_results.extend(tool_results)
                all_tool_media.extend(
                    list(getattr(self, "_last_native_tool_media", None) or [])
                )
                all_tool_media = merge_followup_media([], all_tool_media, [])[-12:]
                # Cap image growth across iterations (keep newest frames).
                all_tool_images.extend(iter_images)
                if len(all_tool_images) > 12:
                    all_tool_images = all_tool_images[-12:]
                if not tool_results:
                    break
                if not _tool_results_need_followup(tool_results):
                    break
                if any(SITE_READ_LOOP_MARKER in (r or "") for r in tool_results):
                    site_loop_strikes += 1
                    if site_loop_strikes >= 2:
                        logger.info(
                            "site read-loop breaker; stopping tool iterations"
                        )
                        break
                # An ack-only turn ("on it…") loops back exactly once, so the
                # promised work runs. Without this guard a model that keeps
                # acknowledging would ping-pong until max_iters.
                if _only_promise_results(tool_results):
                    # The subagent is already queued. Another model turn just
                    # sends the same "on it" line again.
                    if any(
                        "Tool delegate_task:" in (r or "") and '"queued"' in (r or "")
                        for r in all_tool_results
                    ):
                        logger.info("ack after a queued subagent; not sending it again")
                        break
                    if promise_followups >= 1:
                        logger.info("ack-only send_message repeated; not looping again")
                        break
                    promise_followups += 1
                # Native path: append assistant tool_calls + role=tool messages.
                # XML path: append freeform assistant text + synthetic user results.
                if native_followup:
                    conversation_tail.extend(native_followup)
                else:
                    conversation_tail.append({"role": "assistant", "content": response})
                    conversation_tail.append(
                        {
                            "role": "user",
                            "content": "=== TOOL RESULTS ===\n"
                            + "\n".join(tool_results)
                            + "\n=== END ===\nUse these results to continue. Tool images are attached for inspection, not automatically sent to chat. Decide whether to send_media, use them elsewhere, regenerate, or reply normally.",
                        }
                    )
                # Preserve full tool rounds and their call/result pairing.
                conversation_tail = trim_tool_tail(conversation_tail)
                result_messages = MaxwellBot._apply_prompt_budget(
                    self, [dict(m) for m in messages] + list(conversation_tail)
                )
                await self._acquire_ai_slot(
                    timeout=ai_timeout, priority="user", key=channel_id
                )
                try:
                    # Keep the original attachments visible throughout tool
                    # use, alongside media returned by this request's tools.
                    followup_media = merge_followup_media(
                        active_media, all_tool_media, all_tool_images
                    )
                    # Post a progress message during the followup LLM generation
                    # too — without this, the user sees the progress message
                    # get deleted (by the previous tool dispatch) and then nothing
                    # while the model generates its next response. This is
                    # especially visible when the followup itself takes a long
                    # time (e.g. generating a send_message with a long reply, or
                    # deciding to call create_site again with new HTML).
                    #
                    # Fire-and-forget via start_defer() — same fast-tool fix
                    # as gen_progress. If the followup completes with a
                    # no-tool reply in <800ms, the deferred post never lands
                    # and the user just sees the final reply. The old code
                    # would post another tool status, delete it via the
                    # _handle_message finally block, then the reply — the
                    # exact flicker the user complained about.
                    followup_progress = None
                    if message.guild and self._progress_enabled(str(message.guild.id)):
                        followup_progress = _make_tool_progress(message)
                        with contextlib.suppress(Exception):
                            await followup_progress.start_defer()
                        active_progresses.append(followup_progress)

                    async def _on_followup_tool_call_name(
                        tool_name: str, reasoning: str = "", _p=followup_progress
                    ):
                        logger.debug(
                            f"[PROGRESS] followup mid-stream callback: tool_name={tool_name!r} reasoning=[reasoning omitted] progress={_p}"
                        )
                        if _p is not None:
                            with contextlib.suppress(Exception):
                                await _p.update(tool_name, reasoning or "generating…")
                                logger.debug(
                                    "[PROGRESS] followup update done, last_content=[content omitted]"
                                )

                    def _on_followup_token(tok: dict, _p=followup_progress) -> None:
                        if _p is None:
                            return
                        with contextlib.suppress(RuntimeError):
                            _spawn_background(
                                _p.tick(
                                    reasoning_delta=tok.get("reasoning", "")
                                    or tok.get("content", ""),
                                    tool_name=tok.get("tool_name"),
                                )
                            )

                    refreshed = self._build_openai_tools(
                        platform, message=message, content=content
                    )
                    old_names = {
                        (tool.get("function") or {}).get("name")
                        for tool in (openai_tools or [])
                    }
                    new_names = {
                        (tool.get("function") or {}).get("name")
                        for tool in (refreshed or [])
                    }
                    if new_names != old_names:
                        openai_tools = refreshed
                        custom_tool_calls, provider_tools = self._select_tool_protocol(
                            openai_tools
                        )
                    try:
                        followup = await self._generate_response(
                            result_messages,
                            provider=_current_request_provider.get(),
                            media=followup_media,
                            timeout=ai_timeout,
                            max_tokens=max_out_tokens,
                            tools=provider_tools,
                            on_tool_call_name=_on_followup_tool_call_name,
                            on_token=_on_followup_token,
                            custom_tool_calls=custom_tool_calls,
                        )
                    except Exception:
                        # Ensure followup progress is cleaned up on error
                        if followup_progress is not None:
                            with contextlib.suppress(Exception):
                                await followup_progress.stop()
                            followup_progress = None
                        raise
                    pending_native = self._native_calls_from(followup)
                    if not pending_native:
                        pending_native, followup = self._recover_text_tool_calls(
                            followup
                        )
                    guarded, stop_after_send = _apply_send_followup_guard(
                        _turn_sent_message(all_tool_results),
                        pending_native,
                        followup,
                    )
                    if stop_after_send:
                        # last must be empty so leftover-on-send skip doesn't
                        # swallow a long follow-up answer.
                        tool_results = []
                        response = guarded
                        followup_turn_ran = True
                        if not guarded:
                            logger.info(
                                "short plaintext after send_message; ending turn"
                            )
                        break
                    # Hand off the followup progress to the next dispatch iteration
                    # so the same message transitions to the tool name/reasoning.
                    # If no tool calls, KEEP the progress alive so the final
                    # ``message.reply(...)`` below can transition it into the
                    # reply (see the fast-tool fix in tool_progress). The old
                    # code called stop() here which deleted the progress and
                    # then a fresh reply posted underneath — the exact flicker
                    # the user reported.
                    if followup_progress is not None and pending_native:
                        first_dispatch_progress = followup_progress
                        # else: leave it alive for the transition below
                    if (followup and str(followup).strip()) or pending_native:
                        response = followup or ""
                        followup_turn_ran = True
                    else:
                        break
                finally:
                    await self._release_ai_slot()
            from discord_expressions import social_result_is_terminal
            if any(social_result_is_terminal(r) for r in all_tool_results):
                normal_reply_sent = True
                return
            # Terminal silence only for explicit no_response.
            if any(
                tr.startswith("Tool no_response:")
                and "__NO_RESPONSE__" in tr
                and not tr.startswith("Tool no_response: Error")
                for tr in all_tool_results
            ):
                await MaxwellBot._record_request_outcome(self, message, "suppressed", "no_response")
                # Feedback for the watch: repeated silence in a room shortens
                # how long that room stays on watch at all.
                with contextlib.suppress(Exception):
                    self._note_watch_silence(message)
                await self._ensure_reasoning_trace(
                    message, all_tool_results, response, "no_response"
                )
                return
            # If this generation already called send_message, leftover
            # assistant text is not a second reply. A later follow-up
            # with real text and no new send_message still posts (the
            # "checking…" placeholder case).
            if _should_skip_plaintext_after_send(
                tool_results, all_tool_results, followup_turn_ran, response
            ):
                await self._ensure_reasoning_trace(
                    message, all_tool_results, response, "send_message"
                )
                # send_message records every successful chunk using its real ID.
                normal_reply_sent = True
                return
            response = _sanitize_visible_reply(
                response,
                scrub_repeats=bool(self._control.get("scrub_repetitions", True)),
            )
            response = await self._keep_work_draft_inside_subagent(
                message, content, response
            )
            # Safety net: if the user asked for a site/page/website and the
            # model replied with raw HTML/JS in chat instead of calling
            # create_site, auto-route the HTML to create_site so the user
            # actually gets a working URL. Without this, a model that
            # ignores the prompt floods the channel with markup fragments
            # and the user never sees a live site.
            if (
                response
                and not all_tool_results
                and "create_site" in self.tools
                and "delegate_task" not in MaxwellBot._turn_tool_names(self, self._message_tool_platform(message), message)
                and "create_site" not in (self._control.get("disabled_tools", []) or [])
                and self._looks_like_site_request(content or "")
                and self._looks_like_html_document(response)
            ):
                try:
                    await MaxwellBot._mark_request_effect(self, message)
                    site_result = await self._auto_route_html_to_site(
                        message, response, content or ""
                    )
                    if site_result:
                        await self._ensure_reasoning_trace(
                            message, all_tool_results, site_result, "auto_site"
                        )
                        sent = await self._send_with_slowmode(
                            message.channel, site_result, reply_to=message
                        )
                        normal_reply_sent = sent is not None
                        # Record the auto-routed site link in memory so
                        # the user can come back and ask "where did you
                        # put my site?" without maxwell drawing a blank.
                        # Same fast-tool fix as the normal reply path.
                        if (
                            self._control.get("store_memory", True)
                            and getattr(self, "memory", None) is not None
                        ):
                            try:
                                await self.add_message_to_memory(
                                    str(message.channel.id),
                                    {
                                        "author": self.bot_name,
                                        "author_id": str(self.user.id)
                                        if self.user
                                        else "",
                                        "author_is_bot": True,
                                        "content": site_result,
                                        "message_id": f"bot_auto_site:{message.id}",
                                        "timestamp": datetime.now(
                                            timezone.utc
                                        ).isoformat(),
                                    },
                                    message,
                                )
                            except Exception as _e:  # noqa: BLE001
                                logger.debug(
                                    f"Failed to record auto-site in memory: {_e}"
                                )
                        return
                except Exception as e:
                    logger.error(f"Auto-route to create_site failed: {type(e).__name__}")
            if response:
                await self._ensure_reasoning_trace(
                    message, all_tool_results, response, "reply"
                )
                response, reply_delivered = await self._deliver_normal_reply(
                    message, response, active_progresses
                )
                # Write the bot's own reply to channel memory. Without
                # this the next turn sees the user's "Explain X" question
                # but NOT the bot's answer — the user comes back and
                # asks "what did you say?" and the model genuinely has
                # no record. The user reported this as "I asked for an
                # explanation and maxwell couldn't recall its own
                # explanation, and even when I pasted it back maxwell
                # couldn't remember". The fix is to add_to_channel_memory
                # for every normal reply path. The send_message tool
                # path already records via _remember_tool_call (writes
                # a Tool entry); this covers the message.reply(...)
                # path. The synthetic message_id is derived from the
                # user's message_id so it's stable across retries and
                # doesn't collide with the user's own message_id.
                if (
                    reply_delivered
                    and response
                    and self._control.get("store_memory", True)
                    and getattr(self, "memory", None) is not None
                ):
                    try:
                        await self.add_message_to_memory(
                            str(message.channel.id),
                            {
                                "author": self.bot_name,
                                "author_id": str(self.user.id) if self.user else "",
                                "author_is_bot": True,
                                "content": response,
                                "message_id": f"bot_reply:{message.id}",
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                            },
                            message,
                        )
                    except Exception as _e:  # noqa: BLE001
                        logger.debug(
                            f"Failed to record bot reply in channel memory: {_e}"
                        )
                if reply_delivered:
                    normal_reply_sent = True
                    await self._mark_inbox_announced()
        except asyncio.CancelledError as _exc:
            logger.info(f"Cancelled active request in channel {channel_id}")
            raise
        except MessageQuotaExceeded as e:
            if (await MaxwellBot._request_state(self, message)):
                raise
            await message.channel.send(str(e))
            normal_reply_sent = True
        except ProviderUsageExhaustedError as e:
            if (await MaxwellBot._request_state(self, message)):
                raise
            logger.warning(f"Provider usage exhausted while handling message: {type(e).__name__}")
            if self._control.get("error_replies", True):
                try:
                    await message.channel.send(e.user_message)
                    normal_reply_sent = True
                except discord.Forbidden as _exc:
                    pass
        except ProviderEmptyResponseError as e:
            if (await MaxwellBot._request_state(self, message)):
                return
            logger.warning("Provider returned no usable response: %s", e)
            if self._control.get("error_replies", True):
                try:
                    await message.channel.send(e.user_message)
                    normal_reply_sent = True
                except discord.Forbidden as _exc:
                    pass
        except Exception as e:
            if (await MaxwellBot._request_state(self, message)):
                raise
            is_timeout = isinstance(e, asyncio.TimeoutError) or (
                isinstance(e, RuntimeError) and "timed out" in str(e).lower()
            )
            logger.error(f"Error handling message: {type(e).__name__}\n[traceback detail omitted]")
            if self._control.get("error_replies", True):
                try:
                    if is_timeout:
                        await message.channel.send(
                            "timed out waiting for a response. try again or break the task into smaller pieces."
                        )
                    elif self._control.get("error_details", True):
                        await message.channel.send(
                            f"something broke — {_format_user_error(e)}"
                        )
                    else:
                        await message.channel.send("Sorry, please try again.")
                    normal_reply_sent = True
                except discord.Forbidden as _exc:
                    pass
        finally:
            await self._release_turn_context(turn_context)
            # Safety net: walk every progress object this turn ever created
            # and stop() anything still alive, so we never leave an orphan
            # "thinking: …" or "<tool>: …" message
            # after the bot's reply has gone out. Covers LLM errors,
            # empty responses, no-tool-call branches, followup leaks, etc.
            for _prog in active_progresses:
                if _prog is None:
                    continue
                with contextlib.suppress(Exception):
                    await _prog.stop()
            active_progresses.clear()
            # Drop this channel's entry from the per-channel progress
            # dict so it doesn't accumulate over the bot's lifetime
            # under load. The next message in this channel will
            # re-stash. run_one() already does set/restore around
            # each tool call, so this is belt-and-suspenders for the
            # case where an exception escaped run_one before the
            # finally restored the prior value.
            self._current_progress_by_channel.pop(channel_id, None)
            gen_progress = None
            # Channel is no longer in-flight; the _last_bot_reply record
            # below keeps autonomy from re-engaging its own fresh reply.
            MaxwellBot._clear_turn_flags(self, channel_id, current_task)
            self._tick_media_context(channel_id)
            if normal_reply_sent:
                self._last_bot_reply[channel_id] = time.time()
                if author is not None and not getattr(author, "bot", False):
                    self._arm_conversation_watch(channel_id)
            # Keep the reply map bounded.
            if len(self._last_bot_reply) > 64:
                cutoff = time.time() - 3600
                self._last_bot_reply = {
                    c: t for c, t in self._last_bot_reply.items() if t > cutoff
                }
