"""Tool implementations for the discord_guild plugin.

Moved out of the historical bot_tools.py monolith. Shared helpers live in
``tooling.helpers``.
"""
from __future__ import annotations

from tooling import helpers as _helpers
from tools import Tool

# Mechanical split: the original classes used the bot_tools module globals.
# Bind every helper/name here so execute() bodies keep working unchanged.
for _name in dir(_helpers):
    if _name.startswith("__"):
        continue
    globals().setdefault(_name, getattr(_helpers, _name))
del _name

class LeaveServerTool(Tool):
    """Leave a Discord server by name or ID."""
    tool_name = 'leave_server'
    returns_result = True
    ends_turn = False
    requires_admin = True


    def get_description(self):
        return (
            "Leave a Discord server. Pass the server name or numeric ID "
            "(use list_servers to find them). Matches by ID first, then "
            "exact name, then partial/unique name. Reports errors clearly. "
            "Params: server (required)."
        )

    async def execute(
        self, message: Message, server: str | None = None, **kwargs
    ) -> str:
        if not _caller_is_admin(self.bot, message):
            return (
                "Error: leaving a server is restricted to admins. Ask an admin "
                "to run it."
            )
        target = (server or "").strip()
        if not target:
            return "Error: leave_server requires a server name or ID"

        guild, err = _find_guild(list(self.bot.guilds or []), target)
        if guild is None:
            return err

        try:
            await guild.leave()
            return f"LEFT {guild.name} (ID: {guild.id})"
        except discord.Forbidden as e:
            return f"Error leaving {guild.name}: forbidden (HTTP {e.status})"
        except discord.NotFound as e:
            return f"Error leaving {guild.name}: guild not found (HTTP {e.status})"
        except discord.HTTPException as e:
            return f"Error leaving {guild.name}: HTTP {e.status}: " + (
                e.text[:200] if e.text else ""
            )
        except Exception as e:
            return f"Error leaving {guild.name}: {type(e).__name__}: {e}"

class LookupUserTool(Tool):
    """Look up information about a Discord user including bio, roles, and banner"""
    tool_name = 'lookup_user'
    returns_result = True
    ends_turn = False


    def get_description(self):
        return (
            "Look up a Discord user by ID or mention. Params: user_id "
            "(required, numeric ID or @mention). Returns name, account creation date, "
            "banner, accent color, guild roles and permissions, avatar, and voice state."
        )

    async def execute(
        self, message: Message, user_id: str | None = None, **kwargs
    ) -> str:
        if not user_id:
            return "Error: user_id is required"
        # Strip mention syntax like <@123456> or <@!123456>
        cleaned = re.sub(r"[^0-9]", "", str(user_id))
        if not cleaned:
            return f"Error: Could not extract a numeric user ID from '{user_id}'"
        uid_int = int(cleaned)
        try:
            user = None
            getter = getattr(self.bot, "get_user", None)
            if callable(getter):
                with contextlib.suppress(Exception):
                    cached = getter(uid_int)
                    if cached is not None and int(cached.id) == uid_int:
                        user = cached
            if user is None:
                user = await self.bot.fetch_user(uid_int)
            if not user:
                return f"Error: User {user_id} not found"
            created = (
                user.created_at.strftime("%Y-%m-%d") if user.created_at else "unknown"
            )
            avatar = (
                getattr(user.display_avatar, "url", "none")
                if hasattr(user, "display_avatar")
                else getattr(user, "avatar_url", "none")
            )
            info_lines = [
                f"Name: {user.display_name} (@{user.name})",
                f"ID: {user.id}",
                f"Created: {created}",
                f"Bot: {user.bot}",
            ]

            # Banner and accent color
            banner = getattr(getattr(user, "banner", None), "url", None)
            if banner:
                info_lines.append(f"Banner: {banner}")
            accent = getattr(user, "accent_color", None)
            if accent is not None:
                val = getattr(accent, "value", None)
                if val is None and isinstance(accent, int):
                    val = accent
                if val is not None:
                    info_lines.append(f"Accent Color: #{val:06X}")

            # Guild-specific information if executed inside a guild
            guild = getattr(message, "guild", None)
            if guild:
                member = guild.get_member(uid_int)
                if member:
                    if getattr(member, "nick", None):
                        info_lines.append(f"Server Nickname: {member.nick}")
                    if getattr(member, "joined_at", None):
                        info_lines.append(
                            f"Server Joined: {member.joined_at.strftime('%Y-%m-%d')}"
                        )
                    info_lines.extend(_format_member_roles_detail(member, guild))

            info_lines.append(f"Avatar: {avatar}")

            # Voice channel presence
            member, voice_ch = _find_member_voice(
                self.bot, uid_int, getattr(message, "guild", None)
            )
            if voice_ch is not None:
                gname = getattr(getattr(voice_ch, "guild", None), "name", "?")
                info_lines.append(
                    f"Voice: in #{getattr(voice_ch, 'name', voice_ch.id)} ({gname})"
                )
            else:
                info_lines.append("Voice: not in a voice channel (from cached members)")

            return "\n".join(info_lines)
        except discord.NotFound:
            return f"Error: User {user_id} not found"
        except ValueError:
            return f"Error: Invalid user_id: {user_id}"
        except Exception as e:
            return f"Error looking up user: {e}"

