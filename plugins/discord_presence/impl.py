"""Tool implementations for the discord_presence plugin.

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

class ChangePresenceTool(Tool):
    """Change bot online status"""
    tool_name = 'change_presence'
    returns_result = False
    ends_turn = False


    def get_description(self):
        return "Set your online availability/status dot. Params: status (online/idle/dnd/invisible). Use set_activity for the visible custom status text."

    async def execute(self, message: Message, status: str = "online", **kwargs) -> str:
        valid = ["online", "idle", "dnd", "invisible"]
        if status not in valid:
            return f"Error: status must be one of {', '.join(valid)}"
        status_obj = getattr(Status, status, Status.online)
        activities = self.bot._build_activities()
        await self.bot.change_presence(
            status=status_obj,
            activity=activities[0] if activities else None,
        )
        # Silent: no DM, no channel echo, no LLM-visible text. The status
        # change is already visible on the bot's profile. Returning "" tells
        # the LLM not to send_message about it either.
        return ""

class SetActivityTool(Tool):
    """Set bot activity/custom status"""
    tool_name = 'set_activity'
    returns_result = True
    ends_turn = False


    def get_description(self):
        return (
            "Set visible activity or custom status. Call only when asked or "
            "after a real state change — not every turn. "
            "Params: type (playing/watching/listening/competing/custom), text, "
            "elapsed (optional). type='custom' for a plain status. text='' clears."
        )

    def _parse_elapsed(self, elapsed: str) -> int:
        total_ms = 0
        for match in re.finditer(r"(\d+)\s*(h|m|s|d)", elapsed.lower()):
            val = int(match.group(1))
            unit = match.group(2)
            if unit == "d":
                total_ms += val * 86400000
            elif unit == "h":
                total_ms += val * 3600000
            elif unit == "m":
                total_ms += val * 60000
            elif unit == "s":
                total_ms += val * 1000
        if total_ms == 0:
            try:
                total_ms = int(elapsed) * 60000
            except ValueError:
                total_ms = 0
        return total_ms

    async def execute(
        self,
        message: Message,
        type: str | None = None,
        text: str | None = None,
        elapsed: str | None = None,
        **kwargs,
    ) -> str:
        activity_type = (type or "custom").lower()

        if not text:
            if activity_type == "custom":
                self.bot._custom_status = None
            else:
                self.bot._current_game = None
            activities = self.bot._build_activities()
            await self.bot.change_presence(
                activity=activities[0] if activities else None,
            )
            # Silent: the cleared status is already visible on the profile.
            # No DM, no channel echo, no LLM-visible text.
            return ""

        if activity_type == "custom":
            self.bot._custom_status = discord.CustomActivity(name=text, state=text)
        elif activity_type in ("playing", "watching", "listening", "competing"):
            act_kwargs = {
                "type": getattr(discord.ActivityType, activity_type),
                "name": text,
            }
            if elapsed:
                ms = self._parse_elapsed(elapsed)
                if ms > 0:
                    start_time = datetime.now(timezone.utc) - timedelta(milliseconds=ms)
                    act_kwargs["timestamps"] = discord.ActivityTimestamps(
                        start=start_time
                    )
            self.bot._current_game = Activity(**act_kwargs)
        else:
            return "Error: type must be playing/watching/listening/competing/custom"

        activities = self.bot._build_activities()
        await self.bot.change_presence(
            activity=activities[0] if activities else None,
        )
        # Silent: the new status is already visible on the profile. No DM,
        # no channel echo, no LLM-visible text — the user can see it
        # themselves without the bot narrating the change.
        return ""

class SetNicknameTool(Tool):
    """Change the bot's own nickname in the server"""
    tool_name = 'set_nickname'
    returns_result = False
    ends_turn = False


    def get_description(self):
        return (
            "Change your nickname in this server (that becomes your name here). "
            "Params: nickname (required, 'reset' to remove)."
        )

    async def execute(
        self, message: Message, nickname: str | None = None, **kwargs
    ) -> str:
        if not nickname:
            return "Error: nickname is required"
        if not message.guild:
            return "Error: Cannot set nickname in DMs"
        try:
            nick = None if nickname.lower() == "reset" else nickname
            me = getattr(message.guild, "me", None)
            if me is None:
                return "Error: bot member is not cached"
            await me.edit(nick=nick)
            if nick:
                return (
                    f"Nickname changed to '{nickname}'. "
                    f"Your name in this server is now {nickname}."
                )
            return (
                "Nickname removed. Your name in this server is your account name again."
            )
        except discord.Forbidden:
            return "Error: I don't have permission to change my nickname here"
        except Exception as e:
            return f"Error setting nickname: {e}"

class ChangeAvatarTool(Tool):
    """Change the bot's own profile picture"""
    tool_name = 'change_avatar'
    returns_result = True
    ends_turn = False
    requires_admin = True


    def get_description(self):
        return (
            "Change your profile picture (admin only). Params: url (direct jpg/png/gif/webp). "
            "Discord rate-limits spam."
        )

    async def execute(self, message: Message, url: str | None = None, **kwargs) -> str:
        if not self.bot or not self.bot._is_admin(message.author.id):
            return "Error: Changing avatar is restricted to admins only."

        if not url:
            return "Error: url is required"

        if not _is_safe_url(url):
            return "Error: Cannot fetch from private/internal URLs"

        # Local cooldown fully removed — was previously env-driven
        # (AVATAR_COOLDOWN_SECONDS, default 0). Discord's own API rate limit
        # is the only throttle left; the bot will get a 429 from Discord if
        # it spams, which is fine.

        try:
            session = await _get_shared_session()
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=30), allow_redirects=False
            ) as resp:
                if resp.status != 200:
                    return f"Error: Could not download image (status {resp.status})"
                content_type = resp.headers.get("Content-Type", "")
                if content_type and not content_type.startswith("image/"):
                    return "Error: URL did not return an image"
                image_bytes = await _read_response_limited(resp, 10 * 1024 * 1024)

            await self.bot.user.edit(avatar=image_bytes)
            return "Avatar changed successfully"
        except discord.HTTPException as e:
            return f"Error changing avatar: {e}"
        except Exception as e:
            return f"Error: {e}"
