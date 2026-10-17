"""Message-edit handling, raw update decoding and inflight refresh.

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

class MessageUpdatesMixin:

    @staticmethod
    def _raw_update_data(payload) -> dict:
        data = getattr(payload, "data", None)
        if isinstance(data, dict):
            return data
        if isinstance(payload, dict):
            return payload
        return {}

    @staticmethod
    def _prefer_sendable_channel(*candidates):
        """First channel that can send, otherwise the first channel we have.

        get_channel() returns None for a DM that is only on the live message.
        That miss must not replace a channel that can already send.
        """
        fallback = None
        for candidate in candidates:
            if candidate is None:
                continue
            if fallback is None:
                fallback = candidate
            if callable(getattr(candidate, "send", None)):
                return candidate
        return fallback

    @staticmethod
    def _raw_update_namespace(value):
        if isinstance(value, dict):
            return SimpleNamespace(
                **{
                    str(key): MaxwellBot._raw_update_namespace(val)
                    for key, val in value.items()
                }
            )
        if isinstance(value, list):
            return [MaxwellBot._raw_update_namespace(item) for item in value]
        return value

    @staticmethod
    def _coerce_raw_author(author, previous=None):
        """Discord omits ``bot`` on human authors. Raw updates must still have it."""
        prev_author = (
            getattr(previous, "author", None) if previous is not None else None
        )
        if author is None or not getattr(author, "id", None):
            author = prev_author
        if author is None or not getattr(author, "id", None):
            return SimpleNamespace(id="", display_name="unknown", bot=False)
        bot_flag = bool(getattr(author, "bot", getattr(prev_author, "bot", False)))
        display = (
            getattr(author, "display_name", None)
            or getattr(author, "global_name", None)
            or getattr(author, "username", None)
            or getattr(author, "name", None)
            or getattr(prev_author, "display_name", None)
            or "unknown"
        )
        with contextlib.suppress(Exception):
            author.bot = bot_flag
            if not getattr(author, "display_name", None):
                author.display_name = display
        if not hasattr(author, "bot"):
            author = SimpleNamespace(
                id=getattr(author, "id", ""),
                display_name=display,
                bot=bot_flag,
                name=getattr(author, "name", None) or getattr(author, "username", None),
            )
        return author

    async def _message_from_raw_update(self, payload):
        """Resolve a RawMessageUpdateEvent even when Discord evicted its cache."""
        data = self._raw_update_data(payload)
        cached = getattr(payload, "cached_message", None)
        if cached is not None and not data:
            return cached
        message_id = str(getattr(payload, "message_id", None) or data.get("id") or "")
        channel_id = str(
            getattr(payload, "channel_id", None) or data.get("channel_id") or ""
        )
        # A raw update is usually a partial object. Merge it with the last
        # known message before resolving a channel: Discord's payload may
        # contain only ``embeds``, and treating omitted fields as empty would
        # erase the transcript. The in-flight turn still holds the live
        # message when this unfurl arrives before the snapshot map is written.
        snapshots = getattr(self, "_message_snapshots", None) or {}
        previous = cached if cached is not None else snapshots.get(message_id)
        if previous is None:
            for state in self._inflight_for_message(message_id):
                candidate = state.get("message")
                if candidate is not None:
                    previous = candidate
                    break
        resolved = None
        getter = getattr(self, "get_channel", None)
        if channel_id and callable(getter):
            with contextlib.suppress(Exception):
                lookup = int(channel_id) if str(channel_id).isdigit() else channel_id
                resolved = getter(lookup)
            if resolved is None and str(channel_id).isdigit():
                with contextlib.suppress(Exception):
                    resolved = getter(channel_id)
        private_match = None
        if channel_id:
            private_channels = []
            with contextlib.suppress(Exception):
                private_channels = list(getattr(self, "private_channels", None) or [])
            private_match = next(
                (
                    item
                    for item in private_channels
                    if str(getattr(item, "id", "")) == channel_id
                ),
                None,
            )
        channel = self._prefer_sendable_channel(
            resolved,
            private_match,
            getattr(cached, "channel", None),
            getattr(previous, "channel", None) if previous is not None else None,
        )
        fetch = getattr(channel, "fetch_message", None)
        if cached is None and callable(fetch) and message_id:
            with contextlib.suppress(Exception):
                fetched = await asyncio.wait_for(fetch(int(message_id)), timeout=4.0)
                if fetched is not None:
                    return fetched

        def _field(name, default=None):
            if name in data:
                return self._raw_update_namespace(data.get(name))
            return getattr(previous, name, default) if previous is not None else default

        author_data = data.get("author") if "author" in data else None
        author = (
            self._raw_update_namespace(author_data)
            if author_data
            else getattr(previous, "author", None)
        )
        author = self._coerce_raw_author(author, previous)
        guild = getattr(channel, "guild", None)
        if channel is None and previous is not None:
            channel = getattr(previous, "channel", None)
        guild = getattr(channel, "guild", None) or getattr(previous, "guild", None)
        embeds = _field("embeds", []) or []
        attachments = _field("attachments", []) or []
        if "sticker_items" in data:
            stickers = self._raw_update_namespace(data.get("sticker_items") or [])
        elif "stickers" in data:
            stickers = self._raw_update_namespace(data.get("stickers") or [])
        else:
            stickers = list(getattr(previous, "stickers", None) or [])
        snapshot = SimpleNamespace(
            id=message_id,
            channel=channel
            or SimpleNamespace(id=channel_id, guild=guild, name="unknown"),
            guild=guild,
            author=author,
            content=str(_field("content", "") or ""),
            embeds=embeds,
            attachments=attachments,
            stickers=stickers,
            mentions=_field("mentions", []) or [],
            role_mentions=_field("role_mentions", []) or [],
            mention_everyone=bool(_field("mention_everyone", False)),
            reference=_field("reference"),
            components=_field("components", []) or [],
            poll=_field("poll"),
            message_snapshots=_field("message_snapshots") or _field("snapshots") or [],
        )
        donors = [cached, previous]
        donors.extend(
            state.get("message") for state in self._inflight_for_message(message_id)
        )
        return preserve_message_delivery(snapshot, *donors)

    @classmethod
    def _message_update_fingerprint(cls, message) -> str:
        """Stable full-state key for cached/raw MESSAGE_UPDATE deduplication."""
        embeds = [
            {
                "text": cls._embed_text(embed),
                "url": str(getattr(embed, "url", "") or ""),
                "media": cls._embed_media_urls(embed),
                "type": str(getattr(embed, "type", "") or ""),
            }
            for embed in cls._payload_attr_list(message, "embeds", 8)
        ]
        attachments = [
            {
                "id": str(getattr(attachment, "id", "") or ""),
                "filename": str(getattr(attachment, "filename", "") or ""),
                "url": str(getattr(attachment, "url", "") or ""),
                "content_type": str(getattr(attachment, "content_type", "") or ""),
                "size": str(getattr(attachment, "size", "") or ""),
            }
            for attachment in cls._payload_attr_list(message, "attachments", 8)
        ]
        stickers = [
            {
                "id": str(getattr(sticker, "id", "") or ""),
                "name": str(getattr(sticker, "name", "") or ""),
                "url": str(getattr(sticker, "url", "") or ""),
            }
            for sticker in cls._payload_attr_list(message, "stickers", 6)
        ]
        payload = {
            "content": message_combined_content(message)
            or str(getattr(message, "content", "") or ""),
            "embeds": embeds,
            "attachments": attachments,
            "stickers": stickers,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()

    @classmethod
    def _message_media_fingerprint(cls, message) -> str:
        """Key only data that can require a binary media download."""
        media_urls = []
        for embed in cls._payload_attr_list(message, "embeds", 8):
            media_urls.extend(cls._embed_media_urls(embed))
        attachments = [
            (
                str(getattr(item, "filename", "") or ""),
                str(getattr(item, "url", "") or ""),
                str(getattr(item, "content_type", "") or ""),
                str(getattr(item, "size", "") or ""),
            )
            for item in cls._payload_attr_list(message, "attachments", 8)
        ]
        stickers = [
            (
                str(getattr(item, "name", "") or ""),
                str(getattr(item, "url", "") or ""),
            )
            for item in cls._payload_attr_list(message, "stickers", 6)
        ]
        payload = {
            "embeds": media_urls,
            "attachments": attachments,
            "stickers": stickers,
            "links": cls._media_link_refs(message_combined_content(message)),
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()

    def _solo_channel_for(self, guild) -> str:
        """The one channel Maxwell may speak in for this server, or ''.

        Per-server by design: allowed_channels is a global whitelist, so using
        it to quiet one server would silence him in every other server too.
        """
        gid = str(getattr(guild, "id", "") or "")
        if not gid:
            return ""
        mapping = self._control.get("guild_solo_channel") or {}
        if not isinstance(mapping, dict):
            return ""
        return str(mapping.get(gid) or "")

    def _solo_blocks(self, message) -> bool:
        """True when this message is in a soloed server but the wrong channel."""
        solo = self._solo_channel_for(getattr(message, "guild", None))
        if not solo:
            return False
        return str(getattr(getattr(message, "channel", None), "id", "")) != solo

    def _message_update_allowed(self, message) -> bool:
        author = getattr(message, "author", None)
        author_id = str(getattr(author, "id", "") or "")
        if not author_id:
            return False
        admin_check = getattr(self, "_is_admin", None)
        is_admin = bool(admin_check(author_id)) if callable(admin_check) else False
        ignored_users = {
            str(value) for value in (self._control.get("ignore_users", []) or [])
        }
        blacklist = {
            str(value) for value in (getattr(self, "_blacklist", set()) or set())
        }
        if (
            author_id
            and (author_id in blacklist or author_id in ignored_users)
            and not is_admin
        ):
            return False
        channel_id = str(getattr(getattr(message, "channel", None), "id", "") or "")
        if not channel_id:
            return False
        blocked_channels = {
            str(value) for value in (self._control.get("blocked_channels", []) or [])
        }
        if channel_id in blocked_channels:
            return False
        if self._solo_blocks(message):
            return False
        allowed = {
            str(value) for value in (self._control.get("allowed_channels", []) or [])
        }
        return not allowed or channel_id in allowed

    def _replace_media_context_for_message(
        self, channel_id: str, message_id, media: list[dict]
    ) -> None:
        """Replace, rather than append, visuals belonging to an edited message."""
        if getattr(self, "_media_context", None) is None:
            self._media_context = {}
        mid = str(message_id or "")
        cached = list(self._media_context.get(channel_id, []) or [])
        if mid:
            cached = [item for item in cached if str(item.get("message_id", "")) != mid]
        if cached:
            self._media_context[channel_id] = cached
        else:
            self._media_context.pop(channel_id, None)
        if media:
            cache = getattr(self, "_cache_media_context", None)
            if callable(cache):
                cache(channel_id, media)

    async def _extract_context_media(self, message) -> list[dict]:
        """Extract current media for an edit without ever producing a reply."""
        media: list[dict] = []
        _images, current = await self._extract_media(message)
        media.extend(current)
        media.extend(await self._extract_embeds(message))
        media.extend(
            await self._extract_linked_media(
                message,
                skip_urls={str(item.get("url")) for item in media if item.get("url")},
            )
        )
        return media

    def _inflight_for_message(self, message_id: str) -> list[dict]:
        return [
            state
            for state in list((getattr(self, "_inflight_context", None) or {}).values())
            if message_id in set(state.get("message_ids") or set())
        ]

    def _begin_inflight_context(self, message, content: str) -> dict[str, Any]:
        """Register a turn so a late MESSAGE_UPDATE can refresh its context."""
        mid = str(getattr(message, "id", "") or "")
        state: dict[str, Any] = {
            "message": message,
            "content": str(content or ""),
            "root_message_id": mid,
            "message_ids": {mid} if mid else set(),
            "media": [],
            "latest_message": None,
            "latest_content": None,
            "latest_media": None,
            "version": 0,
            "seen_version": 0,
            "update_event": asyncio.Event(),
            "generation_started": False,
        }
        if mid:
            self._inflight_context[mid] = state
        return state

    def _end_inflight_context(self, state: dict[str, Any] | None) -> None:
        if not state:
            return
        store = getattr(self, "_inflight_context", None) or {}
        for key, value in list(store.items()):
            if value is state:
                store.pop(key, None)

    async def _release_turn_context(self, state: dict[str, Any] | None) -> None:
        """End one turn's typing and inflight-edit registration.

        Typing exits first and is idempotent, so an inner ``finally`` and
        the outer dispatcher can both call this for the same turn.
        """
        if not state:
            return
        await self._exit_live_typing(state.get("live_typing"))
        self._end_inflight_context(state)

    def _clear_turn_flags(self, channel_id: str, current_task) -> None:
        """Drop a turn's per-channel busy markers unless a newer turn owns them."""
        if self._active_requests.get(channel_id) is current_task:
            self._active_requests.pop(channel_id, None)
            self._active_request_user.pop(channel_id, None)
        self._replying_channels.discard(channel_id)

    async def _wait_for_late_embeds(self, message, content: str):
        """Fetch one fresh Discord snapshot before a media turn starts."""
        if getattr(message, "embeds", None):
            return message
        if not self._media_link_refs(content):
            return message
        channel = getattr(message, "channel", None)
        fetch = getattr(channel, "fetch_message", None)
        message_id = getattr(message, "id", None)
        if not callable(fetch) or message_id is None:
            # Cached Message objects can be updated in place by discord.py while
            # this coroutine yields, so the caller still re-reads its fields.
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.sleep(min(0.25, LATE_EMBED_WAIT_SECONDS))
            return message
        try:
            await asyncio.sleep(min(0.25, LATE_EMBED_WAIT_SECONDS))
            refreshed = await asyncio.wait_for(
                fetch(message_id), timeout=max(0.1, LATE_EMBED_WAIT_SECONDS - 0.25)
            )
            return refreshed or message
        except asyncio.CancelledError:
            raise
        except Exception:
            return message

    async def _apply_inflight_refresh(
        self,
        state: dict[str, Any],
        message,
        content: str,
        media: list[dict],
        active_media: list[dict],
        media_summary: str,
        messages: list[dict],
    ):
        """Use a completed late edit before the provider request is sent."""
        event = state.get("update_event")
        if event is not None and state.get("version", 0) <= state.get(
            "seen_version", 0
        ):
            # Do not add latency to ordinary text turns. Media/link turns
            # already get _wait_for_late_embeds above.
            return message, content, media, active_media, media_summary, messages
        latest = state.get("latest_message")
        latest_media = state.get("latest_media")
        if latest is None or latest_media is None:
            return message, content, media, active_media, media_summary, messages
        refresh_version = state.get("version", 0)
        # The late edit is often a SimpleNamespace of the unfurled embed.
        # Keep the live message's reply() so send_media and the final answer
        # still post into the same DM or channel.
        latest = preserve_message_delivery(latest, message, state.get("message"))
        message = latest
        content = str(getattr(latest, "content", "") or "")
        media = list(latest_media)
        current_images = [item for item in media if item.get("is_image")]
        channel_id = str(getattr(getattr(latest, "channel", None), "id", "") or "")
        cached_media = []
        reply_media_id = self._reply_media_message_id(
            latest, getattr(self, "_message_snapshots", None)
        )
        if reply_media_id is not None:
            cached_media = self._get_media_context(
                channel_id, message_id=reply_media_id
            )
        elif self._should_use_cached_media_context(latest, content) and (
            not current_images or self._should_mix_cached_with_current(content)
        ):
            cached_media = self._get_media_context(channel_id)
        active_media = current_images + cached_media + self._current_binary_media(media)
        media_summary = self._format_media_summary(media, active_media)
        state["message"] = message
        state["content"] = content
        state["media"] = list(media)
        state["active_media"] = list(active_media)
        state["seen_version"] = state.get("version", 0)
        messages = await self._build_messages(
            latest,
            content,
            has_media=bool(active_media),
            media_summary=media_summary,
        )
        if state.get("version", 0) != refresh_version:
            # Another edit landed while RAG/history was rebuilding. Re-read
            # the newest state instead of marking that update as consumed
            # while returning a stale prompt.
            return await self._apply_inflight_refresh(
                state,
                message,
                content,
                media,
                active_media,
                media_summary,
                messages,
            )
        logger.info(
            "Applied late message update to in-flight turn message=%s",
            getattr(latest, "id", "?"),
        )
        return message, content, media, active_media, media_summary, messages

    async def _refresh_edited_message(self, message, *, before=None) -> bool:
        """Refresh memory/media for MESSAGE_UPDATE, never dispatch a reply."""
        if message is None:
            return False
        snapshots = getattr(self, "_message_snapshots", None)
        if snapshots is None:
            snapshots = {}
            self._message_snapshots = snapshots
        update_state = getattr(self, "_message_update_state", None)
        if update_state is None:
            update_state = {}
            self._message_update_state = update_state
        mid = str(getattr(message, "id", "") or "")
        if not mid or not self._message_update_allowed(message):
            return False
        full_fp = self._message_update_fingerprint(message)
        media_fp = self._message_media_fingerprint(message)
        now = time.monotonic()
        previous = update_state.get(mid)
        if previous and previous[0] == full_fp:
            snapshots[mid] = message
            return False
        snapshots[mid] = message
        update_state[mid] = (full_fp, media_fp, now)
        if len(update_state) > 1000:
            cutoff = now - 3600
            update_state = {
                key: value for key, value in update_state.items() if value[2] > cutoff
            }
            self._message_update_state = update_state
            self._message_snapshots = {
                key: value for key, value in snapshots.items() if key in update_state
            }
            snapshots = self._message_snapshots

        channel_id = str(getattr(getattr(message, "channel", None), "id", "") or "")
        author = getattr(message, "author", None)
        update_users = getattr(self, "_update_recent_users", None)
        if author is not None:
            if callable(update_users):
                with contextlib.suppress(Exception):
                    update_users(channel_id, author)
        for user in list(getattr(message, "mentions", None) or []):
            if callable(update_users):
                with contextlib.suppress(Exception):
                    update_users(channel_id, user)
        if self._control.get("store_memory", True) and getattr(self, "memory", None):
            with contextlib.suppress(Exception):
                await self.add_message_to_memory(
                    channel_id,
                    self._message_memory_item(message, edited=True),
                    message,
                )

        media: list[dict] = []
        media_changed = previous is None or previous[1] != media_fp
        media_refresh_ok = True
        if media_changed:
            try:
                media = await self._extract_context_media(message)
            except Exception:
                media_refresh_ok = False
                logger.warning(
                    "Edited-message media refresh failed for %s", mid, exc_info=True
                )
            latest_state = update_state.get(mid)
            if latest_state and latest_state[0] != full_fp:
                # A newer MESSAGE_UPDATE won while this one was downloading.
                # Do not let the slower, stale extraction overwrite its cache
                # or notify an in-flight turn.
                return True
            # Even when processing is now disabled, remove stale visuals for
            # this message; do not leave an old embed attached to later turns.
            old_cached = any(
                str(item.get("message_id", "")) == mid
                for item in (
                    getattr(self, "_media_context", {}).get(channel_id, []) or []
                )
            )
            if media_refresh_ok and (
                MaxwellBot._image_input_enabled(self) or old_cached
            ):
                self._replace_media_context_for_message(channel_id, mid, media)
        latest_state = update_state.get(mid)
        if latest_state and latest_state[0] != full_fp:
            # A newer update may have superseded this handler even when no
            # binary media needed downloading.
            return True

        # A watch debounce may already have queued this message without
        # acquiring the channel lock yet. Replace that pending target so the
        # eventual single reply uses the edited text/object instead of the
        # stale MESSAGE_CREATE snapshot.
        for bucket in (getattr(self, "_watch_debounce", None) or {}).values():
            for key in ("latest", "latest_directed"):
                queued = bucket.get(key)
                if str(getattr(queued, "id", "") or "") == mid:
                    message = preserve_message_delivery(message, queued)
                    bucket[key] = message
                    if key == "latest_directed":
                        bucket["content"] = str(getattr(message, "content", "") or "")

        for state in self._inflight_for_message(mid):
            state_media = list(state.get("media") or [])
            is_root = mid == str(state.get("root_message_id") or "")
            if is_root:
                if media_changed and media_refresh_ok:
                    state["media"] = list(media)
                message = preserve_message_delivery(message, state.get("message"))
                state["message"] = message
                state["content"] = str(getattr(message, "content", "") or "")
                state["latest_message"] = message
                state["latest_content"] = state["content"]
                state["latest_media"] = list(state.get("media") or state_media)
            else:
                # A reply's parent is part of the in-flight context, but it
                # must not replace the message Maxwell is answering. Refresh
                # the parent's media in the turn payload and let
                # _reply_parent() resolve its newest snapshot when rebuilding
                # the prompt.
                if media_changed and media_refresh_ok:
                    state_media = [
                        item
                        for item in state_media
                        if str(item.get("message_id", "")) != mid
                    ]
                    state_media.extend(media)
                    state["media"] = state_media
                state["latest_message"] = state.get("message")
                state["latest_content"] = state.get("content", "")
                state["latest_media"] = list(state.get("media") or state_media)
            state["version"] = int(state.get("version", 0)) + 1
            event = state.get("update_event")
            if event is not None:
                event.set()
        return True

    async def on_message_edit(self, before, after):
        """Refresh context; only newly added direct mentions can start a turn."""
        if not MaxwellBot._dev_scope_allows(self, after):
            return
        self._dispatch_plugin_event("on_message_edit", before, after)
        try:
            loader = getattr(self, "_load_control", None)
            if callable(loader) and getattr(self, "config", None) is not None:
                with contextlib.suppress(Exception):
                    loader()
            await self._refresh_edited_message(after, before=before)
            await self._maybe_reply_to_edited_mention(before, after)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Failed to process Discord message edit", exc_info=True)

    async def on_raw_message_edit(self, payload):
        """Handle embeds for messages not present in discord.py's cache."""
        if not MaxwellBot._dev_scope_allows(self, payload):
            return
        try:
            loader = getattr(self, "_load_control", None)
            if callable(loader) and getattr(self, "config", None) is not None:
                with contextlib.suppress(Exception):
                    loader()
            message = await self._message_from_raw_update(payload)
            if message is not None:
                before = getattr(payload, "cached_message", None) or (
                    getattr(self, "_message_snapshots", None) or {}
                ).get(str(getattr(message, "id", "")))
                await self._refresh_edited_message(message)
                if "content" in (getattr(payload, "data", None) or {}):
                    await self._maybe_reply_to_edited_mention(before, message)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Failed to process raw Discord message edit", exc_info=True)

    async def _maybe_reply_to_edited_mention(self, before, after):
        if not self._control.get("respond_to_edited_mentions", True):
            return
        journal = getattr(self, "_request_journal", None)
        if journal is None or self.user is None:
            return
        if self.user not in (getattr(after, "mentions", None) or []):
            return
        if before is not None and (
            self.user in (getattr(before, "mentions", None) or [])
            or getattr(before, "content", "") == getattr(after, "content", "")
        ):
            return
        row = (await self._request_state(after))
        if row and (
            row["status"] != "suppressed"
            or row["directed"]
            or row["effects_started"]
            or row["reason"] not in {"not_addressed", "cooldown", "empty_payload"}
        ):
            return
        if row:
            await journal.submit(journal.update, after.id, "received", directed=True, reason="edited_mention")
        await self.on_message(after)