class ListServersTool(Tool):
    """List all servers and group chats the bot is in"""
    tool_name = 'list_servers'
    returns_result = True
    ends_turn = False


    def get_description(self):
        return (
            "List your servers and group chats with ids, member counts, owner, "
            "your nick, and channel counts. No params."
        )

    async def execute(self, message: Message, **kwargs) -> str:
        lines = []
        guilds = list(getattr(self.bot, "guilds", None) or [])
        if guilds:
            lines.append(f"Servers ({len(guilds)}):")
            lines.extend(f"  • {_guild_server_line(guild)}" for guild in guilds[:20])
            if len(guilds) > 20:
                lines.append(f"  ... and {len(guilds) - 20} more")

        group_channels = [
            ch
            for ch in (getattr(self.bot, "private_channels", None) or [])
            if isinstance(ch, discord.GroupChannel)
        ]
        if group_channels:
            lines.append(f"\nGroup chats ({len(group_channels)}):")
            lines.extend(
                f"  • {gc.name or 'Unnamed'} (ID: {gc.id})"
                for gc in group_channels[:10]
            )

        if not lines:
            return "You're not in any servers or group chats."
        return "\n".join(lines)

class ListAdminServersTool(Tool):
    """List servers where Maxwell has useful admin permissions."""
    tool_name = 'list_admin_servers'
    returns_result = True
    ends_turn = False


    def get_description(self):
        return (
            "Inspect Discord roles/permissions. Shows which of YOUR roles grant "
            "mod/admin perms and which tools those unlock. No args: current "
            "server first, then every server where you have elevated perms. "
            "Params: guild_id (optional, one server in detail)."
        )

    async def execute(
        self, message: Message, guild_id: str | None = None, **kwargs
    ) -> str:
        current = getattr(message, "guild", None)
        wanted = _parse_snowflake(guild_id)
        if wanted is not None:
            guild = self.bot.get_guild(wanted)
            if guild is None:
                return f"Error: I am not in server {guild_id} or it is not cached"
            return _guild_access_detail(guild)

        blocks = []
        if current is not None:
            blocks.append("This server:\n" + _guild_access_detail(current))
        others = []
        for guild in getattr(self.bot, "guilds", []) or []:
            if current is not None and getattr(guild, "id", None) == getattr(
                current, "id", None
            ):
                continue
            caps, _reason = _admin_caps(guild)
            if not caps:
                continue
            others.append(_guild_access_detail(guild))
        if others:
            blocks.append(
                "Other servers with elevated perms:\n" + "\n\n".join(others[:20])
            )
        if not blocks:
            return (
                "No cached Discord member/permissions. I cannot see roles in "
                "any joined server right now."
            )
        if current is None and not others:
            return (
                "No servers with cached manage_channels/mod permissions. "
                "Don't try kick/ban/channel/role tools until this lists a target."
            )
        return "\n\n".join(blocks)

class ListChannelsTool(Tool):
    """List a guild's channels with ids and settings."""
    tool_name = 'list_channels'
    returns_result = True
    ends_turn = False


    def get_description(self):
        return (
            "List this server's channels with ids, type, category, topic, nsfw, "
            "slowmode, and who is in voice. Params: guild_id (optional), "
            "kind (text|voice|category|forum|stage|all), query (optional name "
            "filter), category_id (optional)."
        )

    async def execute(
        self,
        message: Message,
        guild_id: str | None = None,
        kind: str | None = None,
        query: str | None = None,
        category_id: str | None = None,
        **kwargs,
    ) -> str:
        guild, error = await _resolve_guild(self.bot, message, guild_id)
        if error:
            return error
        if guild is None:
            return "Error: guild is unavailable"
        return _render_channel_map(
            guild,
            kind=kind,
            query=query,
            category_id=category_id,
            limit=80,
            include_topic=True,
        )

class ListRolesTool(Tool):
    """Read-only role listing with ids, counts, and elevated perms."""
    tool_name = 'list_roles'
    returns_result = True
    ends_turn = False


    def get_description(self):
        return (
            "List this server's roles with ids, position, color, member count, "
            "hoist/mentionable, and elevated perms. Read-only — does not need "
            "manage_roles. Params: guild_id (optional), query (optional name filter)."
        )

    async def execute(
        self,
        message: Message,
        guild_id: str | None = None,
        query: str | None = None,
        **kwargs,
    ) -> str:
        guild, error = await _resolve_guild(self.bot, message, guild_id)
        if error:
            return error
        if guild is None:
            return "Error: guild is unavailable"
        return _render_role_list(guild, query=query, limit=50)

class ListMembersTool(Tool):
    """List guild members with nicks, roles, status, and voice."""
    tool_name = 'list_members'
    returns_result = True
    ends_turn = False


    def get_description(self):
        return (
            "List members in this server: id, nick, username, roles, status, "
            "joined date, timeout, voice. Params: guild_id (optional), "
            "query (name/nick/id), role or role_id (optional), "
            "status (online|idle|dnd|offline|all), limit (default 40, max 80)."
        )

    async def execute(
        self,
        message: Message,
        guild_id: str | None = None,
        query: str | None = None,
        role: str | None = None,
        role_id: str | None = None,
        status: str | None = None,
        limit: str | int | None = None,
        **kwargs,
    ) -> str:
        guild, error = await _resolve_guild(self.bot, message, guild_id)
        if error:
            return error
        if guild is None:
            return "Error: guild is unavailable"
        try:
            cap = max(1, min(int(limit or 40), 80))
        except (TypeError, ValueError):
            cap = 40
        return _render_member_list(
            guild,
            query=query,
            role_spec=role_id or role,
            status=status,
            limit=cap,
        )

