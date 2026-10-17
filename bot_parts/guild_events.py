"""Guild/member/relationship event handlers.

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

class GuildEventsMixin:

    def _friends_snapshot(self) -> dict:
        incoming: list[dict] = []
        outgoing: list[dict] = []
        friends: list[dict] = []
        for rel in getattr(self, "relationships", None) or []:
            user = getattr(rel, "user", None)
            uid = str(getattr(user, "id", "") or "")
            name = (
                getattr(user, "display_name", None)
                or getattr(user, "name", None)
                or uid
                or "?"
            )
            row = {"id": uid, "name": str(name)}
            typ = str(getattr(getattr(rel, "type", None), "name", "") or "")
            if typ == "incoming_request":
                incoming.append(row)
            elif typ == "outgoing_request":
                outgoing.append(row)
            elif typ == "friend":
                friends.append(row)
        return {
            "incoming_count": len(incoming),
            "outgoing_count": len(outgoing),
            "friend_count": len(friends),
            "incoming": incoming[:8],
            "friends": friends[:8],
        }

    async def on_relationship_add(self, relationship):
        if getattr(self.config, "MAXWELL_DEV_MODE", False):
            return
        # Official bots do not receive relationship events, and the inbox no
        # longer stores them. Plugins can still observe the dispatch.
        self._dispatch_plugin_event("on_relationship_add", relationship)

    async def on_relationship_update(self, before, after):
        if getattr(self.config, "MAXWELL_DEV_MODE", False):
            return
        self._dispatch_plugin_event("on_relationship_update", before, after)

    async def on_relationship_remove(self, relationship):
        if getattr(self.config, "MAXWELL_DEV_MODE", False):
            return
        self._dispatch_plugin_event("on_relationship_remove", relationship)

    async def on_group_join(self, channel, user):
        if getattr(self.config, "MAXWELL_DEV_MODE", False):
            return
        me = self.user
        if me is None or getattr(user, "id", None) != me.id:
            return
        name = getattr(channel, "name", None) or "group DM"
        try:
            await self.inbox.add_notice(
                kind="group_dm",
                summary=f"You were added to {name}",
                actor_id=str(getattr(channel, "id", "") or ""),
                actor_name=str(name),
                actions=["dismiss"],
                item_id=f"group_{getattr(channel, 'id', '')}",
            )
        except Exception as e:
            logger.warning("Inbox group_join notice failed: %s", e)

    async def on_guild_join(self, guild):
        """Someone added this official bot to a server."""
        if not MaxwellBot._dev_scope_allows(self, guild, guild_id=guild.id):
            return
        from discord_expressions import sync_guild
        sync_guild(self, guild)
        self._dispatch_plugin_event("on_guild_join", guild)
        with contextlib.suppress(Exception):
            logger.info("Joined guild: %s (id=%s)", guild.name, guild.id)
        try:
            await self.inbox.add_notice(
                kind="guild_join",
                summary=f"Joined server {guild.name}",
                actor_id=str(getattr(guild, "id", "") or ""),
                actor_name=str(getattr(guild, "name", "") or ""),
                actions=["dismiss"],
                item_id=f"guild_{getattr(guild, 'id', '')}",
            )
        except Exception as e:
            logger.debug("Inbox guild_join notice failed: %s", e)

    async def on_guild_remove(self, guild):
        self._guild_emojis.pop(str(guild.id), None)
        self._guild_stickers.pop(str(guild.id), None)
        self._dispatch_plugin_event("on_guild_remove", guild)

    async def on_guild_emojis_update(self, guild, before, after):
        if MaxwellBot._dev_scope_allows(self, guild, guild_id=guild.id):
            from discord_expressions import sync_guild
            sync_guild(self, guild, emojis=after)

    async def on_guild_stickers_update(self, guild, before, after):
        if MaxwellBot._dev_scope_allows(self, guild, guild_id=guild.id):
            from discord_expressions import sync_guild
            sync_guild(self, guild, stickers=after)


    async def on_member_join(self, member):
        self._dispatch_plugin_event("on_member_join", member)

    async def on_member_remove(self, member):
        self._dispatch_plugin_event("on_member_remove", member)

    async def on_member_update(self, before, after):
        self._dispatch_plugin_event("on_member_update", before, after)

    async def on_voice_state_update(self, member, before, after):
        self._dispatch_plugin_event("on_voice_state_update", member, before, after)

    async def on_presence_update(self, before, after):
        self._dispatch_plugin_event("on_presence_update", before, after)

    async def on_message_delete(self, message):
        self._dispatch_plugin_event("on_message_delete", message)

    async def on_guild_channel_create(self, channel):
        """A channel was created in a server Maxwell is in.

        Notices the room and, for support/ticket-style channels, posts a short
        opening line so Maxwell is present in the new room and it lands in his
        memory / conversation-watch scope (his own posts go through the normal
        memory path). Fire-and-forget: never raises, never blocks a turn. Gated
        by the server's ticket-greeting setting, ``bot_enabled``, and the bot
        actually having send permission in the channel.
        """
        if not MaxwellBot._dev_scope_allows(self, channel):
            return
        try:
            self._load_control()
        except Exception:
            return
        if not self._control.get("bot_enabled", True):
            return
        # Only real text channels can be greeted. Voice/category/stage/forum
        # events are observation-only, never a place to post a line. Guard with
        # isinstance; channel proxies in some gateway builds lack the type.
        if not isinstance(channel, discord.TextChannel):
            return
        guild = getattr(channel, "guild", None)
        if guild is None:
            return
        name = str(getattr(channel, "name", "") or "")
        if not name:
            return
        kind = channel_watch.channel_kind(name)
        try:
            logger.info(
                "Channel created in %s: #%s (id=%s, kind=%s)",
                guild.name,
                name,
                getattr(channel, "id", ""),
                kind,
            )
        except Exception:  # noqa: S110 - the failing statement IS the logger
            pass
        # A soloed server only allows one channel; don't poke a new one there.
        solo = self._solo_channel_for(guild)
        if solo and str(getattr(channel, "id", "")) != solo:
            return
        if kind != "ticket" or not self._ticket_greeting_enabled(
            str(getattr(guild, "id", "") or "")
        ):
            return
        me = getattr(guild, "me", None)
        if me is None:
            try:
                me = await guild.fetch_member(self.user.id)
            except Exception:
                me = None
        if me is None:
            return
        try:
            perms = channel.permissions_for(me)
        except Exception:
            return
        if not getattr(perms, "send_messages", False):
            return
        try:
            await channel.send(
                channel_watch.greeting_for(name, process_name(self))
            )
        except discord.Forbidden:
            logger.debug("no permission to greet new ticket channel #%s", name)
        except Exception as e:
            logger.debug("ticket channel greet failed for #%s: %s", name, e)
