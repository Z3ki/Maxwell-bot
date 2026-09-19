"""Tool implementations for the discord_moderation plugin.

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

class KickMemberTool(Tool):
    tool_name = 'kick_member'
    returns_result = True
    ends_turn = False

    def get_description(self):
        return (
            "Kick a member from a server. Requires kick_members. "
            "Params: user_id (required), reason (optional), guild_id (optional)."
        )

    async def execute(
        self,
        message: Message,
        user_id: str | None = None,
        reason: str | None = None,
        guild_id: str | None = None,
        **kwargs,
    ) -> str:
        guild, error = await _resolve_guild(self.bot, message, guild_id)
        if error:
            return error
        missing = _missing_cap(guild, "kick_members", message)
        if missing:
            return missing
        member, error = await _resolve_member(guild, user_id)
        if error:
            return error
        blocked = _moderation_block(guild, _guild_me(guild), member, action="kick")
        if blocked:
            return blocked
        try:
            await member.kick(
                reason=_mod_reason(message) if not reason else str(reason)[:512]
            )
            return f"Kicked {member} ({member.id}) from {guild.name}"
        except discord.Forbidden:
            return f"Error: Discord denied kicking {member}; hierarchy or missing kick_members"
        except Exception as e:
            return f"Error kicking member: {e}"

class BanMemberTool(Tool):
    tool_name = 'ban_member'
    returns_result = True
    ends_turn = False

    def get_description(self):
        return (
            "Ban a member. Requires ban_members. Params: user_id (required), "
            "reason (optional), delete_message_seconds (optional 0-604800), "
            "guild_id (optional)."
        )

    async def execute(
        self,
        message: Message,
        user_id: str | None = None,
        reason: str | None = None,
        delete_message_seconds: str = "0",
        guild_id: str | None = None,
        **kwargs,
    ) -> str:
        guild, error = await _resolve_guild(self.bot, message, guild_id)
        if error:
            return error
        missing = _missing_cap(guild, "ban_members", message)
        if missing:
            return missing
        member, error = await _resolve_member(guild, user_id)
        if error:
            return error
        blocked = _moderation_block(guild, _guild_me(guild), member, action="ban")
        if blocked:
            return blocked
        try:
            seconds = max(
                0,
                min(
                    int(_parse_duration_seconds(delete_message_seconds, 0) or 0), 604800
                ),
            )
        except (TypeError, ValueError):
            seconds = 0
        try:
            await guild.ban(
                member,
                reason=_mod_reason(message) if not reason else str(reason)[:512],
                delete_message_seconds=seconds,
            )
            return f"Banned {member} ({member.id}) from {guild.name}"
        except TypeError:
            try:
                await guild.ban(
                    member,
                    reason=_mod_reason(message) if not reason else str(reason)[:512],
                    delete_message_days=min(7, seconds // 86400),
                )
                return f"Banned {member} ({member.id}) from {guild.name}"
            except Exception as e:
                return f"Error banning member: {e}"
        except discord.Forbidden:
            return f"Error: Discord denied banning {member}; hierarchy or missing ban_members"
        except Exception as e:
            return f"Error banning member: {e}"

class UnbanMemberTool(Tool):
    tool_name = 'unban_member'
    returns_result = True
    ends_turn = False

    def get_description(self):
        return (
            "Unban a user by id. Requires ban_members. "
            "Params: user_id (required), reason (optional), guild_id (optional)."
        )

    async def execute(
        self,
        message: Message,
        user_id: str | None = None,
        reason: str | None = None,
        guild_id: str | None = None,
        **kwargs,
    ) -> str:
        guild, error = await _resolve_guild(self.bot, message, guild_id)
        if error:
            return error
        missing = _missing_cap(guild, "ban_members", message)
        if missing:
            return missing
        uid = _parse_snowflake(user_id)
        if uid is None:
            return "Error: user_id is required"
        try:
            await guild.unban(
                discord.Object(id=uid),
                reason=_mod_reason(message) if not reason else str(reason)[:512],
            )
            return f"Unbanned {uid} in {guild.name}"
        except discord.NotFound:
            return f"Error: user {uid} is not banned in {guild.name}"
        except discord.Forbidden:
            return f"Error: Discord denied unbanning {uid} in {guild.name}"
        except Exception as e:
            return f"Error unbanning member: {e}"

class SoftbanMemberTool(Tool):
    """Ban then unban to kick and delete recent messages."""
    tool_name = 'softban_member'
    returns_result = True
    ends_turn = False


    def get_description(self):
        return (
            "Softban a member (ban then immediately unban) to kick them and delete "
            "recent messages without a lasting ban. Requires ban_members. "
            "Params: user_id, reason, delete_message_seconds (default 86400), guild_id."
        )

    async def execute(
        self,
        message: Message,
        user_id: str | None = None,
        reason: str | None = None,
        delete_message_seconds: str = "86400",
        guild_id: str | None = None,
        **kwargs,
    ) -> str:
        guild, error = await _resolve_guild(self.bot, message, guild_id)
        if error:
            return error
        missing = _missing_cap(guild, "ban_members", message)
        if missing:
            return missing
        member, error = await _resolve_member(guild, user_id)
        if error:
            return error
        blocked = _moderation_block(guild, _guild_me(guild), member, action="ban")
        if blocked:
            return blocked
        try:
            seconds = max(
                0,
                min(int(_parse_duration_seconds(delete_message_seconds, 86400) or 0), 604800),
            )
        except (TypeError, ValueError):
            seconds = 86400
        why = _mod_reason(message) if not reason else f"softban: {str(reason)[:480]}"
        try:
            try:
                await guild.ban(member, reason=why, delete_message_seconds=seconds)
            except TypeError:
                await guild.ban(
                    member, reason=why, delete_message_days=min(7, seconds // 86400)
                )
            await guild.unban(discord.Object(id=member.id), reason=why)
            return (
                f"Softbanned {member} ({member.id}) in {guild.name} "
                f"(deleted up to {seconds}s of messages, not banned)"
            )
        except discord.Forbidden:
            return f"Error: Discord denied softbanning {member}"
        except Exception as e:
            return f"Error softbanning member: {e}"

class ListBansTool(Tool):
    tool_name = 'list_bans'
    returns_result = True
    ends_turn = False

    def get_description(self):
        return (
            "List banned users in a server. Requires ban_members. "
            "Params: guild_id (optional), limit (optional, default 20)."
        )

    async def execute(
        self,
        message: Message,
        guild_id: str | None = None,
        limit: str = "20",
        **kwargs,
    ) -> str:
        guild, error = await _resolve_guild(self.bot, message, guild_id)
        if error:
            return error
        missing = _missing_cap(guild, "ban_members", message)
        if missing:
            return missing
        try:
            cap = max(1, min(int(limit or 20), 50))
        except (TypeError, ValueError):
            cap = 20
        rows = []
        try:
            async for entry in guild.bans(limit=cap):
                user = getattr(entry, "user", None) or entry
                why = getattr(entry, "reason", None) or "no reason"
                rows.append(
                    f"{getattr(user, 'name', user)} ({getattr(user, 'id', '?')}): {why}"
                )
        except discord.Forbidden:
            return f"Error: cannot list bans in {guild.name}"
        except Exception as e:
            return f"Error listing bans: {e}"
        if not rows:
            return f"No bans in {guild.name}"
        return f"Bans in {guild.name} ({len(rows)}):\n" + "\n".join(rows)

class TimeoutMemberTool(Tool):
    tool_name = 'timeout_member'
    returns_result = True
    ends_turn = False

    def get_description(self):
        return (
            "Timeout or untimeout a member. Requires moderate_members. "
            "Params: user_id (required), duration (e.g. 10m, 1h, 1d; 0/clear to remove), "
            "reason (optional), guild_id (optional)."
        )

    async def execute(
        self,
        message: Message,
        user_id: str | None = None,
        duration: str | None = None,
        reason: str | None = None,
        guild_id: str | None = None,
        **kwargs,
    ) -> str:
        guild, error = await _resolve_guild(self.bot, message, guild_id)
        if error:
            return error
        missing = _missing_cap(guild, "moderate_members", message)
        if missing:
            return missing
        member, error = await _resolve_member(guild, user_id)
        if error:
            return error
        blocked = _moderation_block(guild, _guild_me(guild), member, action="timeout")
        if blocked:
            return blocked
        seconds = _parse_duration_seconds(duration, None)
        if seconds is None:
            return "Error: duration like 10m, 1h, 2d, or 0/clear to remove"
        until = None
        if seconds > 0:
            seconds = max(60, min(seconds, 28 * 86400))
            until = datetime.now(timezone.utc) + timedelta(seconds=seconds)
        why = _mod_reason(message) if not reason else str(reason)[:512]
        try:
            if hasattr(member, "timeout"):
                await member.timeout(until, reason=why)
            else:
                await member.edit(timed_out_until=until, reason=why)
            if until is None:
                return f"Removed timeout from {member} in {guild.name}"
            return f"Timed out {member} in {guild.name} until {until.isoformat()}"
        except discord.Forbidden:
            return f"Error: Discord denied timing out {member}"
        except Exception as e:
            return f"Error timing out member: {e}"

class ListTimeoutsTool(Tool):
    """List members currently timed out."""
    tool_name = 'list_timeouts'
    returns_result = True
    ends_turn = False


    def get_description(self):
        return (
            "List members currently timed out in a server. Requires moderate_members. "
            "Params: guild_id (optional), limit (default 25)."
        )

    async def execute(
        self,
        message: Message,
        guild_id: str | None = None,
        limit: str = "25",
        **kwargs,
    ) -> str:
        guild, error = await _resolve_guild(self.bot, message, guild_id)
        if error:
            return error
        missing = _missing_cap(guild, "moderate_members", message)
        if missing:
            return missing
        try:
            cap = max(1, min(int(limit or 25), 80))
        except (TypeError, ValueError):
            cap = 25
        rows = []
        now = datetime.now(timezone.utc)
        for member in getattr(guild, "members", []) or []:
            until = getattr(member, "timed_out_until", None)
            if until is None:
                continue
            try:
                if until <= now:
                    continue
            except TypeError:
                pass
            until_s = until.isoformat() if hasattr(until, "isoformat") else str(until)
            rows.append(f"{member} ({member.id}) until {until_s}")
            if len(rows) >= cap:
                break
        if not rows:
            return f"No timed-out members in {guild.name}"
        return f"Timeouts in {guild.name} ({len(rows)}):\n" + "\n".join(rows)

class ManageRoleTool(Tool):
    tool_name = 'manage_role'
    returns_result = True
    ends_turn = False

    def get_description(self):
        return (
            "Create/edit/delete roles, reorder role hierarchy, or add/remove them on "
            "members. Requires manage_roles. Params: action "
            "(list|create|edit|delete|add|remove|move), guild_id (optional), name, "
            "role_id, user_id, color (hex), hoist, mentionable, permissions "
            "(comma perm names), confirm_name (required to delete), position "
            "(absolute hierarchy index; higher = higher in Server Settings > Roles), "
            "above (role name/id to place this role immediately above), below "
            "(role name/id to place this role immediately below). "
            "Use action=move with above=/below=/position= instead of asking anyone "
            "to drag roles in Server Settings."
        )

    async def execute(
        self,
        message: Message,
        action: str | None = None,
        guild_id: str | None = None,
        name: str | None = None,
        role_id: str | None = None,
        user_id: str | None = None,
        color: str | None = None,
        hoist: str | None = None,
        mentionable: str | None = None,
        permissions: str | None = None,
        confirm_name: str | None = None,
        position: str | None = None,
        above: str | None = None,
        below: str | None = None,
        **kwargs,
    ) -> str:
        guild, error = await _resolve_guild(self.bot, message, guild_id)
        if error:
            return error
        missing = _missing_cap(guild, "manage_roles", message)
        if missing:
            return missing
        me = _guild_me(guild)
        act = str(action or "list").strip().lower()
        why = _mod_reason(message)
        if act == "list":
            return _render_role_list(guild, limit=40)
        if act == "create":
            clean = _clean_discord_name(name)
            if not clean:
                return "Error: name is required to create a role"
            kwargs_create = {"name": clean, "reason": why}
            colour = _colour_from_text(color)
            if colour is not None:
                kwargs_create["colour"] = colour
            perms = _permissions_from_names(permissions)
            if perms is not None:
                kwargs_create["permissions"] = perms
            if hoist is not None:
                kwargs_create["hoist"] = parse_bool(hoist, False)
            if mentionable is not None:
                kwargs_create["mentionable"] = parse_bool(mentionable, False)
            try:
                role = await guild.create_role(**kwargs_create)
                return f"Created role {_role_label(role)} in {guild.name}"
            except discord.Forbidden:
                return f"Error: Discord denied creating a role in {guild.name}"
            except Exception as e:
                return f"Error creating role: {e}"
        role, error = _find_role(guild, role_id or name)
        if error:
            return error
        blocked = _role_blocked(me, role)
        if blocked and act != "list":
            return blocked
        kind, _value, place_err = None, None, ""
        if act in {"move", "reorder", "edit"}:
            kind, _value, place_err = _parse_role_placement(
                position, above, below
            )
            if place_err:
                return place_err
        placement = kind is not None
        if act in {"move", "reorder"}:
            return await _move_role_hierarchy(
                guild,
                me,
                role,
                position=position,
                above=above,
                below=below,
                reason=why,
            )
        if act == "edit":
            updates = {}
            if name:
                clean = _clean_discord_name(name)
                current = str(getattr(role, "name", "") or "")
                if clean and (role_id or clean != current):
                    updates["name"] = clean
            colour = _colour_from_text(color)
            if colour is not None:
                updates["colour"] = colour
            perms = _permissions_from_names(permissions)
            if perms is not None:
                updates["permissions"] = perms
            if hoist is not None:
                updates["hoist"] = parse_bool(hoist, False)
            if mentionable is not None:
                updates["mentionable"] = parse_bool(mentionable, False)
            if not updates and not placement:
                return (
                    "Error: provide a field to edit (name, color, hoist, "
                    "mentionable, permissions, position, above, or below)"
                )
            notes = []
            if updates:
                try:
                    await role.edit(**updates, reason=why)
                    notes.append(
                        f"Edited role {_role_label(role)}: "
                        + ", ".join(sorted(updates))
                    )
                except discord.Forbidden:
                    return f"Error: Discord denied editing {role.name}"
                except Exception as e:
                    return f"Error editing role: {e}"
            if placement:
                notes.append(
                    await _move_role_hierarchy(
                        guild,
                        me,
                        role,
                        position=position,
                        above=above,
                        below=below,
                        reason=why,
                    )
                )
            return "\n".join(notes)
        if act == "delete":
            actual = getattr(role, "name", "")
            if str(confirm_name or "") != actual:
                return f"Error: confirm_name must exactly match '{actual}'"
            try:
                label = _role_label(role)
                await role.delete(reason=why)
                return f"Deleted role {label} from {guild.name}"
            except discord.Forbidden:
                return f"Error: Discord denied deleting {actual}"
            except Exception as e:
                return f"Error deleting role: {e}"
        if act in {"add", "remove"}:
            member, error = await _resolve_member(guild, user_id)
            if error:
                return error
            blocked = _moderation_block(guild, me, member, action="role")
            if blocked:
                return blocked
            try:
                if act == "add":
                    await member.add_roles(role, reason=why)
                    return f"Added {role.name} to {member} in {guild.name}"
                await member.remove_roles(role, reason=why)
                return f"Removed {role.name} from {member} in {guild.name}"
            except discord.Forbidden:
                return f"Error: Discord denied changing roles on {member}"
            except Exception as e:
                return f"Error changing roles: {e}"
        return (
            "Error: action must be list, create, edit, delete, add, remove, or move"
        )

class VoiceModTool(Tool):
    tool_name = 'voice_mod'
    returns_result = True
    ends_turn = False

    def get_description(self):
        return (
            "Server-mute, deafen, move, or disconnect a member in voice. "
            "Params: action (mute|unmute|deafen|undeafen|move|disconnect), "
            "user_id (required), channel_id (for move), guild_id (optional)."
        )

    async def execute(
        self,
        message: Message,
        action: str | None = None,
        user_id: str | None = None,
        channel_id: str | None = None,
        guild_id: str | None = None,
        **kwargs,
    ) -> str:
        guild, error = await _resolve_guild(self.bot, message, guild_id)
        if error:
            return error
        act = str(action or "").strip().lower()
        cap = {
            "mute": "mute_members",
            "unmute": "mute_members",
            "deafen": "deafen_members",
            "undeafen": "deafen_members",
            "move": "move_members",
            "disconnect": "move_members",
        }.get(act)
        if not cap:
            return "Error: action must be mute, unmute, deafen, undeafen, move, or disconnect"
        missing = _missing_cap(guild, cap, message)
        if missing:
            return missing
        member, error = await _resolve_member(guild, user_id)
        if error:
            return error
        blocked = _moderation_block(guild, _guild_me(guild), member, action="voice")
        if blocked:
            return blocked
        why = _mod_reason(message)
        try:
            if act == "mute":
                await member.edit(mute=True, reason=why)
                return f"Server-muted {member}"
            if act == "unmute":
                await member.edit(mute=False, reason=why)
                return f"Unmuted {member}"
            if act == "deafen":
                await member.edit(deafen=True, reason=why)
                return f"Server-deafened {member}"
            if act == "undeafen":
                await member.edit(deafen=False, reason=why)
                return f"Undeafened {member}"
            if act == "disconnect":
                await member.edit(voice_channel=None, reason=why)
                return f"Disconnected {member} from voice"
            dest, error = await _get_guild_channel(self.bot, channel_id)
            if error:
                return error
            if not isinstance(dest, discord.VoiceChannel):
                return "Error: move requires a voice channel_id"
            await member.edit(voice_channel=dest, reason=why)
            return f"Moved {member} to {_channel_label(dest)}"
        except discord.Forbidden:
            return f"Error: Discord denied voice mod on {member}"
        except Exception as e:
            return f"Error in voice_mod: {e}"

class LockChannelTool(Tool):
    tool_name = 'lock_channel'
    returns_result = True
    ends_turn = False

    def get_description(self):
        return (
            "Lock or unlock a channel or category for @everyone (deny/allow send or connect). "
            "A category locks every child channel. Needs manage_channels or manage_roles. "
            "Params: channel_id (optional), unlock (optional bool)."
        )

    async def execute(
        self,
        message: Message,
        channel_id: str | None = None,
        unlock: str = "false",
        **kwargs,
    ) -> str:
        channel = getattr(message, "channel", None)
        if channel_id:
            channel, error = await _get_guild_channel(self.bot, channel_id)
            if error:
                return error
        guild = getattr(channel, "guild", None)
        if guild is None:
            return "Error: lock only works in servers"
        missing = _missing_cap(
            guild,
            "manage_channels",
            message,
            alt_caps=("manage_roles",),
            channel=channel,
        )
        if missing:
            return missing
        locked = not parse_bool(unlock, False)
        why = _mod_reason(message)
        targets = (
            list(getattr(channel, "channels", []) or [])
            if isinstance(channel, discord.CategoryChannel)
            else [channel]
        )
        if isinstance(channel, discord.CategoryChannel) and not targets:
            return f"Category {channel.name} has no channels to lock"
        ok = 0
        errors = []
        for ch in targets:
            err = await _lock_target(ch, locked, why)
            if err:
                errors.append(err)
            else:
                ok += 1
        state = "Locked" if locked else "Unlocked"
        if isinstance(channel, discord.CategoryChannel):
            out = f"{state} {ok}/{len(targets)} channels in category {channel.name}"
        else:
            out = f"{state} {_channel_label(channel)} for @everyone"
        if errors:
            out += " | " + "; ".join(errors[:3])
        return out

class LockdownTool(Tool):
    """Lock or unlock a category or the whole server."""
    tool_name = 'lockdown'
    returns_result = True
    ends_turn = False


    def get_description(self):
        return (
            "Lock (or unlock) every text/voice channel in a category, or the whole server, "
            "by denying @everyone send/connect. Needs manage_channels or manage_roles. "
            "Params: target (category_id, category_name, or 'server'), unlock, guild_id."
        )

    async def execute(
        self,
        message: Message,
        target: str | None = None,
        unlock: str = "false",
        guild_id: str | None = None,
        **kwargs,
    ) -> str:
        guild, error = await _resolve_guild(self.bot, message, guild_id)
        if error:
            return error
        if guild is None:
            return "Error: guild is unavailable"
        missing = _missing_cap(
            guild, "manage_channels", message, alt_caps=("manage_roles",)
        )
        if missing:
            return missing
        locked = not parse_bool(unlock, False)
        spec = str(target or "").strip()
        if not spec:
            return "Error: target is required (category id/name, or 'server')"
        channels = []
        label = spec
        if spec.lower() in {"server", "guild", "all", "*"}:
            channels = [
                ch
                for ch in (getattr(guild, "channels", []) or [])
                if not isinstance(ch, discord.CategoryChannel)
            ]
            label = f"server {guild.name}"
        else:
            category, err = _find_category(guild, spec if spec.isdigit() else None, spec)
            if err and not spec.isdigit():
                category, err = _find_category(guild, spec, None)
            if err:
                return err
            if category is None:
                return f"Error: category '{spec}' not found"
            channels = list(getattr(category, "channels", []) or [])
            label = f"category {category.name}"
        if not channels:
            return f"No channels to lock in {label}"
        why = _mod_reason(message)
        ok = []
        failed = []
        for ch in channels:
            err = await _lock_target(ch, locked, why)
            if err:
                failed.append(err)
            else:
                ok.append(_channel_label(ch))
        state = "Locked" if locked else "Unlocked"
        out = f"{state} {len(ok)}/{len(channels)} channels in {label}"
        if failed:
            out += " | " + "; ".join(failed[:5])
        return out

class SetChannelPermissionsTool(Tool):
    tool_name = 'set_channel_permissions'
    returns_result = True
    ends_turn = False

    def get_description(self):
        return (
            "Set or clear a channel permission overwrite for a role or member. "
            "Needs manage_roles. Params: channel_id (required), target (role/user id "
            "or 'everyone'), allow (comma perm=true/false/inherit), reset (bool)."
        )

    async def execute(
        self,
        message: Message,
        channel_id: str | None = None,
        target: str | None = None,
        allow: str | None = None,
        reset: str = "false",
        **kwargs,
    ) -> str:
        if not channel_id or not target:
            return "Error: channel_id and target are required"
        channel, error = await _get_guild_channel(self.bot, channel_id)
        if error:
            return error
        guild = channel.guild
        missing = _missing_cap(guild, "manage_roles", message)
        if missing:
            return missing
        spec = str(target).strip().lower()
        subject = None
        if spec in {"everyone", "@everyone", "default"}:
            subject = guild.default_role
        if subject is None:
            subject, _err = _find_role(guild, target)
        if subject is None:
            subject, error = await _resolve_member(guild, target)
            if error and subject is None:
                return f"Error: target '{target}' is not a role, member, or everyone"
        why = _mod_reason(message)
        try:
            if parse_bool(reset, False):
                await channel.set_permissions(subject, overwrite=None, reason=why)
                return f"Cleared overwrites for {target} on {_channel_label(channel)}"
            pairs = _parse_overwrite_pairs(allow)
            if not pairs:
                return "Error: provide allow like send_messages=false,view_channel=true"
            await channel.set_permissions(subject, reason=why, **pairs)
            return (
                f"Updated overwrites for {target} on {_channel_label(channel)}: {pairs}"
            )
        except discord.Forbidden:
            return (
                f"Error: Discord denied editing overwrites on {_channel_label(channel)}"
            )
        except Exception as e:
            return f"Error setting channel permissions: {e}"

class SetMemberNicknameTool(Tool):
    tool_name = 'set_member_nickname'
    returns_result = True
    ends_turn = False

    def get_description(self):
        return (
            "Change another member's nickname. Requires manage_nicknames. "
            "Params: user_id (required), nickname (required, 'reset' to clear), guild_id (optional)."
        )

    async def execute(
        self,
        message: Message,
        user_id: str | None = None,
        nickname: str | None = None,
        guild_id: str | None = None,
        **kwargs,
    ) -> str:
        if not nickname:
            return "Error: nickname is required"
        guild, error = await _resolve_guild(self.bot, message, guild_id)
        if error:
            return error
        missing = _missing_cap(guild, "manage_nicknames", message)
        if missing:
            return missing
        member, error = await _resolve_member(guild, user_id)
        if error:
            return error
        blocked = _moderation_block(guild, _guild_me(guild), member, action="nick")
        if blocked:
            return blocked
        nick = None if str(nickname).strip().lower() == "reset" else str(nickname)[:32]
        try:
            await member.edit(nick=nick, reason=_mod_reason(message))
            if nick:
                return f"Set {member}'s nickname to {nick}"
            return f"Cleared {member}'s nickname"
        except discord.Forbidden:
            return f"Error: Discord denied changing nickname for {member}"
        except Exception as e:
            return f"Error setting nickname: {e}"
