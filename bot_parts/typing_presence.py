"""Typing-status tracking, presence formatting and live-typing helpers.

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

class TypingPresenceMixin:

    _TYPING_TTL_SECONDS = 10.0

    def _prune_typing(self, channel_id=None) -> None:
        """Drop expired typing rows. Optional channel_id limits the sweep."""
        now = time.monotonic()
        store = getattr(self, "_typing_users", None)
        if not isinstance(store, dict):
            self._typing_users = {}
            return
        keys = [str(channel_id)] if channel_id is not None else list(store)
        for cid in keys:
            room = store.get(str(cid))
            if not isinstance(room, dict):
                store.pop(str(cid), None)
                continue
            for uid, row in list(room.items()):
                try:
                    exp = float((row or {}).get("expires_at") or 0)
                except (TypeError, ValueError):
                    exp = 0.0
                if exp <= now:
                    room.pop(uid, None)
            if not room:
                store.pop(str(cid), None)

    def _note_typing(self, channel, user) -> None:
        """Record that this user started (or refreshed) typing in channel."""
        if channel is None or user is None:
            return
        uid = str(getattr(user, "id", "") or "")
        cid = str(getattr(channel, "id", "") or "")
        if not uid or not cid:
            return
        me = getattr(self, "user", None)
        if me is not None and uid == str(getattr(me, "id", "")):
            return
        if getattr(user, "bot", False):
            return
        if uid in (getattr(self, "_blacklist", None) or set()):
            return
        ignored = set(
            (getattr(self, "_control", None) or {}).get("ignore_users", []) or []
        )
        if uid in ignored:
            return
        # discord.py-self can attribute Maxwell's own DM typing to the
        # other person. While we are generating a reply there, ignore it.
        if cid in (getattr(self, "_replying_channels", None) or set()):
            if isinstance(channel, discord.DMChannel):
                return
        name = getattr(user, "display_name", None) or getattr(user, "name", None) or uid
        store = getattr(self, "_typing_users", None)
        if not isinstance(store, dict):
            self._typing_users = {}
            store = self._typing_users
        room = store.setdefault(cid, {})
        room[uid] = {
            "name": str(name),
            "expires_at": time.monotonic() + self._TYPING_TTL_SECONDS,
        }
        updater = getattr(self, "_update_recent_users", None)
        if callable(updater):
            with contextlib.suppress(Exception):
                updater(cid, user)
        if len(store) > 80:
            self._prune_typing()

    def _clear_typing(self, channel_id, user_id) -> None:
        cid = str(channel_id or "")
        uid = str(user_id or "")
        if not cid or not uid:
            return
        store = getattr(self, "_typing_users", None) or {}
        room = store.get(cid)
        if isinstance(room, dict):
            room.pop(uid, None)
            if not room:
                store.pop(cid, None)

    def _typing_in_channel(self, channel_id) -> list[dict]:
        """Humans currently typing in this room, newest refresh last."""
        self._prune_typing(channel_id)
        room = (getattr(self, "_typing_users", None) or {}).get(str(channel_id or ""))
        if not isinstance(room, dict):
            return []
        people = []
        for uid, row in room.items():
            name = str((row or {}).get("name") or uid)
            people.append({"id": str(uid), "name": name})
        return people

    def _typing_channel_ids(self) -> list[str]:
        self._prune_typing()
        return [
            cid
            for cid, room in (getattr(self, "_typing_users", None) or {}).items()
            if room
        ]

    def _typing_prompt_lines(self, channel_id) -> list[str]:
        people = self._typing_in_channel(channel_id)
        if not people:
            return []
        bits = [f"{p['name']}({p['id']})" for p in people[:6]]
        who = ", ".join(bits)
        verb = "is" if len(people) == 1 else "are"
        return [
            f"{who} {verb} typing in this room right now. Wait for them to send "
            "— don't talk over them. Prefer wait or no_response unless they "
            "already asked you something that needs an immediate answer."
        ]

    async def _user_label(self, uid: str, *, guild=None) -> str:
        """`DisplayName (id)` for commands. Falls back to the id if unknown.

        Cache only — listing blacklist/admins must not GET /users for every id.
        """
        uid = str(uid or "").strip()
        name = self._cached_user_display_name(uid, guild=guild)
        if name:
            return f"{name} ({uid})"
        return uid

    @staticmethod
    def _format_presence_activity(activity) -> str:
        if activity is None:
            return ""
        atype = getattr(activity, "type", None)
        kind = str(getattr(atype, "name", atype) or "").lower()
        title = str(
            getattr(activity, "name", None) or getattr(activity, "title", None) or ""
        ).strip()
        state = str(getattr(activity, "state", "") or "").strip()
        details = str(getattr(activity, "details", "") or "").strip()
        url = str(getattr(activity, "url", "") or "").strip()
        emoji = getattr(activity, "emoji", None)
        emoji_txt = ""
        if emoji is not None:
            emoji_txt = str(getattr(emoji, "name", None) or emoji or "").strip()
        artists = getattr(activity, "artists", None)
        song = str(getattr(activity, "title", "") or "").strip()
        if kind == "listening" or artists or song:
            track = song or title
            if artists:
                by = ", ".join(str(a) for a in artists)
                return f"listening to {track} by {by}" if track else f"listening to {by}"
            if track:
                return f"listening to {track}"
        if kind == "streaming":
            bit = f"streaming {title}" if title else "streaming"
            if url:
                bit += f" <{url}>"
            return bit
        if kind == "watching":
            return f"watching {title}" if title else "watching"
        if kind == "competing":
            return f"competing in {title}" if title else "competing"
        if kind == "playing":
            extra = " — ".join(x for x in (details, state) if x)
            if title and extra:
                return f"playing {title} ({extra})"
            return f"playing {title}" if title else "playing"
        if kind in {"custom", "hang"}:
            text = state or title
            if emoji_txt and text:
                return f"{emoji_txt} {text}"
            return text or emoji_txt
        return title or state

    def _reply_typing_delay(self, content: str) -> float:
        """How long to look like we're composing before the send lands."""
        n = len(str(content or "").strip())
        if n <= 0:
            return 0.35
        return max(0.35, min(1.2, 0.3 + n / 120.0))

    def _should_show_live_typing(self, message) -> bool:
        """Typing for the whole turn: @Maxwell, reply-to-Maxwell, or a DM."""
        if not (getattr(self, "_control", None) or {}).get("typing_indicator", True):
            return False
        if getattr(message, "suppress_typing", False):
            return False
        return bool(self._directly_addressed(message))

    async def _enter_live_typing(self, message):
        """Start Discord typing and keep refreshing until `_exit_live_typing`."""
        if not self._should_show_live_typing(message):
            return None
        return await typing_acquire(self, getattr(message, "channel", None))

    async def _exit_live_typing(self, handle) -> None:
        """Release a typing handle. Idempotent across overlapping cleanups."""
        await typing_release(handle)

    @contextlib.asynccontextmanager
    async def _reply_typing(self, channel, content: str = "", *, message=None):
        """Hold typing around the actual send, after a short compose delay."""
        if not (getattr(self, "_control", None) or {}).get("typing_indicator", True):
            yield
            return
        if message is not None and getattr(message, "suppress_typing", False):
            yield
            return
        handle = await typing_acquire(self, channel)
        try:
            delay = self._reply_typing_delay(content)
            if delay > 0:
                await asyncio.sleep(delay)
            yield
        finally:
            await typing_release(handle)