class CreateCategoryTool(Tool):
    """Create a Discord category channel."""
    tool_name = 'create_category'
    returns_result = True
    ends_turn = False


    def get_description(self):
        return (
            "Create a Discord category (the separator/group that channels sit under). Requires manage_channels. "
            "Params: name (required), guild_id (optional unless not in that server), position (optional), nsfw (optional). "
            "Use list_admin_servers first to pick a server where manage_channels is available."
        )

    async def execute(
        self,
        message: Message,
        name: str | None = None,
        guild_id: str | None = None,
        position: str | None = None,
        nsfw: str = "false",
        **kwargs,
    ) -> str:
        clean = _clean_discord_name(name)
        if not clean:
            return "Error: name is required"
        guild, error = await _resolve_guild(self.bot, message, guild_id)
        if error:
            return error
        if guild is None:
            return "Error: guild is unavailable"
        guild = cast(Any, guild)
        missing = _missing_cap(guild, "manage_channels", message)
        if missing:
            return missing
        try:
            create_kwargs = {
                "name": clean,
                "reason": _mod_reason(message),
            }
            if parse_bool(nsfw, False):
                create_kwargs["nsfw"] = True
            category = await guild.create_category(**create_kwargs)
            if position is not None:
                try:
                    await category.edit(
                        position=max(0, int(position)),
                        reason=f"{process_name() or 'Bot'} admin tool position update",
                    )
                except (TypeError, ValueError):
                    return f"Created category {category.name} ({category.id}), but position was invalid"
            return f"Created category {category.name} ({category.id}) in {guild.name}"
        except discord.Forbidden:
            return f"Error: Discord denied creating category in {guild.name}; missing manage_channels or role hierarchy issue"
        except Exception as e:
            return f"Error creating category: {e}"

class CreateChannelTool(Tool):
    """Create text, voice, announcement, forum, or stage channels."""
    tool_name = 'create_channel'
    returns_result = True
    ends_turn = False


    def get_description(self):
        return (
            "Create a Discord channel. Requires manage_channels. "
            "Params: name (required), kind/type (text, voice, announcement, forum, or stage; default text), "
            "guild_id (optional), category_id or category_name (optional), topic (optional), "
            "nsfw (optional), slowmode_seconds (optional), bitrate and user_limit (voice/stage). "
            "Use create_category first when the user wants a new channel group/section."
        )

    async def execute(
        self,
        message: Message,
        name: str | None = None,
        kind: str | None = None,
        type: str | None = None,
        guild_id: str | None = None,
        category_id: str | None = None,
        category_name: str | None = None,
        topic: str | None = None,
        nsfw: str = "false",
        slowmode_seconds: str = "0",
        bitrate: str | None = None,
        user_limit: str | None = None,
        **kwargs,
    ) -> str:
        clean = _clean_channel_name(name)
        if not clean:
            return "Error: name is required"
        guild, error = await _resolve_guild(self.bot, message, guild_id)
        if error:
            return error
        if guild is None:
            return "Error: guild is unavailable"
        guild = cast(Any, guild)
        missing = _missing_cap(guild, "manage_channels", message)
        if missing:
            return missing
        category, error = _find_category(guild, category_id, category_name)
        category = cast(Any, category)
        if error:
            return error
        channel_kind = str(kind or type or "text").strip().lower()
        nsfw_flag = parse_bool(nsfw, False)
        try:
            slowmode = max(0, min(int(slowmode_seconds or 0), 21600))
        except (TypeError, ValueError):
            slowmode = 0
        voice_kwargs = {"category": category, "reason": _mod_reason(message)}
        if bitrate:
            try:
                voice_kwargs["bitrate"] = max(8000, min(int(bitrate), 384000))
            except (TypeError, ValueError):
                return "Error: bitrate must be a number"
        if user_limit:
            try:
                voice_kwargs["user_limit"] = max(0, min(int(user_limit), 99))
            except (TypeError, ValueError):
                return "Error: user_limit must be a number"
        try:
            if channel_kind in {"voice", "vc"}:
                channel = await guild.create_voice_channel(clean, **voice_kwargs)
            elif channel_kind in {"stage", "stage_voice"}:
                create_stage = getattr(guild, "create_stage_channel", None)
                if not callable(create_stage):
                    return "Error: this Discord library cannot create stage channels"
                channel = await create_stage(clean, **voice_kwargs)
            elif channel_kind in {"forum"}:
                create_forum = getattr(guild, "create_forum_channel", None)
                if not callable(create_forum):
                    return "Error: this Discord library cannot create forum channels"
                channel = await create_forum(
                    clean,
                    category=category,
                    topic=str(topic or "")[:1024],
                    nsfw=nsfw_flag,
                    reason=_mod_reason(message),
                )
            elif channel_kind in {"text", "chat", "announcement", "news"}:
                channel = await guild.create_text_channel(
                    clean,
                    category=category,
                    topic=str(topic or "")[:1024],
                    nsfw=nsfw_flag,
                    slowmode_delay=slowmode,
                    news=channel_kind in {"announcement", "news"},
                    reason=_mod_reason(message),
                )
            else:
                return "Error: kind/type must be text, voice, announcement, forum, or stage"
            where = f" under {category.name}" if category else ""
            return f"Created {channel_kind} channel {_channel_label(channel)} in {guild.name}{where}"
        except TypeError:
            if channel_kind in {"announcement", "news"}:
                return "Error: this Discord library cannot create announcement channels"
            return f"Error creating {channel_kind} channel: unsupported argument"
        except discord.Forbidden:
            return f"Error: Discord denied creating channel in {guild.name}; missing manage_channels or role hierarchy issue"
        except Exception as e:
            return f"Error creating channel: {e}"

