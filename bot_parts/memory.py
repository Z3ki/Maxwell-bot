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

class MemoryMixin:

    async def add_message_to_memory(
        self, channel_id: str, message_dict: dict, message=None
    ) -> None:
        """Bot-side convenience that wraps memory.add_to_channel_memory
        and forwards `guild_id` + `message_type` from a discord.Message.

        Use this instead of calling self.memory.add_to_channel_memory
        directly in any code path that has the originating discord.Message
        in scope. Old direct call sites are being phased out.

        The audit on 2026-07-30 found 13 direct call sites, only 2 of
        which carried guild_id. This wrapper is the single point of
        truth now.
        """
        enriched = dict(message_dict)
        enriched.update(self._mem_kwargs(message))
        if not self._message_content_may_be_stored(message, enriched):
            return
        await self.memory.add_to_channel_memory(channel_id, enriched)
        # Private /maxwell histories use a synthetic per-user channel key and
        # must not update the shared cross-context entity directory.
        if str(getattr(message, "response_visibility", "public") or "public") != "private":
            await self._observe_message_author(message, enriched)

    async def _observe_message_author(self, message, enriched: dict) -> None:
        """Keep the global per-user entity row current.

        Discord ids are already global — the same person in two servers and a
        DM is one user_id — but nothing used that, so the bot could learn your
        name in one server and meet you as a stranger in the next. This is the
        cheap half of the fix: one upsert per stored message recording who was
        seen, under what name, and where. Historical scoped facts remain
        separate; there is no automatic fact extractor.

        Runs on the message path, so it must never raise and never block: an
        identity row is a nicety, a dropped message is not.
        """
        if message is None or enriched.get("is_tool"):
            return
        if not (self._control.get("entity_memory_enabled", True)):
            return
        observe = getattr(self.memory, "observe_user", None)
        if not callable(observe):
            return
        try:
            author = getattr(message, "author", None)
            if author is None or getattr(author, "bot", False):
                return
            uid = str(getattr(author, "id", "") or "")
            if not uid:
                return
            guild = getattr(message, "guild", None)
            await observe(
                uid,
                display_name=str(getattr(author, "display_name", "") or ""),
                guild_id=str(getattr(guild, "id", "") or ""),
                is_dm=guild is None,
            )
        except Exception as e:
            logger.debug(f"entity observe skipped: {e}")

    def _message_content_may_be_stored(self, message, row: dict) -> bool:
        """False when this write would store an opted-out person's content.

        The live request is still answered. This only gates persistent
        history: their own rows, tool rows recorded on their message, and
        Maxwell posts that reply to them.
        """
        from plugins.maxwell_extras.user_preferences import tracks_message_content

        store = getattr(self, "_user_preferences", None)
        subjects: list[str] = []
        author = getattr(message, "author", None)
        author_is_bot = bool(getattr(author, "bot", False))
        if author is not None and not author_is_bot:
            subjects.append(str(getattr(author, "id", "") or ""))
        if not row.get("author_is_bot"):
            subjects.append(str(row.get("author_id") or ""))
        subjects.append(str(row.get("requested_by") or ""))
        if author_is_bot or row.get("author_is_bot"):
            subjects.append(str(row.get("reply_to_author_id") or ""))
            parent_fn = getattr(self, "_reply_parent", None)
            parent = parent_fn(message) if callable(parent_fn) and message is not None else None
            parent_author = getattr(parent, "author", None)
            if parent_author is not None and not getattr(parent_author, "bot", False):
                subjects.append(str(getattr(parent_author, "id", "") or ""))
        seen: list[str] = []
        for uid in subjects:
            if uid.isdigit() and uid not in seen:
                seen.append(uid)
        return all(tracks_message_content(store, uid) for uid in seen)

    def _mem_kwargs(self, message) -> dict:
        """Build the standard kwargs for add_to_channel_memory from a
        discord.Message. Centralizes guild_id + message_type so every
        caller site that records a message stops forgetting them.

        Audit on 2026-07-30: 13 call sites, only 2 carried guild_id.
        """
        if message is None:
            return {}
        guild = getattr(message, "guild", None)
        channel = getattr(message, "channel", None)
        return {
            "guild_id": str(getattr(guild, "id", "") or ""),
            "guild_name": str(getattr(guild, "name", "") or ""),
            "channel_name": str(getattr(channel, "name", "") or ""),
            "message_type": str(
                getattr(getattr(message, "type", None), "name", "default") or "default"
            ),
        }

    def _load_emojis(self):
        from discord_expressions import sync_guild
        self._guild_emojis = {}
        self._guild_stickers = {}
        for guild in self.guilds:
            if MaxwellBot._dev_scope_allows(self, guild, guild_id=guild.id):
                sync_guild(self, guild)

    def _extract_stickers_from_text(self, text: str, guild) -> tuple[str, list]:
        from discord_expressions import extract_stickers
        return extract_stickers(self, text, guild)

    def _render_custom_emojis(self, text: str, guild) -> str:
        from discord_expressions import available_emojis, available_emoji_codes
        catalog = available_emojis(self, guild)
        codes = available_emoji_codes(self, guild)
        # Only current-server or application-owned markup is allowed.
        text = re.sub(
            r"<a?:([A-Za-z0-9_]{2,32}):(\d+)>",
            lambda m: codes.get(m.group(2)) or f":{m.group(1)}:",
            text,
        )
        return render_custom_emoji_aliases(text, catalog, emoji_codes=codes)

    def _message_memory_content(self, message) -> str:
        """Render the current Discord payload for transcript storage.

        Discord edits can add embeds, attachments, or stickers without
        changing ``content``. Keep the same structured annotations used by the
        normal MESSAGE_CREATE path so an edited row replaces the old text
        instead of leaving the model with contradictory snapshots.
        """
        memory_content = str(getattr(message, "content", "") or "")
        att_note = _compact_attachments_note(message)
        if att_note:
            memory_content = f"{memory_content} {att_note}".strip()
        embed_note = _compact_embeds_note(message)
        if embed_note:
            memory_content = f"{memory_content} {embed_note}".strip()
        if not memory_content and message_has_visible_payload(message):
            memory_content = "[media attached]"
        from maxwell_core.prompts.history import compact_media_annotations
        return compact_media_annotations(
            render_discord_context_text(
                message,
                memory_content,
                known_users=(getattr(self, "_recent_users", None) or {}).get(
                    str(getattr(getattr(message, "channel", None), "id", "") or ""),
                    {},
                ),
                include_timestamp=False,
            )
        )

    def _message_memory_item(self, message, *, edited: bool = False) -> dict:
        """Build one idempotent transcript row from the latest message state."""
        author = getattr(message, "author", None)
        item = {
            "author": getattr(author, "display_name", "System")
            if author is not None
            else "System",
            "author_id": str(getattr(author, "id", "system"))
            if author is not None
            else "system",
            "author_is_bot": bool(getattr(author, "bot", False))
            if author is not None
            else False,
            "content": self._message_memory_content(message),
            "message_id": str(getattr(message, "id", "") or ""),
            "timestamp": _message_created_at_iso(message),
            "prompt_format_version": 2,
        }
        mentions = list(getattr(message, "mentions", None) or [])
        if mentions:
            item["mentions"] = [
                {
                    "id": str(getattr(user, "id", "") or ""),
                    "name": getattr(
                        user, "display_name", str(getattr(user, "id", "") or "")
                    ),
                }
                for user in mentions[:10]
            ]
        reply_builder = getattr(self, "_reply_meta_from_message", None)
        reply_meta = reply_builder(message) if callable(reply_builder) else {}
        if reply_meta:
            item.update(reply_meta)
        if edited:
            item["edited_at"] = datetime.now(timezone.utc).isoformat()
        return item

    async def _memory_cleanup_loop(self):
        # Active cleanup of stale channel rows on a 10-minute cadence.
        while True:
            await asyncio.sleep(600)
            try:
                await self._cleanup_stale_memory()
            except Exception as e:
                logger.error(f"Memory cleanup error: {e}")

    async def _cleanup_stale_memory(self):
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=12)
        cleared = 0
        cleanup = getattr(self.memory, "cleanup_stale_channels", None)
        if callable(cleanup):
            cleared = await cleanup(cutoff)
        # Compatibility with lightweight history backends used by transports.
        else:
            for cid, msgs in list(getattr(self.memory, "memory", {}).items()):
                if not msgs:
                    continue
                ts = msgs[-1].get("timestamp")
                if not ts:
                    continue
                try:
                    if datetime.fromisoformat(ts) < cutoff:
                        await self.memory.clear_channel_memory(cid)
                        cleared += 1
                except Exception as e:
                    logger.debug("Cleanup skipped channel %s: %s", cid, e)
        # KeyedLocks self-prunes as it grows; this pass additionally drops
        # locks for rooms whose memory we just cleared, keeping anything still
        # held or still live.
        live_channels = set(getattr(self.memory, "memory", {}) or {})
        pruned_locks = self._channel_locks.prune(keep=live_channels, all_idle=True)
        pruned_state = self._prune_per_channel_state()
        if cleared or pruned_locks or pruned_state:
            logger.info(
                "Cleared %s stale channel memories, pruned %s idle channel locks "
                "and %s per-channel state entries",
                cleared,
                pruned_locks,
                pruned_state,
            )

    def _prune_per_channel_state(self) -> int:
        """Trim unbounded per-channel maps back under their caps.

        Eviction order is insertion order (dicts preserve it), so the rooms
        that go are the ones written to longest ago. Every value here is a
        cache or a hint — losing one costs at most a missed cooldown or an
        empty typing list, never correctness.
        """
        removed = 0
        for attr, cap in _PER_CHANNEL_STATE_CAPS.items():
            store = getattr(self, attr, None)
            if not isinstance(store, dict) or len(store) <= cap:
                continue
            for key in list(store)[: len(store) - cap // 2]:
                store.pop(key, None)
                removed += 1
        # Typing state expires on its own clock; drop rooms with nobody typing.
        typing = getattr(self, "_typing_users", None)
        if isinstance(typing, dict):
            for cid in [k for k, room in typing.items() if not room]:
                typing.pop(cid, None)
                removed += 1
        # Watch debounce buckets are popped by their own flush. One that was
        # cancelled mid-flight would otherwise sit here forever.
        debounce = getattr(self, "_watch_debounce", None)
        if isinstance(debounce, dict):
            for cid, bucket in list(debounce.items()):
                task = (bucket or {}).get("task")
                if task is None or task.done():
                    debounce.pop(cid, None)
                    removed += 1
        # Finished reply tasks that never made it through their finally.
        active = getattr(self, "_active_requests", None)
        if isinstance(active, dict):
            for cid, task in list(active.items()):
                if task is None or task.done():
                    active.pop(cid, None)
                    self._active_request_user.pop(cid, None)
                    removed += 1
        return removed
