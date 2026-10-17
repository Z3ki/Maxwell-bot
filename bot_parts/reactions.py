"""Reaction memory and reaction event handlers.

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

class ReactionsMixin:

    _MAX_REACTION_MESSAGES = 500
    _MAX_REACTORS_PER_MESSAGE = 40

    def _remember_reaction_message(self, message_id: str) -> None:
        mid = str(message_id or "").strip()
        if not mid:
            return
        store = getattr(self, "_message_reactions", None)
        order = getattr(self, "_message_reactions_order", None)
        if store is None:
            self._message_reactions = {}
            store = self._message_reactions
        if order is None:
            self._message_reactions_order = []
            order = self._message_reactions_order
        if mid in store:
            with contextlib.suppress(ValueError):
                order.remove(mid)
        else:
            store[mid] = []
        order.append(mid)
        while len(order) > self._MAX_REACTION_MESSAGES:
            old = order.pop(0)
            store.pop(old, None)

    def _record_message_reaction(
        self, message, user, emoji, *, added: bool = True
    ) -> list[dict]:
        mid = str(getattr(message, "id", "") or "")
        if not mid:
            return []
        uid = str(getattr(user, "id", "") or "")
        name = str(
            getattr(user, "display_name", None) or getattr(user, "name", None) or uid
        )
        mark = str(emoji or "")[:120]
        if not mark:
            return []
        self._remember_reaction_message(mid)
        rows = list(self._message_reactions.get(mid) or [])
        if added:
            if not any(
                row.get("user_id") == uid and row.get("emoji") == mark for row in rows
            ):
                rows.append({"emoji": mark, "user_id": uid, "user_name": name})
                if len(rows) > self._MAX_REACTORS_PER_MESSAGE:
                    rows = rows[-self._MAX_REACTORS_PER_MESSAGE :]
        else:
            rows = [
                row
                for row in rows
                if not (row.get("user_id") == uid and row.get("emoji") == mark)
            ]
        self._message_reactions[mid] = rows
        return rows

    def _reactions_annotation_for(self, target) -> str:
        mid = ""
        stored: list[dict] = []
        discord_msg = None
        if isinstance(target, dict):
            mid = str(target.get("message_id") or target.get("id") or "")
            raw = target.get("reactions")
            if isinstance(raw, list):
                stored = [item for item in raw if isinstance(item, dict)]
        else:
            mid = str(getattr(target, "id", "") or "")
            discord_msg = target
        overlays = getattr(self, "_message_reactions", None) or {}
        if mid in overlays:
            # An empty live overlay means the last reaction was removed.
            # Do not resurrect the persisted snapshot or Discord count cache.
            return format_reactions_annotation(overlays[mid])
        if stored:
            return format_reactions_annotation(stored)
        if discord_msg is None:
            return ""
        counts: list[dict] = []
        for reaction in list(getattr(discord_msg, "reactions", None) or []):
            mark = str(getattr(reaction, "emoji", "") or "")
            if not mark:
                continue
            try:
                count = int(getattr(reaction, "count", 0) or 0)
            except (TypeError, ValueError):
                count = 0
            counts.append({"emoji": mark, "count": count or 1})
        return format_reactions_annotation(counts)

    async def _persist_message_reactions(
        self, message_id: str, rows: list[dict]
    ) -> None:
        mem = getattr(self, "memory", None)
        merge = (
            getattr(mem, "merge_message_metadata", None) if mem is not None else None
        )
        if not callable(merge):
            return
        with contextlib.suppress(Exception):
            await merge(str(message_id), {"reactions": list(rows)})

    async def _note_reaction(self, reaction, user, *, added: bool) -> None:
        """Remember who reacted. Never start a live turn from an emoji."""
        if not MaxwellBot._dev_scope_allows(self, getattr(reaction, "message", None)):
            return
        if not self.user or getattr(user, "id", None) == self.user.id:
            return
        self._load_control()
        if not self._control.get("bot_enabled", True):
            return
        uid = str(getattr(user, "id", ""))
        if uid in getattr(self, "_blacklist", set()) or uid in set(
            self._control.get("ignore_users", []) or []
        ):
            return
        if getattr(user, "bot", False) and not self._control.get("reply_to_bots", True):
            return
        message = getattr(reaction, "message", None)
        if message is None:
            return
        emoji = str(getattr(reaction, "emoji", ""))[:120]
        rows = self._record_message_reaction(message, user, emoji, added=added)
        mid = str(getattr(message, "id", "") or "")
        if mid:
            await self._persist_message_reactions(mid, rows)
        logger.debug(
            "Reaction %s message=%s user=%s emoji=%s",
            "add" if added else "remove",
            mid,
            uid,
            emoji,
        )

    async def on_reaction_add(self, reaction, user):
        """Attach the reaction to that message so Maxwell sees it in context."""
        dispatch = getattr(self, "_dispatch_plugin_event", None)
        if callable(dispatch):
            dispatch("on_reaction_add", reaction, user)
        try:
            await self._note_reaction(reaction, user, added=True)
        except Exception as e:
            logger.warning(f"Failed recording reaction on message: {e}")

    async def on_reaction_remove(self, reaction, user):
        dispatch = getattr(self, "_dispatch_plugin_event", None)
        if callable(dispatch):
            dispatch("on_reaction_remove", reaction, user)
        try:
            await self._note_reaction(reaction, user, added=False)
        except Exception as e:
            logger.warning(f"Failed recording reaction removal: {e}")