class EditChannelTool(Tool):
    """Rename/move/update basic channel settings."""
    tool_name = 'edit_channel'
    returns_result = True
    ends_turn = False


    def get_description(self):
        return (
            "Edit a Discord channel. Requires manage_channels. Params: channel_id (required), "
            "name (optional), category_id or category_name (optional), topic (text only, optional), slowmode_seconds (text only, optional), nsfw (text only, optional)."
        )

    async def execute(
        self,
        message: Message,
        channel_id: str | None = None,
        name: str | None = None,
        category_id: str | None = None,
        category_name: str | None = None,
        topic: str | None = None,
        slowmode_seconds: str | None = None,
        nsfw: str | None = None,
        position: str | None = None,
        bitrate: str | None = None,
        user_limit: str | None = None,
        **kwargs,
    ) -> str:
        if not channel_id:
            return "Error: channel_id is required"
        try:
            channel = self.bot.get_channel(
                int(channel_id)
            ) or await self.bot.fetch_channel(int(channel_id))
        except (TypeError, ValueError):
            return f"Error: invalid channel_id: {channel_id}"
        except Exception as e:
            return f"Error finding channel: {e}"
        guild = getattr(channel, "guild", None)
        if not guild:
            return "Error: channel is not in a server"
        missing = _missing_cap(guild, "manage_channels", message)
        if missing:
            return missing
        updates = {}
        if name:
            clean = (
                _clean_channel_name(name)
                if not isinstance(channel, discord.CategoryChannel)
                else _clean_discord_name(name)
            )
            if clean:
                updates["name"] = clean
        if category_id or category_name:
            category, error = _find_category(guild, category_id, category_name)
            if error:
                return error
            updates["category"] = category
        textish = isinstance(channel, discord.TextChannel) or isinstance(
            channel, getattr(discord, "ForumChannel", ())
        )
        if textish:
            if topic is not None:
                updates["topic"] = str(topic)[:1024]
            if slowmode_seconds is not None:
                try:
                    updates["slowmode_delay"] = max(
                        0, min(int(slowmode_seconds), 21600)
                    )
                except (TypeError, ValueError):
                    return "Error: slowmode_seconds must be a number"
            if nsfw is not None:
                updates["nsfw"] = parse_bool(nsfw, False)
        elif topic is not None or slowmode_seconds is not None or nsfw is not None:
            if isinstance(channel, discord.CategoryChannel) and nsfw is not None:
                updates["nsfw"] = parse_bool(nsfw, False)
            elif topic is not None or slowmode_seconds is not None:
                return (
                    "Error: topic and slowmode_seconds only apply to text/forum channels"
                )
        if bitrate is not None or user_limit is not None:
            if not (
                isinstance(channel, discord.VoiceChannel)
                or isinstance(channel, getattr(discord, "StageChannel", ()))
            ):
                return "Error: bitrate and user_limit only apply to voice/stage channels"
            if bitrate is not None:
                try:
                    updates["bitrate"] = max(8000, min(int(bitrate), 384000))
                except (TypeError, ValueError):
                    return "Error: bitrate must be a number"
            if user_limit is not None:
                try:
                    updates["user_limit"] = max(0, min(int(user_limit), 99))
                except (TypeError, ValueError):
                    return "Error: user_limit must be a number"
        if position is not None:
            try:
                updates["position"] = max(0, int(position))
            except (TypeError, ValueError):
                return "Error: position must be a number"
        if not updates:
            return "Error: provide at least one edit field"
        try:
            await channel.edit(
                **updates, reason=_mod_reason(message)
            )
            return f"Edited {_channel_label(channel)} in {guild.name}: {', '.join(sorted(updates))}"
        except discord.Forbidden:
            return f"Error: Discord denied editing {_channel_label(channel)}; missing manage_channels or role hierarchy issue"
        except Exception as e:
            return f"Error editing channel: {e}"

class DeleteChannelTool(Tool):
    """Delete a Discord channel with name confirmation."""
    tool_name = 'delete_channel'
    returns_result = True
    ends_turn = False


    def get_description(self):
        return (
            "Delete a Discord channel or category. Dangerous. Requires manage_channels. "
            "Params: channel_id (required), confirm_name (required and must exactly match the channel/category name)."
        )

    async def execute(
        self,
        message: Message,
        channel_id: str | None = None,
        confirm_name: str | None = None,
        **kwargs,
    ) -> str:
        if not channel_id or not confirm_name:
            return "Error: channel_id and confirm_name are required"
        try:
            channel = self.bot.get_channel(
                int(channel_id)
            ) or await self.bot.fetch_channel(int(channel_id))
        except (TypeError, ValueError):
            return f"Error: invalid channel_id: {channel_id}"
        except Exception as e:
            return f"Error finding channel: {e}"
        guild = getattr(channel, "guild", None)
        if not guild:
            return "Error: channel is not in a server"
        missing = _missing_cap(guild, "manage_channels", message)
        if missing:
            return missing
        actual = getattr(channel, "name", "")
        if str(confirm_name) != actual:
            return f"Error: confirm_name must exactly match '{actual}'"
        try:
            label = _channel_label(channel)
            await channel.delete(
                reason=_mod_reason(message)
            )
            return f"Deleted {label} from {guild.name}"
        except discord.Forbidden:
            return f"Error: Discord denied deleting {_channel_label(channel)}; missing manage_channels or role hierarchy issue"
        except Exception as e:
            return f"Error deleting channel: {e}"

class EditCategoryTool(Tool):
    """Rename/reorder a Discord category."""
    tool_name = 'edit_category'
    returns_result = True
    ends_turn = False


    def get_description(self):
        return (
            "Edit a Discord category (name, position, nsfw). Requires manage_channels. "
            "Params: category_id or category_name, name, position, nsfw, guild_id."
        )

    async def execute(
        self,
        message: Message,
        category_id: str | None = None,
        category_name: str | None = None,
        name: str | None = None,
        position: str | None = None,
        nsfw: str | None = None,
        guild_id: str | None = None,
        **kwargs,
    ) -> str:
        guild, error = await _resolve_guild(self.bot, message, guild_id)
        if error:
            return error
        if guild is None:
            return "Error: guild is unavailable"
        missing = _missing_cap(guild, "manage_channels", message)
        if missing:
            return missing
        category, error = _find_category(guild, category_id, category_name)
        if error:
            return error
        if category is None:
            return "Error: category_id or category_name is required"
        updates = {}
        if name:
            clean = _clean_discord_name(name)
            if clean:
                updates["name"] = clean
        if position is not None:
            try:
                updates["position"] = max(0, int(position))
            except (TypeError, ValueError):
                return "Error: position must be a number"
        if nsfw is not None:
            updates["nsfw"] = parse_bool(nsfw, False)
        if not updates:
            return "Error: provide name, position, or nsfw"
        try:
            await category.edit(**updates, reason=_mod_reason(message))
            return (
                f"Edited category {category.name} ({category.id}) in {guild.name}: "
                + ", ".join(sorted(updates))
            )
        except discord.Forbidden:
            return f"Error: Discord denied editing category {category.name}"
        except Exception as e:
            return f"Error editing category: {e}"

class MoveChannelTool(Tool):
    """Move a channel into or out of a category."""
    tool_name = 'move_channel'
    returns_result = True
    ends_turn = False


    def get_description(self):
        return (
            "Move a Discord channel into a category, or out of one. Requires manage_channels. "
            "Params: channel_id (required), category_id or category_name "
            "(use 'none' to uncategorize), position (optional)."
        )

    async def execute(
        self,
        message: Message,
        channel_id: str | None = None,
        category_id: str | None = None,
        category_name: str | None = None,
        position: str | None = None,
        **kwargs,
    ) -> str:
        if not channel_id:
            return "Error: channel_id is required"
        channel, error = await _get_guild_channel(self.bot, channel_id)
        if error:
            return error
        guild = getattr(channel, "guild", None)
        if guild is None:
            return "Error: channel is not in a server"
        if isinstance(channel, discord.CategoryChannel):
            return "Error: cannot move a category into a category; use edit_category position"
        missing = _missing_cap(guild, "manage_channels", message)
        if missing:
            return missing
        if category_id is None and category_name is None:
            return "Error: category_id, category_name, or category_id=none is required"
        category, error = _find_category(guild, category_id, category_name)
        if error:
            return error
        updates = {"category": category}
        if position is not None:
            try:
                updates["position"] = max(0, int(position))
            except (TypeError, ValueError):
                return "Error: position must be a number"
        try:
            await channel.edit(**updates, reason=_mod_reason(message))
            where = f"under {category.name}" if category else "uncategorized"
            return f"Moved {_channel_label(channel)} {where} in {guild.name}"
        except discord.Forbidden:
            return f"Error: Discord denied moving {_channel_label(channel)}"
        except Exception as e:
            return f"Error moving channel: {e}"

class CloneChannelTool(Tool):
    """Clone a channel, including permission overwrites."""
    tool_name = 'clone_channel'
    returns_result = True
    ends_turn = False


    def get_description(self):
        return (
            "Clone a Discord channel or category, copying permission overwrites. "
            "Requires manage_channels. Params: channel_id (required), name (optional), "
            "category_id or category_name (optional destination)."
        )

    async def execute(
        self,
        message: Message,
        channel_id: str | None = None,
        name: str | None = None,
        category_id: str | None = None,
        category_name: str | None = None,
        **kwargs,
    ) -> str:
        if not channel_id:
            return "Error: channel_id is required"
        channel, error = await _get_guild_channel(self.bot, channel_id)
        if error:
            return error
        guild = getattr(channel, "guild", None)
        if guild is None:
            return "Error: channel is not in a server"
        missing = _missing_cap(guild, "manage_channels", message)
        if missing:
            return missing
        clone_kwargs = {"reason": _mod_reason(message)}
        if name:
            clean = (
                _clean_discord_name(name)
                if isinstance(channel, discord.CategoryChannel)
                else _clean_channel_name(name)
            )
            if clean:
                clone_kwargs["name"] = clean
        if category_id or category_name:
            category, error = _find_category(guild, category_id, category_name)
            if error:
                return error
            clone_kwargs["category"] = category
        if not hasattr(channel, "clone"):
            return "Error: this channel type cannot be cloned"
        try:
            clone = await channel.clone(**clone_kwargs)
            return f"Cloned {_channel_label(channel)} -> {_channel_label(clone)} in {guild.name}"
        except discord.Forbidden:
            return f"Error: Discord denied cloning {_channel_label(channel)}"
        except Exception as e:
            return f"Error cloning channel: {e}"

class SyncChannelTool(Tool):
    """Sync channel permission overwrites with the parent category."""
    tool_name = 'sync_channel'
    returns_result = True
    ends_turn = False


    def get_description(self):
        return (
            "Sync a channel's permission overwrites with its parent category, "
            "or sync every child of a category. Requires manage_channels. "
            "Params: channel_id (channel or category)."
        )

    async def execute(
        self,
        message: Message,
        channel_id: str | None = None,
        **kwargs,
    ) -> str:
        if not channel_id:
            return "Error: channel_id is required"
        channel, error = await _get_guild_channel(self.bot, channel_id)
        if error:
            return error
        guild = getattr(channel, "guild", None)
        if guild is None:
            return "Error: channel is not in a server"
        missing = _missing_cap(guild, "manage_channels", message)
        if missing:
            return missing
        why = _mod_reason(message)
        targets = []
        if isinstance(channel, discord.CategoryChannel):
            targets = list(getattr(channel, "channels", []) or [])
            if not targets:
                return f"Category {channel.name} has no channels to sync"
        else:
            if getattr(channel, "category", None) is None:
                return f"Error: {_channel_label(channel)} is not in a category"
            targets = [channel]
        synced = []
        errors = []
        for ch in targets:
            try:
                await ch.edit(sync_permissions=True, reason=why)
                synced.append(_channel_label(ch))
            except Exception as e:
                errors.append(f"{_channel_label(ch)}: {e}")
        if not synced:
            return "Error: could not sync: " + "; ".join(errors)
        out = f"Synced {len(synced)} channel(s): " + ", ".join(synced)
        if errors:
            out += " | failed: " + "; ".join(errors)
        return out

class ListPermissionsTool(Tool):
    """Show permission overwrites on a channel or category."""
    tool_name = 'list_permissions'
    returns_result = True
    ends_turn = False


    def get_description(self):
        return (
            "List permission overwrites on a channel or category. "
            "Needs manage_roles or manage_channels. Params: channel_id (required)."
        )

    async def execute(
        self,
        message: Message,
        channel_id: str | None = None,
        **kwargs,
    ) -> str:
        if not channel_id:
            return "Error: channel_id is required"
        channel, error = await _get_guild_channel(self.bot, channel_id)
        if error:
            return error
        guild = getattr(channel, "guild", None)
        if guild is None:
            return "Error: channel is not in a server"
        missing = _missing_cap(
            guild, "manage_roles", message, alt_caps=("manage_channels",)
        )
        if missing:
            return missing
        overwrites = getattr(channel, "overwrites", None) or {}
        if not overwrites:
            synced = getattr(channel, "permissions_synced", None)
            extra = " (synced with category)" if synced else ""
            return f"No overwrites on {_channel_label(channel)}{extra}"
        lines = [f"Overwrites on {_channel_label(channel)}:"]
        for target, ow in list(overwrites.items())[:30]:
            tname = getattr(target, "name", None) or getattr(target, "id", target)
            allow = []
            deny = []
            pair_iter = getattr(ow, "__iter__", None)
            try:
                for perm, value in ow:
                    if value is True:
                        allow.append(perm)
                    elif value is False:
                        deny.append(perm)
            except Exception:
                allow = [p for p, v in (pair_iter() if pair_iter else []) if v is True]
            bits = []
            if allow:
                bits.append("allow=" + ",".join(allow[:12]))
            if deny:
                bits.append("deny=" + ",".join(deny[:12]))
            lines.append(f"  {tname}: " + ("; ".join(bits) or "(empty)"))
        synced = getattr(channel, "permissions_synced", None)
        if synced:
            lines.append("synced with parent category: yes")
        elif synced is False:
            lines.append("synced with parent category: no")
        return "\n".join(lines)

class ManageInvitesTool(Tool):
    """List or revoke server invites."""
    tool_name = 'manage_invites'
    returns_result = True
    ends_turn = False


    def get_description(self):
        return (
            "List or revoke Discord invites. Requires create_instant_invite. "
            "Params: action (list|revoke), code (for revoke), guild_id, channel_id (list filter)."
        )

    async def execute(
        self,
        message: Message,
        action: str | None = None,
        code: str | None = None,
        guild_id: str | None = None,
        channel_id: str | None = None,
        **kwargs,
    ) -> str:
        guild, error = await _resolve_guild(self.bot, message, guild_id)
        if error:
            return error
        missing = _missing_cap(guild, "create_instant_invite", message)
        if missing:
            return missing
        act = str(action or "list").strip().lower()
        if act == "list":
            try:
                invites = await guild.invites()
            except discord.Forbidden:
                return f"Error: cannot list invites in {guild.name}"
            except Exception as e:
                return f"Error listing invites: {e}"
            if channel_id:
                try:
                    cid = int(str(channel_id).strip())
                except (TypeError, ValueError):
                    return f"Error: invalid channel_id: {channel_id}"
                invites = [
                    inv
                    for inv in invites
                    if getattr(getattr(inv, "channel", None), "id", None) == cid
                ]
            if not invites:
                return f"No invites in {guild.name}"
            rows = []
            for inv in invites[:40]:
                ch = getattr(inv, "channel", None)
                uses = getattr(inv, "uses", 0)
                max_uses = getattr(inv, "max_uses", 0) or "inf"
                inviter = getattr(getattr(inv, "inviter", None), "name", "?")
                rows.append(
                    f"{inv.code} #{getattr(ch, 'name', '?')} uses={uses}/{max_uses} by {inviter}"
                )
            return f"Invites in {guild.name} ({len(invites)}):\n" + "\n".join(rows)
        if act == "revoke":
            token = str(code or "").strip()
            if not token:
                return "Error: code is required to revoke"
            token = token.rsplit("/", 1)[-1]
            try:
                invites = await guild.invites()
            except Exception as e:
                return f"Error listing invites: {e}"
            match = next((inv for inv in invites if inv.code == token), None)
            if match is None:
                return f"Error: invite {token} not found in {guild.name}"
            try:
                await match.delete(reason=_mod_reason(message))
                return f"Revoked invite {token} in {guild.name}"
            except discord.Forbidden:
                return f"Error: Discord denied revoking invite {token}"
            except Exception as e:
                return f"Error revoking invite: {e}"
        return "Error: action must be list or revoke"

class EditServerTool(Tool):
    tool_name = 'edit_server'
    returns_result = True
    ends_turn = False

    def get_description(self):
        return (
            "Edit server settings. Requires manage_guild. Params: name, description, "
            "verification_level (none|low|medium|high|highest), "
            "explicit_content_filter (disabled|no_role|all_members), "
            "afk_channel_id (or none), afk_timeout, system_channel_id (or none), "
            "icon_url, guild_id."
        )

    async def execute(
        self,
        message: Message,
        name: str | None = None,
        description: str | None = None,
        verification_level: str | None = None,
        explicit_content_filter: str | None = None,
        afk_channel_id: str | None = None,
        afk_timeout: str | None = None,
        system_channel_id: str | None = None,
        icon_url: str | None = None,
        guild_id: str | None = None,
        **kwargs,
    ) -> str:
        guild, error = await _resolve_guild(self.bot, message, guild_id)
        if error:
            return error
        missing = _missing_cap(guild, "manage_guild", message)
        if missing:
            return missing
        updates = {}
        if name:
            clean = _clean_discord_name(name)
            if clean:
                updates["name"] = clean
        if description is not None:
            updates["description"] = str(description)[:120]
        if verification_level:
            levels = getattr(discord, "VerificationLevel", None)
            key = str(verification_level).strip().lower()
            mapping = {
                "none": "none",
                "low": "low",
                "medium": "medium",
                "high": "high",
                "highest": "highest",
                "very_high": "highest",
            }
            attr = mapping.get(key)
            level = getattr(levels, attr, None) if levels is not None and attr else None
            if level is None:
                return "Error: verification_level must be none, low, medium, high, or highest"
            updates["verification_level"] = level
        if explicit_content_filter:
            filt = getattr(discord, "ContentFilter", None)
            key = str(explicit_content_filter).strip().lower().replace("-", "_")
            mapping = {
                "disabled": "disabled",
                "none": "disabled",
                "off": "disabled",
                "no_role": "no_role",
                "members_without_roles": "no_role",
                "all_members": "all_members",
                "all": "all_members",
            }
            attr = mapping.get(key)
            value = getattr(filt, attr, None) if filt is not None and attr else None
            if value is None:
                return "Error: explicit_content_filter must be disabled, no_role, or all_members"
            updates["explicit_content_filter"] = value
        if afk_timeout is not None:
            try:
                updates["afk_timeout"] = max(60, min(int(afk_timeout), 3600))
            except (TypeError, ValueError):
                return "Error: afk_timeout must be a number of seconds"
        if afk_channel_id is not None:
            raw = str(afk_channel_id).strip().lower()
            if raw in {"none", "null", "off", "0"}:
                updates["afk_channel"] = None
            else:
                ch, err = await _get_guild_channel(self.bot, afk_channel_id)
                if err:
                    return err
                updates["afk_channel"] = ch
        if system_channel_id is not None:
            raw = str(system_channel_id).strip().lower()
            if raw in {"none", "null", "off", "0"}:
                updates["system_channel"] = None
            else:
                ch, err = await _get_guild_channel(self.bot, system_channel_id)
                if err:
                    return err
                updates["system_channel"] = ch
        if icon_url:
            if not _is_safe_url(icon_url):
                return "Error: icon_url must be a public http(s) URL"
            try:
                session = await _get_shared_session()
                async with session.get(
                    icon_url,
                    timeout=aiohttp.ClientTimeout(total=30),
                    allow_redirects=False,
                ) as resp:
                    if resp.status != 200:
                        return f"Error: could not download icon (status {resp.status})"
                    updates["icon"] = await _read_response_limited(resp, 10 * 1024 * 1024)
            except Exception as e:
                return f"Error downloading icon: {e}"
        if not updates:
            return "Error: provide at least one server setting to edit"
        try:
            await guild.edit(**updates, reason=_mod_reason(message))
            shown = [k for k in updates if k != "icon"]
            if "icon" in updates:
                shown.append("icon")
            return f"Edited {guild.name}: {', '.join(sorted(shown))}"
        except discord.Forbidden:
            return f"Error: Discord denied editing {guild.name}"
        except Exception as e:
            return f"Error editing server: {e}"

class AuditLogTool(Tool):
    tool_name = 'audit_log'
    returns_result = True
    ends_turn = False

    def get_description(self):
        return (
            "Read recent audit-log entries. Requires view_audit_log. "
            "Params: guild_id (optional), limit (optional, default 10)."
        )

    async def execute(
        self,
        message: Message,
        guild_id: str | None = None,
        limit: str = "10",
        **kwargs,
    ) -> str:
        guild, error = await _resolve_guild(self.bot, message, guild_id)
        if error:
            return error
        missing = _missing_cap(guild, "view_audit_log", message)
        if missing:
            return missing
        try:
            cap = max(1, min(int(limit or 10), 25))
        except (TypeError, ValueError):
            cap = 10
        rows = []
        try:
            async for entry in guild.audit_logs(limit=cap):
                actor = getattr(getattr(entry, "user", None), "name", "?")
                action = getattr(getattr(entry, "action", None), "name", entry.action)
                target = getattr(entry, "target", None)
                rows.append(f"{actor} {action} {target}")
        except discord.Forbidden:
            return f"Error: cannot read audit log in {guild.name}"
        except Exception as e:
            return f"Error reading audit log: {e}"
        if not rows:
            return f"No audit-log entries in {guild.name}"
        return f"Audit log for {guild.name}:\n" + "\n".join(rows)

class ManageEmojiTool(Tool):
    tool_name = 'manage_emoji'
    returns_result = True
    ends_turn = False

    def get_description(self):
        return (
            "List, create, or delete custom emojis. Requires manage_expressions. "
            "Params: action (list|create|delete), name, url (image for create), "
            "emoji_id or name (for delete), guild_id (optional)."
        )

    async def execute(
        self,
        message: Message,
        action: str | None = None,
        name: str | None = None,
        url: str | None = None,
        emoji_id: str | None = None,
        guild_id: str | None = None,
        **kwargs,
    ) -> str:
        guild, error = await _resolve_guild(self.bot, message, guild_id)
        if error:
            return error
        missing = _missing_cap(guild, "manage_expressions", message)
        if missing:
            return missing
        act = str(action or "list").strip().lower()
        emojis = list(getattr(guild, "emojis", []) or [])
        if act == "list":
            if not emojis:
                return f"No custom emojis in {guild.name}"
            return f"Emojis in {guild.name} ({len(emojis)}):\n" + "\n".join(
                f":{e.name}: ({e.id})" for e in emojis[:40]
            )
        if act == "create":
            clean = re.sub(r"[^A-Za-z0-9_]", "", str(name or ""))[:32]
            if len(clean) < 2:
                return "Error: emoji name must be 2-32 letters/numbers/underscore"
            if not url or not _is_safe_url(url):
                return "Error: a public image url is required"
            try:
                session = await _get_shared_session()
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=30), allow_redirects=False
                ) as resp:
                    if resp.status != 200:
                        return f"Error: could not download image (status {resp.status})"
                    image = await _read_response_limited(resp, 256 * 1024)
                emoji = await guild.create_custom_emoji(
                    name=clean, image=image, reason=_mod_reason(message)
                )
                return f"Created emoji :{emoji.name}: ({emoji.id}) in {guild.name}"
            except discord.Forbidden:
                return f"Error: Discord denied creating emoji in {guild.name}"
            except Exception as e:
                return f"Error creating emoji: {e}"
        if act == "delete":
            spec = str(emoji_id or name or "").strip().strip(":")
            eid = _parse_snowflake(spec)
            emoji = None
            if eid is not None:
                emoji = next((e for e in emojis if e.id == eid), None)
            if emoji is None:
                matches = [e for e in emojis if e.name.lower() == spec.lower()]
                emoji = matches[0] if len(matches) == 1 else None
            if emoji is None:
                return f"Error: emoji '{spec}' not found"
            try:
                label = f":{emoji.name}: ({emoji.id})"
                await emoji.delete(reason=_mod_reason(message))
                return f"Deleted emoji {label} from {guild.name}"
            except discord.Forbidden:
                return f"Error: Discord denied deleting :{emoji.name}:"
            except Exception as e:
                return f"Error deleting emoji: {e}"
        return "Error: action must be list, create, or delete"
