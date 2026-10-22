"""Tool implementations for the discord_messages plugin.

Moved out of the historical bot_tools.py monolith. Shared helpers live in
``tooling.helpers``.
"""
from __future__ import annotations
from generated_artifacts import generated_file

from types import SimpleNamespace

from tooling import helpers as _helpers
from tools import Tool

# Mechanical split: the original classes used the bot_tools module globals.
# Bind every helper/name here so execute() bodies keep working unchanged.
for _name in dir(_helpers):
    if _name.startswith("__"):
        continue
    globals().setdefault(_name, getattr(_helpers, _name))
del _name
from discord_account import application_client_id, bot_oauth_install_urls


class ReactTool(Tool):
    """A reaction can be the complete response, without a forced text reply."""

    tool_name = "react"
    returns_result = True
    ends_turn = False
    produces_visible_output = True

    def get_description(self):
        return (
            "React naturally to the current message, or message_id in this channel. "
            "Use Unicode, current-server/application emoji aliases, IDs, or custom markup. "
            "Animated emojis are supported; no Nitro subscription is needed. "
            "finish defaults true: the reaction is the entire response and ends the turn. "
            "Set finish=false only when a written answer or further work is also needed."
        )

    async def execute(
        self,
        message: Message,
        emoji: str | None = None,
        finish: bool = True,
        message_id: str | None = None,
        **kwargs,
    ) -> str:
        raw = str(emoji or "").strip()
        if not raw:
            return "Error: emoji parameter is required"
        from discord_expressions import channel_permissions, custom_emoji_id, resolve_custom_emoji

        guild = getattr(message, "guild", None)
        eid = custom_emoji_id(raw)
        alias = re.fullmatch(r":?([A-Za-z0-9_]{2,32}):?", raw)
        if eid is not None or alias:
            resolved = resolve_custom_emoji(self.bot, guild, raw)
            if resolved is None and eid is not None:
                return "Error: this emoji is not owned by this application or the current server"
            if resolved is None:
                return "Error: emoji name is unavailable here; use Unicode or list_expressions"
            raw = resolved
        perms = channel_permissions(message)
        if any(
            getattr(perms, p, None) is False
            for p in ("view_channel", "read_message_history")
        ):
            return (
                "Error: missing permissions to read/react in this channel; do not retry"
            )
        target = message
        try:
            if message_id and str(message_id) != str(getattr(message, "id", "")):
                target = await message.channel.fetch_message(int(message_id))
            if not callable(getattr(target, "add_reaction", None)):
                return "Error: cannot react to this message"
            existing = any(
                (custom_emoji_id(getattr(r, "emoji", "")) == custom_emoji_id(raw))
                if custom_emoji_id(raw) is not None
                else str(getattr(r, "emoji", "")) == raw
                for r in getattr(target, "reactions", ())
            )
            if getattr(perms, "add_reactions", None) is False and not existing:
                return "Error: Add Reactions is unavailable; do not retry with a different emoji"
            await target.add_reaction(raw)
        except (ValueError, discord.NotFound):
            return "Error: message or emoji is unavailable; do not retry"
        except discord.Forbidden:
            return "Error: missing permissions to react; do not retry"
        except discord.HTTPException:
            return "Error: Discord could not add the reaction"
        if parse_bool(finish, True):
            record = getattr(self.bot, "_record_request_outcome", None)
            if callable(record):
                await record(message, "delivered", reason="reaction_only")
            return f"__REACTION_SENT__ Reacted with {raw}"
        return f"Reacted with {raw}"


class ListExpressionsTool(Tool):
    tool_name = "list_expressions"
    returns_result = True
    side_effects = False

    def get_description(self):
        return "List current-server/application emojis and official Discord stickers, including animated formats. Optional query filters names. Other servers' assets are excluded."

    async def execute(self, message: Message, query: str = "", **kwargs) -> str:
        from discord_expressions import available_emojis, available_emoji_codes, available_stickers

        guild = getattr(message, "guild", None)
        needle = str(query or "").strip().lower()
        emojis = available_emojis(self.bot, guild)
        stickers = available_stickers(self.bot, guild)
        lines = [
            f"Emoji :{n}: {v} ({'animated' if v.startswith('<a:') else 'static'})"
            for n, v in sorted(emojis.items()) if needle in n
        ]
        lines += [
            f"Emoji {v} (application; use markup or ID)"
            for v in available_emoji_codes(self.bot, guild).values()
            if v not in emojis.values() and needle in v.lower()
        ]
        lines += [
            f"Sticker [STICKER ({n})] (id={getattr(s, 'id', '')}; "
            f"format={getattr(getattr(s, 'format', None), 'name', 'unknown')})"
            for n, s in sorted(stickers.items()) if needle in n
        ]
        return "\n".join(lines) or "No matching expressions available here."


_GIF_LIMIT = 8_000_000
_NEKOS_AGENT = "Maxwell (https://maxwell.z3ki.dev)"
_NEKOS_CATEGORIES = frozenset(
    {
        "angry", "baka", "bite", "bleh", "blowkiss", "blush", "bonk", "bored",
        "carry", "clap", "confused", "cry", "cuddle", "dance", "facepalm", "feed",
        "handhold", "handshake", "happy", "highfive", "hug", "kabedon", "kick",
        "kiss", "lappillow", "laugh", "lurk", "nod", "nom", "nope", "nya", "pat",
        "peck", "poke", "pout", "punch", "run", "salute", "shake", "shoot",
        "shocked", "shrug", "sip", "slap", "sleep", "smile", "smug", "spin",
        "stare", "tableflip", "teehee", "think", "thumbsup", "tickle", "wag",
        "wave", "wink", "yawn", "yeet",
    }
)
_GIF_ALIASES = {
    "sad": "cry",
    "crying": "cry",
    "tears": "cry",
    "lol": "laugh",
    "lmao": "laugh",
    "haha": "laugh",
    "laughing": "laugh",
    "hello": "wave",
    "hi": "wave",
    "hey": "wave",
    "bye": "wave",
    "goodbye": "wave",
    "waving": "wave",
    "yes": "thumbsup",
    "yeah": "thumbsup",
    "yep": "thumbsup",
    "ok": "thumbsup",
    "okay": "thumbsup",
    "thumbs up": "thumbsup",
    "thumbs-up": "thumbsup",
    "no": "nope",
    "nah": "nope",
    "mad": "angry",
    "rage": "angry",
    "tired": "sleep",
    "sleepy": "sleep",
    "wow": "shocked",
    "surprised": "shocked",
    "shock": "shocked",
    "idk": "shrug",
    "smh": "facepalm",
    "love": "hug",
    "hugs": "hug",
    "hugging": "hug",
    "kissing": "kiss",
    "dancing": "dance",
    "smiling": "smile",
    "grin": "smile",
    "blushing": "blush",
    "headpat": "pat",
    "head pat": "pat",
    "cuddling": "cuddle",
    "thinking": "think",
    "high five": "highfive",
    "high-five": "highfive",
}


def _reaction_category(query: str) -> str:
    parts = [
        part
        for part in "".join(ch.lower() if ch.isalnum() else " " for ch in query).split()
        if part
    ]
    if not parts or len(parts) > 3:
        return ""
    for candidate in ("".join(parts), "-".join(parts), " ".join(parts)):
        mapped = _GIF_ALIASES.get(
            candidate, candidate if candidate in _NEKOS_CATEGORIES else ""
        )
        if mapped in _NEKOS_CATEGORIES:
            return mapped
    return ""


def _gif_host(url: str) -> str:
    from urllib.parse import urlsplit

    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.username or parsed.password:
        return ""
    return (parsed.hostname or "").lower().rstrip(".")


def _allowed_gif_host(host: str) -> bool:
    return host in {"nekos.best", "giphy.com"} or host.endswith(
        (".nekos.best", ".giphy.com")
    )


async def _nekos_gif_url(session, category: str, index: int) -> str:
    if category not in _NEKOS_CATEGORIES:
        return ""
    async with session.get(
        f"https://nekos.best/api/v2/{category}",
        params={"amount": 10},
        headers={"User-Agent": _NEKOS_AGENT, "Accept": "application/json"},
        timeout=aiohttp.ClientTimeout(total=12),
        allow_redirects=False,
    ) as response:
        if response.status != 200:
            return ""
        payload = await response.json()
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list) or index >= len(results) or not isinstance(results[index], dict):
        return ""
    url = str(results[index].get("url") or "")
    path = url.split("?", 1)[0].lower()
    if not _allowed_gif_host(_gif_host(url)) or not path.endswith(".gif"):
        return ""
    return url


async def _giphy_gif_url(session, key: str, query: str, index: int) -> tuple[str, str]:
    async with session.get(
        "https://api.giphy.com/v1/gifs/search",
        params={"api_key": key, "q": query, "limit": 10, "rating": "pg-13"},
        timeout=aiohttp.ClientTimeout(total=12),
        allow_redirects=False,
    ) as response:
        if response.status != 200:
            return "", f"Error: GIF search returned HTTP {response.status}; do not retry in this turn"
        payload = await response.json()
    results = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(results, list) or index >= len(results) or not isinstance(results[index], dict):
        return "", ""
    images = results[index].get("images") or {}
    url = ""
    for name in ("downsized", "fixed_height", "original"):
        url = str((images.get(name) or {}).get("url") or "")
        if url:
            break
    if not _allowed_gif_host(_gif_host(url)):
        return "", ""
    return url, ""


async def _download_gif(session, url: str) -> bytes:
    if not _allowed_gif_host(_gif_host(url)):
        return b""
    async with session.get(
        url,
        headers={"User-Agent": _NEKOS_AGENT, "Accept": "image/gif"},
        timeout=aiohttp.ClientTimeout(total=12),
        allow_redirects=False,
    ) as response:
        if response.status != 200:
            return b""
        final = _gif_host(str(getattr(response, "url", url) or url))
        if final and not _allowed_gif_host(final):
            return b""
        content = getattr(response, "content", None)
        if content is not None and hasattr(content, "iter_chunked"):
            chunks = []
            total = 0
            async for chunk in content.iter_chunked(65536):
                total += len(chunk)
                if total > _GIF_LIMIT:
                    return b""
                chunks.append(chunk)
            data = b"".join(chunks)
        else:
            data = await response.read()
    if not isinstance(data, (bytes, bytearray)) or len(data) > _GIF_LIMIT:
        return b""
    data = bytes(data)
    if not data.startswith((b"GIF87a", b"GIF89a")):
        return b""
    return data


class SendGifTool(Tool):
    tool_name = "send_gif"
    returns_result = False
    ends_turn = True
    produces_visible_output = True

    def get_description(self):
        return (
            "Send one reaction or meme GIF in this chat, with no caption. "
            "Use it when a reaction GIF fits the moment (banter, hype, a joke landing) or when asked. "
            "query is a short search phrase. A reaction such as hug, laugh, or wave "
            "uses the keyless catalog. Other phrases use Giphy when it is configured. "
            "index optionally selects a different result (0-9). "
            "Success ends the turn; no extra written reply is required."
        )

    async def execute(
        self, message: Message, query: str = "", index: int = 0, **kwargs
    ) -> str:
        import io

        from discord_expressions import (
            cannot_send,
            channel_permissions,
            mark_send_unavailable,
        )

        key = str(
            getattr(getattr(self.bot, "config", None), "GIPHY_API_KEY", "") or ""
        ).strip()
        if cannot_send(message, self.bot):
            return "Error: cannot send here; use react or no_response instead"
        query = str(query or "").strip()
        if not query or len(query) > 50:
            return "Error: a GIF search phrase must be 1-50 characters"
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index <= 9:
            return "Error: index must be 0-9"
        try:
            session = await _get_shared_session()
            url = ""
            category = _reaction_category(query)
            if category:
                url = await _nekos_gif_url(session, category, index)
            notice = ""
            if not url and key:
                url, notice = await _giphy_gif_url(session, key, query, index)
            if notice:
                return notice
            if not url:
                return "Error: no matching GIF; try a different search phrase"
            perms = channel_permissions(message)
            file_ok = perms is None or getattr(perms, "attach_files", None) is not False
            data = await _download_gif(session, url) if file_ok else b""
        except (
            aiohttp.ClientError,
            asyncio.TimeoutError,
            ValueError,
            TypeError,
            AttributeError,
        ):
            # HTTP exceptions include the request URL (and API key). Never expose it.
            return "Error: GIF search failed; do not retry in this turn"
        if file_ok and not data:
            return "Error: GIF could not be downloaded; do not retry in this turn"
        try:
            self._signal_streaming(message)
            args = {"allowed_mentions": discord.AllowedMentions.none()}
            if data:
                args["file"] = discord.File(io.BytesIO(data), filename="gif.gif")
            else:
                args["content"] = url
            sent = await message.channel.send(**args)
        except discord.Forbidden:
            mark_send_unavailable(self.bot, message)
            return "Error: cannot send here; use react or no_response instead"
        except discord.HTTPException:
            return "Error: Discord could not send the GIF; do not retry in this turn"
        record = getattr(self.bot, "_record_delivery", None)
        if callable(record):
            await record(message, sent)
        remember = getattr(self.bot, "_remember_sent_message", None)
        if callable(remember):
            try:
                await remember(message, sent, f"[GIF] {url}", message.channel)
            except Exception as exc:
                logger.warning(
                    "Could not remember delivered GIF (%s)", type(exc).__name__
                )
        return "__GIF_SENT__"

class EditMessageTool(Tool):
    """Edit one of the bot's own messages"""
    tool_name = 'edit_message'
    returns_result = True
    ends_turn = False


    def get_description(self):
        return (
            "Edit a Maxwell response attributable to this request, or a Maxwell "
            "response a server moderator is authorized to manage. Legacy messages "
            "with unknown ownership require manage_messages. Only the current "
            "channel is available. Params: message_id (required), content (required)."
        )

    async def execute(
        self,
        message: Message,
        message_id: str | None = None,
        content: str | None = None,
        **kwargs,
    ) -> str:
        if not message_id or content is None:
            return "Error: message_id and content are required"
        if len(str(content)) > 2000:
            return "Error: message content exceeds Discord's 2000-character limit"
        requested_channel = kwargs.get("channel_id")
        channel = getattr(message, "channel", None)
        if requested_channel and str(requested_channel) != str(getattr(channel, "id", "")):
            return "Error: edit_message is limited to the current channel"
        try:
            msg = await channel.fetch_message(int(str(message_id).strip()))
            if msg.author.id != self.bot.user.id:
                return "Error: I can only edit my own messages"
            requester_id = str(getattr(getattr(message, "author", None), "id", "") or "")
            current_channel_id = str(getattr(channel, "id", "") or "")
            journal = getattr(self.bot, "_request_journal", None)
            owner = await journal.submit(journal.find_response, message_id) if journal is not None else None
            attributable = bool(
                owner
                and str(owner.get("author_id") or "") == requester_id
                and str(owner.get("channel_id") or "") == current_channel_id
            )
            guild = getattr(channel, "guild", None)
            moderator = False
            if guild is not None:
                moderator = not bool(
                    _missing_cap(
                        guild,
                        "manage_messages",
                        message,
                        channel=channel,
                    )
                )
            if not attributable and not moderator:
                return "Error: you may only edit your own tracked Maxwell response"
            await msg.edit(
                content=str(content),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            logger.info(
                "moderation action=edit_message requester=%s guild=%s channel=%s target=%s ownership=%s",
                requester_id,
                getattr(guild, "id", ""),
                current_channel_id,
                str(message_id),
                "request" if attributable else "moderator",
            )
            return f"Message {message_id} edited successfully"
        except discord.NotFound:
            return f"Error: Message {message_id} not found"
        except discord.Forbidden:
            return "Error: I don't have permission to edit that message"
        except Exception as e:
            return f"Error editing message: {e}"

class DeleteMessageTool(Tool):
    """Delete a message. Own messages always; others need manage_messages."""
    tool_name = 'delete_message'
    returns_result = True
    ends_turn = False


    def get_description(self):
        return (
            "Delete a message. Maxwell responses require request ownership or "
            "manage_messages. Another user's message requires manage_messages "
            "for both you and Maxwell in the target channel. Targets must be in "
            "this server. Params: message_id (required), channel_id (optional)."
        )

    async def execute(
        self,
        message: Message,
        message_id: str | None = None,
        channel_id: str | None = None,
        **kwargs,
    ) -> str:
        if not message_id:
            return "Error: message_id is required"
        channel = getattr(message, "channel", None)
        if channel_id:
            channel, error = await _get_guild_channel(
                self.bot,
                channel_id,
                expected_guild_id=getattr(getattr(message, "guild", None), "id", None),
            )
            if error:
                return error
        request_guild = getattr(message, "guild", None)
        target_guild = getattr(channel, "guild", None)
        if request_guild is None or target_guild is None:
            return "Error: deletion is limited to a server channel in this request's server"
        if str(getattr(request_guild, "id", "")) != str(getattr(target_guild, "id", "")):
            return "Error: target channel is outside this server"
        if channel is None or not hasattr(channel, "fetch_message"):
            return "Error: channel is unavailable"
        try:
            msg = await channel.fetch_message(int(str(message_id).strip()))
            mine = self.bot.user and msg.author.id == self.bot.user.id
            if mine:
                journal = getattr(self.bot, "_request_journal", None)
                owner = await journal.submit(journal.find_response, message_id) if journal is not None else None
                requester_id = str(getattr(getattr(message, "author", None), "id", "") or "")
                channel_matches = str(owner.get("channel_id") or "") == str(
                    getattr(channel, "id", "") or ""
                ) if owner else False
                owns_response = bool(
                    owner
                    and channel_matches
                    and str(owner.get("author_id") or "") == requester_id
                )
                if not owns_response:
                    missing = _missing_cap(
                        target_guild, "manage_messages", message, channel=channel
                    )
                    if missing:
                        return "Error: response ownership is unknown; " + missing
            else:
                missing = _missing_cap(
                    target_guild, "manage_messages", message, channel=channel
                )
                if missing:
                    return missing
            await msg.delete()
            logger.info(
                "moderation action=delete_message requester=%s guild=%s channel=%s target=%s ownership=%s",
                str(getattr(getattr(message, "author", None), "id", "") or ""),
                getattr(target_guild, "id", ""),
                getattr(channel, "id", ""),
                str(message_id),
                "request" if mine and owns_response else ("moderator" if mine else "authorized_other"),
            )
            who = "my" if mine else "that"
            return f"Deleted {who} message {message_id}"
        except discord.NotFound:
            return f"Error: Message {message_id} not found"
        except discord.Forbidden:
            return "Error: I don't have permission to delete that message"
        except Exception as e:
            return f"Error deleting message: {e}"

class CreatePollTool(Tool):
    """Create a poll in the channel"""
    tool_name = 'create_poll'
    returns_result = True
    ends_turn = False
    produces_visible_output = True


    def get_description(self):
        return (
            "Post a native Discord poll in this channel. Use it when a group is choosing "
            "or voting (where to play, which option, rate this), or when someone asks for one. "
            "Params: question (required), options (required, 2-10 answers as a list), "
            "duration_hours (optional, default 24), multiple (optional, allow several picks)."
        )

    async def execute(
        self,
        message: Message,
        question: str | None = None,
        options: Any = None,
        duration_hours: Any = 24,
        multiple: Any = False,
        **kwargs,
    ) -> str:
        if not question or not options:
            return "Error: question and options are required"
        # A list lets an answer contain a comma. The string form is the old
        # contract and still turns up from models that learned it.
        raw = options if isinstance(options, list) else str(options).split(",")
        option_list = [str(o).strip() for o in raw if str(o).strip()]
        if len(option_list) < 2:
            return "Error: Need at least 2 options for a poll"
        if len(option_list) > 10:
            return "Error: Maximum 10 options allowed"
        too_long = [o for o in option_list if len(o) > 55]
        if too_long:
            return f"Error: poll answers are limited to 55 characters; shorten {too_long[0][:60]!r}"
        question = str(question).strip()
        if len(question) > 300:
            return "Error: the poll question is limited to 300 characters"
        try:
            hours = int(float(duration_hours))
        except (TypeError, ValueError):
            return "Error: duration_hours must be a number"
        if hours < 1 or hours > 168:
            return "Error: duration_hours must be between 1 and 168"
        try:
            poll = discord.Poll(
                question=question,
                duration=timedelta(hours=hours),
                multiple=parse_bool(multiple, False),
            )
            for opt in option_list:
                poll.add_answer(text=opt)

            # Step aside for the live progress message before posting
            # the poll. The poll itself is the user-visible action.
            self._signal_streaming(message)
            sent = await message.channel.send(poll=poll)
        except Exception as e:
            return f"Error creating poll: {e}"
        record = getattr(self.bot, "_record_delivery", None)
        if callable(record):
            await record(message, sent)
        remember = getattr(self.bot, "_remember_sent_message", None)
        if callable(remember):
            # Discord stores a poll with no content, so the gateway copy never
            # reaches memory. Without this Maxwell forgets he asked.
            try:
                await remember(
                    message, sent, f"[poll] {question} — {' / '.join(option_list)}", message.channel
                )
            except Exception as exc:
                logger.warning("Could not remember poll (%s)", type(exc).__name__)
        return f"Poll created: '{question}' with options: {', '.join(option_list)}"

class ForwardMessageTool(Tool):
    """Forward a message to another channel"""
    tool_name = 'forward_message'
    returns_result = True
    ends_turn = False


    def get_description(self):
        return "Forward a message to another channel. Params: message_id (required), channel_id (required)."

    async def execute(
        self,
        message: Message,
        message_id: str | None = None,
        channel_id: str | None = None,
        **kwargs,
    ) -> str:
        if not message_id or not channel_id:
            return "Error: message_id and channel_id are required"
        if _is_private_chat(message):
            return "Error: forwarding to another channel is not available in DMs"
        try:
            dest = self.bot.get_channel(int(channel_id))
            if not dest:
                dest = await self.bot.fetch_channel(int(channel_id))
            if not dest:
                return f"Error: Channel {channel_id} not found"

            orig = await message.channel.fetch_message(int(message_id))
            if not orig:
                return f"Error: Message {message_id} not found"
            src_guild = getattr(message.channel, "guild", None)
            dest_guild = getattr(dest, "guild", None)
            if not src_guild or not dest_guild or str(
                getattr(src_guild, "id", "")
            ) != str(getattr(dest_guild, "id", "")):
                return "Error: refusing to forward across servers"

            member = _resolve_requester_member(dest_guild, message)
            bot_member = _guild_me(dest_guild)
            if member is None or bot_member is None:
                return "Error: could not verify channel permissions"
            user_perms = dest.permissions_for(member)
            bot_perms = dest.permissions_for(bot_member)
            if not getattr(user_perms, "view_channel", False) or not getattr(
                user_perms, "send_messages", False
            ):
                return "Error: you cannot send messages in the destination channel"
            if not getattr(bot_perms, "view_channel", False) or not getattr(
                bot_perms, "send_messages", False
            ):
                return "Error: I cannot send messages in the destination channel"

            await orig.forward(dest)
            channel_name = getattr(dest, "name", channel_id)
            guild_name = (
                getattr(dest.guild, "name", "DM") if hasattr(dest, "guild") else "DM"
            )
            return f"Forwarded message {message_id} to #{channel_name} in {guild_name}"
        except discord.NotFound:
            return "Error: Message or channel not found"
        except discord.Forbidden:
            return "Error: I don't have permission to forward messages"
        except Exception as e:
            return f"Error forwarding message: {e}"

class TypingTool(Tool):
    """Trigger typing indicator in the channel"""
    tool_name = 'typing'
    returns_result = False
    ends_turn = False


    def get_description(self):
        return "Trigger typing indicator. No params."

    async def execute(self, message: Message, **kwargs) -> str:
        try:
            async with message.channel.typing():
                pass
            return "Triggered typing indicator"
        except Exception as e:
            return f"Error triggering typing: {e}"

class SendMessageTool(Tool):
    """Send a reply to the current message with Discord markdown formatting."""
    tool_name = 'send_message'
    returns_result = False
    ends_turn = True
    produces_visible_output = True


    def get_description(self):
        return (
            "Send a message to the current chat. reply defaults to true. Usually send one complete message, "
            "but you may emit 2-3 send_message calls in the same model response when a short conversational burst reads more naturally. "
            "The first delivered message in a turn may quote-reply. Later calls in that turn, including a later model round, are posted in the channel with no quote-reply. "
            "Do not split a normal informational answer just to create more messages. "
            "Content supports Discord markdown: **bold**, *italic*, `code`, ```code blocks```, > quotes, bullet lists. "
            "Current-server/application emojis support animation without Nitro. "
            "Use [STICKER (name)] or [STICKER (id)] for a listed current-server/official sticker; PNG, APNG, GIF and Lottie are supported as native stickers. "
            "Params: content (required), reply (optional bool, default true — Discord "
            "quote-reply is on; pass false only for a standalone line with no quote), "
            "reply_to (optional short quote or who said it, like nah or alice — not an id). "
            "channel_id/user_id to another channel or server is not available from DMs. "
            "From a server, that is admin-only — everyone else must reply in this chat."
        )

    @staticmethod
    def _open_fence(text: str) -> str | None:
        """The fence marker if *text* ends inside a fenced code block."""
        opener = None
        for line in text.splitlines():
            stripped = line.lstrip()
            if not stripped.startswith("```"):
                continue
            if opener is None:
                opener = stripped.rstrip()
            elif not stripped.strip("`").strip():
                # A closing fence has no info string.
                opener = None
        return opener

    @classmethod
    def _chunks(cls, text: str, limit: int = 1900) -> list[str]:
        # Discord hard-fails over 2000 chars. Keep each chunk inside the limit
        # and, when a split lands inside a fence, close it and reopen the same
        # fence on the next message so the code stays readable.
        chunks: list[str] = []
        remaining = text
        prefix = ""
        while remaining:
            room = max(1, limit - len(prefix))
            if len(remaining) + len(prefix) <= limit:
                chunks.append(prefix + remaining)
                break
            # Reserve room for a closing fence only when the cut will land
            # inside one; otherwise use the full limit.
            probe = remaining[:room]
            usable = (
                max(1, room - 8)
                if cls._open_fence(prefix + probe) is not None
                else room
            )
            cut = remaining.rfind("\n", 0, usable)
            if cut < usable // 2:
                cut = usable
            piece = remaining[:cut].rstrip()
            opener = cls._open_fence(prefix + piece)
            if opener is not None:
                run = len(opener) - len(opener.lstrip("`"))
                piece += "\n" + "`" * max(3, run)
                next_prefix = opener + "\n"
            else:
                next_prefix = ""
            chunks.append(prefix + piece)
            remaining = remaining[cut:].lstrip("\n")
            prefix = next_prefix
        return chunks or [""]

    async def execute(
        self,
        message: Message,
        content: str | None = None,
        reply: bool = True,
        reply_to: str | None = None,
        channel_id: str | None = None,
        user_id: str | None = None,
        **kwargs,
    ) -> str:
        text = str(content or "").strip()
        if not text:
            return "Error: content is required"
        sent_any = False
        sent_chunks: list[str] = []
        try:
            target_channel = getattr(message, "channel", None)
            raw_dest = (
                channel_id
                if channel_id is not None
                else (user_id if user_id is not None else kwargs.get("recipient_id"))
            )
            target_dest = str(raw_dest or "").strip()
            dest_id = _parse_snowflake(target_dest) if target_dest else None
            if target_dest and not dest_id:
                return "Error: invalid destination ID"
            cross_chat = bool(
                dest_id and not _destination_is_current_chat(message, dest_id)
            )
            if cross_chat and _is_private_chat(message):
                return (
                    "Error: sending to another channel or server is not "
                    "available in DMs. Reply in this chat instead."
                )
            if cross_chat and getattr(message, "user_install", False) and str(
                getattr(message, "response_visibility", "") or ""
            ) == "private":
                return (
                    "Error: sending to another channel or server is not "
                    "available for a private reply. Reply in this chat instead."
                )
            if cross_chat and not _caller_is_admin(self.bot, message):
                return (
                    "Error: sending to another channel or DM is restricted "
                    "to admins. Reply in this chat instead."
                )
            if cross_chat:
                if self.bot is None:
                    return "Error: cannot resolve the requested destination"
                ch = self.bot.get_channel(dest_id)
                if not ch and hasattr(self.bot, "fetch_channel"):
                    try:
                        ch = await self.bot.fetch_channel(dest_id)
                    except Exception:
                        ch = None
                if not ch:
                    usr = self.bot.get_user(dest_id)
                    if not usr and hasattr(self.bot, "fetch_user"):
                        try:
                            usr = await self.bot.fetch_user(dest_id)
                        except Exception:
                            usr = None
                    if usr:
                        try:
                            ch = usr.dm_channel or await usr.create_dm()
                        except Exception:
                            ch = None
                if ch is None or not callable(getattr(ch, "send", None)):
                    return "Error: cannot resolve a sendable channel or DM for the requested destination"
                target_channel = ch
                reply = False

            from discord_expressions import cannot_send, mark_send_unavailable
            if target_channel == getattr(message, "channel", None) and cannot_send(message, self.bot):
                return "Error: cannot send here; use react or no_response instead"
            guild = getattr(target_channel, "guild", None)
            stickers = []
            if self.bot and hasattr(self.bot, "_render_custom_emojis"):
                text = self.bot._render_custom_emojis(text, guild)
            if self.bot and hasattr(self.bot, "_extract_stickers_from_text"):
                text, stickers = self.bot._extract_stickers_from_text(text, guild)

            if stickers and getattr(target_channel, "_session", None) is not None:
                return "Error: Discord webhooks cannot send native stickers; use send_gif or Unicode instead"
            rejected = (getattr(self.bot, "_rejected_native_stickers", None) or {}).get(str(getattr(message, "id", "")), set())
            if any(getattr(sticker, "id", None) in rejected for sticker in stickers):
                return "Error: Discord already refused this native sticker for the bot; use a GIF or Unicode instead"

            chunks = self._chunks(text)
            if not chunks and stickers:
                chunks = [""]
            target = None
            if reply and target_channel == getattr(message, "channel", None):
                target = await resolve_send_reply_target(
                    message,
                    reply=reply,
                    reply_to=reply_to
                    if reply_to is not None
                    else kwargs.get("reply_to"),
                    bot=self.bot,
                )
            use_reply = target is not None
            reply_to_message = target if target is not None else message
            # If reply_to_message is a mock/SimpleNamespace without .reply, don't attempt direct .reply()
            if not callable(getattr(reply_to_message, "reply", None)):
                use_reply = False
            send_fn = (
                getattr(self.bot, "_send_with_slowmode", None) if self.bot else None
            )
            async with _tool_reply_typing(self.bot, message, text):
                for i, chunk in enumerate(chunks):
                    chunk_stickers = stickers if i == 0 else None
                    try:
                        mark_effect = getattr(self.bot, "_mark_request_effect", None)
                        if callable(mark_effect):
                            await mark_effect(message)
                        # Only pass stickers to Discord; drop all other tool kwargs (reasoning, channel_id, etc.)
                        # to avoid "multiple values for keyword argument 'content'" and "unexpected keyword"
                        extra = {}
                        if chunk_stickers:
                            extra["stickers"] = chunk_stickers
                        extra.pop("content", None)
                        extra.pop("file", None)
                        if callable(send_fn):
                            sent = await send_fn(
                                target_channel,
                                chunk,
                                reply_to=(
                                    reply_to_message if (i == 0 and use_reply) else None
                                ),
                                **extra,
                            )
                            if sent is None:
                                if not sent_any:
                                    mark_send_unavailable(self.bot, message)
                                    return "Error: cannot send here; use react or no_response instead"
                                record = getattr(self.bot, "_record_request_outcome", None)
                                if callable(record):
                                    await record(message, "delivered", reason="partial_delivery")
                                return "__MESSAGE_SENT__\n" + "\n".join(sent_chunks)
                        elif i == 0 and use_reply:
                            try:
                                sent = await reply_to_message.reply(chunk, **extra)
                            except (discord.NotFound, discord.HTTPException) as exc:
                                code = getattr(exc, "code", None)
                                parent_gone = isinstance(
                                    exc, discord.NotFound
                                ) or code in {
                                    10008,
                                    50035,
                                }
                                if (
                                    code == 50035
                                    and "message_reference" not in str(exc).lower()
                                ):
                                    raise
                                if not parent_gone:
                                    raise
                                sent = await target_channel.send(chunk, **extra)
                        else:
                            sent = await target_channel.send(chunk, **extra)
                        sent_any = True
                        sent_chunks.append(chunk)
                        record_delivery = getattr(self.bot, "_record_delivery", None)
                        if callable(record_delivery):
                            await record_delivery(message, sent)
                        remember = getattr(self.bot, "_remember_sent_message", None)
                        if callable(remember):
                            try:
                                await remember(message, sent, chunk, target_channel)
                            except Exception as exc:
                                logger.warning("Could not record delivered message %s (%s)", getattr(sent, "id", "?"), type(exc).__name__)
                    except Exception as exc:
                        if sent_any:
                            logger.warning(
                                "Partial send for message %s (%s)",
                                getattr(message, "id", "?"),
                                type(exc).__name__,
                            )
                            record = getattr(self.bot, "_record_request_outcome", None)
                            if callable(record):
                                with contextlib.suppress(Exception):
                                    await record(message, "delivered", reason="partial_delivery")
                            return "__MESSAGE_SENT__\n" + "\n".join(sent_chunks)
                        raise
                    if len(chunks) > 1:
                        await asyncio.sleep(0.2)
            # Return the marker followed by the actual sent content. The
            # content is what the bot said, so recalling it in memory is
            # correct. We intentionally do NOT include leakable debug prose
            # like "Sent N chars in M chunk(s)": that prose reads as natural
            # language and the model echoed it into visible replies
            # ("20 chars in 1 chunk(s)" appeared in chat). Downstream detects
            # send_message via " __MESSAGE_SENT__" in the result string.
            return f"__MESSAGE_SENT__\n{text}"
        except discord.Forbidden:
            mark_send_unavailable(self.bot, message)
            return "Error: cannot send here; use react or no_response instead"
        except discord.HTTPException as exc:
            if stickers and (exc.code == 50081 or (exc.code == 50035 and "sticker" in str(exc).lower())):
                if self.bot is not None:
                    rejected = getattr(self.bot, "_rejected_native_stickers", None)
                    if rejected is None:
                        rejected = self.bot._rejected_native_stickers = {}
                    if len(rejected) >= 1000:
                        rejected.clear()
                    rejected.setdefault(str(getattr(message, "id", "")), set()).update(getattr(sticker, "id", None) for sticker in stickers)
                return "Error: Discord refused this native sticker for the bot; use a GIF or Unicode instead"
            return f"Error sending message: {type(exc).__name__}"
        except Exception as e:
            if sent_any:
                return "__MESSAGE_SENT__\n" + "\n".join(sent_chunks)
            return f"Error sending message: {e}"

class SendFileTool(Tool):
    """Create and send an arbitrary file attachment, or send an existing file from disk."""
    tool_name = 'send_file'
    returns_result = True
    ends_turn = False
    produces_visible_output = True


    MAX_SIZE = 25 * 1024 * 1024

    def get_description(self):
        return (
            "Create or send a file attachment. Params: filename + content "
            "(inline), or path (a generated file, a /workspace file in the live sandbox, or a "
            "/storage file, which works even after the sandbox was wiped). "
            "encoding=text|base64 (prefer base64 for code/HTML). "
            "A path is not delivery — this tool attaches the file."
        )

    async def execute(
        self,
        message: Message,
        filename: str | None = None,
        content: str | None = None,
        encoding: str = "text",
        path: str | None = None,
        **kwargs,
    ) -> str:
        if path:
            artifact = generated_file(path)
            if artifact is not None:
                blob, generated_name = artifact
                if len(blob) > self.MAX_SIZE:
                    return "Error: generated attachment exceeds the upload size limit"
                safe_name = _safe_attachment_filename(filename or generated_name, default="file")
                return await self._send_blob(message, blob, safe_name)
            shell_tool = (getattr(self.bot, "tools", None) or {}).get("shell")
            reader = getattr(shell_tool, "read_workspace_file", None)
            if not callable(reader):
                return "Error: this request has no active private shell workspace"
            blob, workspace_name, error = await reader(
                message, path, max_size=self.MAX_SIZE
            )
            if blob is None or workspace_name is None:
                return f"Error: cannot read a file from this request's workspace ({error or 'unavailable'})"
            safe_name = _safe_attachment_filename(filename or workspace_name, default="file")
            return await self._send_blob(message, blob, safe_name)

        # Inline files belong to this request and never touch a shared host path.
        if not filename or not str(filename).strip():
            return "Error: filename is required"
        if content is None:
            return "Error: content is required"

        safe_name = _safe_attachment_filename(filename, default="file")
        if not safe_name or safe_name in {".", ".."}:
            return "Error: invalid filename"

        mode = str(encoding or "text").strip().lower()
        try:
            if mode in {"base64", "b64"}:
                # Standard base64 output is line-wrapped (CLI, MIME, many
                # encoders). Whitespace is not part of the payload.
                encoded = "".join(str(content).split())
                if len(encoded) > ((self.MAX_SIZE + 2) // 3) * 4:
                    return "Error: file is too large"
                blob = base64.b64decode(encoded, validate=True)
            elif mode in {"text", "utf8", "utf-8"}:
                blob = str(content).encode("utf-8")
            else:
                return "Error: encoding must be text or base64"
        except Exception as exc:
            return f"Error: could not decode file content ({type(exc).__name__})"

        return await self._send_blob(message, blob, safe_name)

    async def _send_blob(self, message: Message, blob: bytes, safe_name: str) -> str:
        if len(blob) > self.MAX_SIZE:
            return f"Error: file is too large (max {self.MAX_SIZE // 1024 // 1024} MB)"

        import mimetypes
        mimetypes.add_type("audio/mpeg", ".mp3")
        mimetypes.add_type("audio/wav", ".wav")
        mimetypes.add_type("audio/ogg", ".ogg")
        file = File(BytesIO(blob), filename=safe_name)
        sent, error = await deliver_attachment(message, file, label="file")
        if error:
            return error
        record_delivery = getattr(self.bot, "_record_delivery", None)
        if callable(record_delivery) and sent is not None:
            await record_delivery(message, sent)

        # Every piece of media gets its URL attached: the sent Discord
        # attachment carries a CDN URL the model can curl/pull/reuse.
        file_url = ""
        if sent is not None and getattr(sent, "attachments", None):
            file_url = sent.attachments[0].url
        result = f"__FILE_SENT__ Sent file: {safe_name} ({len(blob)} bytes)"
        if file_url:
            result += f"\nFile URL: {file_url}"
        return result

class PinMessageTool(Tool):
    tool_name = 'pin_message'
    returns_result = True
    ends_turn = False

    def get_description(self):
        return (
            "Pin or unpin a message. Needs pin_messages or manage_messages. "
            "Params: message_id (required), channel_id (optional), unpin (optional bool)."
        )

    async def execute(
        self,
        message: Message,
        message_id: str | None = None,
        channel_id: str | None = None,
        unpin: str = "false",
        **kwargs,
    ) -> str:
        if not message_id:
            return "Error: message_id is required"
        channel = getattr(message, "channel", None)
        if channel_id:
            channel, error = await _get_guild_channel(
                self.bot,
                channel_id,
                expected_guild_id=getattr(getattr(message, "guild", None), "id", None),
            )
            if error:
                return error
        guild = getattr(channel, "guild", None)
        if guild is None:
            return "Error: pin only works in servers"
        if str(getattr(guild, "id", "")) != str(
            getattr(getattr(message, "guild", None), "id", "")
        ):
            return "Error: target channel is outside this server"
        missing = _missing_cap(
            guild,
            "pin_messages",
            message,
            alt_caps=("manage_messages",),
            channel=channel,
        )
        if missing:
            return missing
        try:
            msg = await channel.fetch_message(int(str(message_id).strip()))
            if parse_bool(unpin, False):
                await msg.unpin(reason=_mod_reason(message))
                logger.info(
                    "moderation action=unpin_message requester=%s guild=%s channel=%s target=%s",
                    str(getattr(getattr(message, "author", None), "id", "") or ""),
                    getattr(guild, "id", ""),
                    getattr(channel, "id", ""),
                    str(message_id),
                )
                return f"Unpinned message {message_id}"
            await msg.pin(reason=_mod_reason(message))
            logger.info(
                "moderation action=pin_message requester=%s guild=%s channel=%s target=%s",
                str(getattr(getattr(message, "author", None), "id", "") or ""),
                getattr(guild, "id", ""),
                getattr(channel, "id", ""),
                str(message_id),
            )
            return f"Pinned message {message_id}"
        except discord.NotFound:
            return f"Error: message {message_id} not found"
        except discord.Forbidden:
            return "Error: Discord denied pinning that message"
        except Exception as e:
            return f"Error pinning message: {e}"

class _PurgeConfirmationView(discord.ui.View):
    """Short-lived, opener-bound confirmation for one exact purge target set."""

    def __init__(self, bot, requester_id: str, guild, channel, target_ids: list[str], user_id: int | None):
        super().__init__(timeout=120)
        self.bot = bot
        self.requester_id = str(requester_id)
        self.guild = guild
        self.channel = channel
        self.guild_id = str(getattr(guild, "id", ""))
        self.channel_id = str(getattr(channel, "id", ""))
        self.target_ids = tuple(str(item) for item in target_ids)
        self.user_id = user_id
        self.preview_message = None

        confirm = discord.ui.Button(
            label=f"Delete {len(self.target_ids)} messages",
            style=discord.ButtonStyle.danger,
        )
        cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        confirm.callback = self._confirm  # type: ignore[method-assign]
        cancel.callback = self._cancel  # type: ignore[method-assign]
        self.add_item(confirm)
        self.add_item(cancel)

    async def interaction_check(self, interaction) -> bool:
        user_id = str(getattr(getattr(interaction, "user", None), "id", "") or "")
        guild_id = str(getattr(interaction, "guild_id", "") or "")
        channel_id = str(getattr(interaction, "channel_id", "") or "")
        if (
            user_id == self.requester_id
            and guild_id == self.guild_id
            and channel_id == self.channel_id
        ):
            return True
        response = getattr(interaction, "response", None)
        if response is not None and not response.is_done():
            await response.send_message(
                "Only the person who requested this purge can confirm or cancel it.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        return False

    async def _cancel(self, interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        if self.preview_message is not None:
            with contextlib.suppress(Exception):
                await self.preview_message.edit(
                    content="Purge cancelled. No messages were deleted.", view=None,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        await interaction.followup.send(
            "Purge cancelled.", ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.stop()

    async def _confirm(self, interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        requester_id = self.requester_id
        fetch_member = getattr(self.guild, "fetch_member", None)
        if not callable(fetch_member):
            result = "Purge refused: current server membership could not be verified."
            await interaction.followup.send(
                result, ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        try:
            member = await fetch_member(int(requester_id))
        except Exception:
            result = "Purge refused: you are no longer a member of this server."
            await interaction.followup.send(
                result, ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        # The interaction's embedded permission snapshot may predate a role
        # revoke. Fetch the current member and check the actual target channel.
        actor = SimpleNamespace(author=member, guild=self.guild, channel=self.channel)
        deleted = 0
        missing = 0
        refused = 0
        failures = 0
        permission_revoked = False
        for target_id in self.target_ids:
            missing_cap = _missing_cap(
                self.guild, "manage_messages", actor, channel=self.channel
            )
            if missing_cap:
                permission_revoked = True
                refused += len(self.target_ids) - deleted - missing - refused - failures
                break
            try:
                target = await self.channel.fetch_message(int(target_id))
            except discord.NotFound:
                missing += 1
                continue
            except discord.Forbidden:
                failures += 1
                break
            except discord.HTTPException:
                # A transient rate-limit/5xx on one fetch must not kill the
                # callback: the interaction was already deferred, so an
                # uncaught error would leave it pending forever with no result.
                failures += 1
                break
            if self.user_id is not None and str(
                getattr(getattr(target, "author", None), "id", "")
            ) != str(self.user_id):
                # The author is immutable in Discord. Treat any mismatch as a
                # stale/corrupt target set and do not delete it.
                refused += 1
                continue
            try:
                # discord.py 2.7 Message.delete only accepts delay. The audit
                # reason still belongs on the HTTP call, which does accept it.
                reason = _mod_reason(actor)
                state = getattr(target, "_state", None)
                http = getattr(state, "http", None)
                delete_message = getattr(http, "delete_message", None)
                channel_id = getattr(getattr(target, "channel", None), "id", None)
                message_id = getattr(target, "id", None)
                if (
                    callable(delete_message)
                    and channel_id is not None
                    and message_id is not None
                ):
                    await delete_message(channel_id, message_id, reason=reason)
                else:
                    try:
                        await target.delete(reason=reason)
                    except TypeError:
                        await target.delete()
                deleted += 1
            except discord.NotFound:
                missing += 1
            except discord.Forbidden:
                failures += 1
                break
            except discord.HTTPException:
                failures += 1
                break

        details = [f"deleted {deleted}"]
        if missing:
            details.append(f"already missing {missing}")
        if refused:
            details.append(f"not deleted {refused}")
        if failures:
            details.append(f"failed {failures}")
        result = (
            "Purge stopped because current channel permissions no longer allow it: "
            if permission_revoked
            else "Purge finished: "
        ) + ", ".join(details) + "."
        logger.info(
            "moderation action=purge_confirmed requester=%s guild=%s channel=%s target_count=%s deleted=%s missing=%s refused=%s failures=%s",
            requester_id,
            self.guild_id,
            self.channel_id,
            len(self.target_ids),
            deleted,
            missing,
            refused,
            failures,
        )
        if self.preview_message is not None:
            with contextlib.suppress(Exception):
                await self.preview_message.edit(
                    content=result, view=None,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        await interaction.followup.send(
            result, ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.stop()

    async def on_timeout(self) -> None:
        if self.preview_message is not None:
            with contextlib.suppress(Exception):
                await self.preview_message.edit(view=None)


class PurgeMessagesTool(Tool):
    tool_name = 'purge_messages'
    returns_result = True
    ends_turn = False

    def get_description(self):
        return (
            "Preview up to 20 recent messages in this channel for bulk deletion. "
            "Requires manage_messages; the person who requested it must confirm "
            "with the button. Params: limit (1-20, default 20), channel_id "
            "(optional current-server channel), user_id (optional author filter)."
        )

    async def execute(
        self,
        message: Message,
        limit: str = "20",
        channel_id: str | None = None,
        user_id: str | None = None,
        **kwargs,
    ) -> str:
        channel = getattr(message, "channel", None)
        if channel_id:
            channel, error = await _get_guild_channel(
                self.bot,
                channel_id,
                expected_guild_id=getattr(getattr(message, "guild", None), "id", None),
            )
            if error:
                return error
        guild = getattr(channel, "guild", None)
        if guild is None:
            return "Error: purge only works in servers"
        if str(getattr(guild, "id", "")) != str(
            getattr(getattr(message, "guild", None), "id", "")
        ):
            return "Error: target channel is outside this server"
        missing = _missing_cap(
            guild, "manage_messages", message, channel=channel
        )
        if missing:
            return missing
        try:
            cap = max(1, min(int(limit or 20), 20))
        except (TypeError, ValueError):
            return "Error: limit must be a number"
        uid = _parse_snowflake(user_id)
        if user_id and uid is None:
            return "Error: user_id must be a valid Discord user ID"
        if not hasattr(channel, "history"):
            return "Error: this channel type cannot be previewed for purge"

        try:
            candidates = []
            async for target in channel.history(limit=cap):
                author_id = str(getattr(getattr(target, "author", None), "id", "") or "")
                # Never include the bot's confirmation/status posts in a later
                # purge preview. The candidate set remains bounded and explicit.
                if str(getattr(getattr(self.bot, "user", None), "id", "")) == author_id:
                    continue
                if uid is not None and author_id != str(uid):
                    continue
                candidates.append(target)
            if not candidates:
                return "No matching recent messages were found; nothing was deleted."

            requester_id = str(getattr(getattr(message, "author", None), "id", "") or "")
            target_ids = [str(target.id) for target in candidates]
            lines = [
                f"Purge preview for {_channel_label(channel)} — {len(candidates)} exact target(s).",
                "Only the requester can confirm within 2 minutes. No message text is shown.",
            ]
            for target in candidates:
                created_at = getattr(target, "created_at", None)
                stamp = created_at.strftime("%Y-%m-%d %H:%M UTC") if created_at else "time unavailable"
                author_id = getattr(getattr(target, "author", None), "id", "unknown")
                lines.append(f"• message_id={target.id} author_id={author_id} at={stamp}")
            preview = "\n".join(lines)
            view = _PurgeConfirmationView(
                self.bot, requester_id, guild, channel, target_ids, uid
            )
            sent = await channel.send(
                preview,
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            view.preview_message = sent
            logger.info(
                "moderation action=purge_preview requester=%s guild=%s channel=%s target_count=%s target_ids=%s",
                requester_id,
                getattr(guild, "id", ""),
                getattr(channel, "id", ""),
                len(target_ids),
                ",".join(target_ids),
            )
            return (
                f"Previewed {len(candidates)} exact message target(s) in "
                f"{_channel_label(channel)}. The requester must use the confirmation button; nothing has been deleted yet."
            )
        except discord.Forbidden:
            return f"Error: Discord denied reading or posting a purge preview in {_channel_label(channel)}"
        except Exception as e:
            return f"Error creating purge preview: {e}"

class SearchMessagesTool(Tool):
    """Look up one message or search bounded history the requester can read."""
    tool_name = 'search_messages'
    returns_result = True
    ends_turn = False


    def get_description(self):
        return (
            "Look up a message by ID in this channel, by a Discord message link "
            "in this server, or by the current request's reply reference. "
            "Otherwise search this channel's history, including older messages. Filter "
            "with query (substring), author (display name, username, or user ID), "
            "and before/after (ISO-8601 time or unix seconds). A filtered search "
            "reads up to 400 messages in that window. Only channels both you and "
            "Maxwell can view and read are available. Results include the real "
            "ID, author, channel, timestamp, and a short content preview. "
            "Params: message_id, message_link, reply_reference, query, author, "
            "before, after, limit (1-10)."
        )

    @staticmethod
    def _message_link(value: str) -> tuple[str, str, str] | None:
        try:
            parsed = urlparse(str(value or "").strip().strip("<>"))
            if parsed.scheme != "https" or (parsed.hostname or "").lower() not in {
                "discord.com", "www.discord.com", "canary.discord.com",
                "ptb.discord.com", "discordapp.com", "www.discordapp.com",
            }:
                return None
            parts = [item for item in parsed.path.split("/") if item]
            if len(parts) != 4 or parts[0] != "channels":
                return None
            if not all(item.isdigit() for item in parts[1:]):
                return None
            return parts[1], parts[2], parts[3]
        except Exception:
            return None

    @staticmethod
    async def _read_denial(message, channel) -> str | None:
        guild = getattr(channel, "guild", None)
        if guild is None:
            return None if channel is getattr(message, "channel", None) else (
                "Error: direct-message lookup is limited to the current channel"
            )
        source_guild = getattr(message, "guild", None)
        if source_guild is None or str(getattr(source_guild, "id", "")) != str(
            getattr(guild, "id", "")
        ):
            return "Error: message lookup is limited to this server"
        requester = _resolve_requester_member(guild, message)
        bot_member = _guild_me(guild)
        if requester is None or bot_member is None:
            return "Error: current requester and bot membership must be verified"
        permissions_for = getattr(channel, "permissions_for", None)
        if not callable(permissions_for):
            return "Error: channel read permissions could not be verified"
        for member, label in ((requester, "you"), (bot_member, "Maxwell")):
            try:
                permissions = permissions_for(member)
            except Exception:
                permissions = None
            if permissions is None or not getattr(permissions, "view_channel", False):
                return f"Error: {label} cannot view that channel"
            if not getattr(permissions, "read_message_history", False):
                return f"Error: {label} cannot read that channel's history"
            if label == "you" and isinstance(channel, discord.Thread):
                # Thread.permissions_for inherits the parent channel and does
                # not account for private-thread membership.
                if getattr(channel, "type", None) == discord.ChannelType.private_thread:
                    if not getattr(permissions, "manage_threads", False):
                        try:
                            await channel.fetch_thread_member(requester.id)
                        except (discord.NotFound, AttributeError):
                            return "Error: you are not a member of that private thread"
                        except discord.HTTPException:
                            return "Error: private thread membership could not be verified"
        return None

    @staticmethod
    def _format_message(msg, channel) -> str:
        content = str(getattr(msg, "content", "") or "")
        snippet = content[:500] + ("…" if len(content) > 500 else "")
        author = getattr(msg, "author", None)
        author_name = getattr(author, "display_name", None) or getattr(author, "name", "unknown")
        author_id = getattr(author, "id", "unknown")
        created_at = getattr(msg, "created_at", None)
        timestamp = created_at.isoformat() if created_at is not None else "unknown"
        guild = getattr(channel, "guild", None)
        reply_id = getattr(getattr(msg, "reference", None), "message_id", None)
        attachments = [
            str(getattr(item, "filename", "attachment"))[:100]
            for item in list(getattr(msg, "attachments", None) or [])[:5]
        ]
        lines = [
            f"message_id={msg.id} author={author_name} author_id={author_id} "
            f"channel=#{getattr(channel, 'name', 'chat')} channel_id={getattr(channel, 'id', 'unknown')} "
            f"guild_id={getattr(guild, 'id', 'DM')} timestamp={timestamp}",
            f"content_preview={snippet or '[no text content]'}",
        ]
        if reply_id:
            lines.append(f"reply_to_message_id={reply_id}")
        if attachments:
            lines.append("attachments=" + ", ".join(attachments))
        return "\n".join(lines)

    @staticmethod
    def _parse_history_bound(value: str | None):
        """Return (discord history cursor, error). Cursor is a datetime or snowflake object."""
        from datetime import datetime, timezone

        text = str(value or "").strip()
        if not text:
            return None, None
        if text.isdigit():
            number = int(text)
            if number > 10**15:
                return discord.Object(id=number), None
            if number > 10**12:
                return datetime.fromtimestamp(number / 1000, tz=timezone.utc), None
            if number > 10**9:
                return datetime.fromtimestamp(number, tz=timezone.utc), None
            return None, "Error: before/after must be an ISO-8601 time or unix seconds"
        parsed = text[:-1] + "+00:00" if text.endswith("Z") else text
        try:
            when = datetime.fromisoformat(parsed)
        except ValueError:
            return None, "Error: before/after must be an ISO-8601 time or unix seconds"
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return when, None

    @staticmethod
    def _author_matches(msg, author: str) -> bool:
        needle = str(author or "").strip()
        if needle.startswith("<@") and needle.endswith(">"):
            needle = needle.strip("<@!>")
        person = getattr(msg, "author", None)
        if needle.isdigit() and str(getattr(person, "id", "")) == needle:
            return True
        low = needle.lower()
        names = (
            getattr(person, "display_name", None),
            getattr(person, "global_name", None),
            getattr(person, "name", None),
        )
        return any(low == str(name).lower() or low in str(name).lower() for name in names if name)

    async def execute(
        self,
        message: Message,
        query: str | None = None,
        limit: str = "5",
        message_id: str | None = None,
        message_link: str | None = None,
        reply_reference: str | None = None,
        author: str | None = None,
        before: str | None = None,
        after: str | None = None,
        **kwargs,
    ) -> str:
        chan = getattr(message, "channel", None)
        if not chan:
            return "Error: Channel context unavailable"
        try:
            search_limit = max(1, min(int(limit), 10))
            clean_query = str(query or "").strip().lower()
            clean_author = str(author or "").strip()
            before_at, before_error = self._parse_history_bound(before)
            if before_error:
                return before_error
            after_at, after_error = self._parse_history_bound(after)
            if after_error:
                return after_error
            selectors = sum(bool(str(v or "").strip()) for v in (message_id, message_link, reply_reference))
            if selectors > 1:
                return "Error: use only one of message_id, message_link, or reply_reference"
            target_channel = chan
            target_id = None
            if message_link:
                parsed = self._message_link(message_link)
                if parsed is None:
                    return "Error: message_link must be a valid Discord message link"
                guild_id, linked_channel, target_id = parsed
                source_guild = getattr(message, "guild", None)
                if source_guild is None or guild_id != str(getattr(source_guild, "id", "")):
                    return "Error: message lookup is limited to this server"
                target_channel, error = await _get_guild_channel(
                    self.bot,
                    linked_channel,
                    expected_guild_id=guild_id,
                )
                if error:
                    return error
            elif message_id:
                target_id = str(message_id).strip()
                if not target_id.isdigit():
                    return "Error: message_id must be numeric"
            elif reply_reference:
                target_id = str(reply_reference).strip()
                if not target_id.isdigit():
                    return "Error: reply_reference must be a message ID"
            elif not clean_query and not clean_author and before_at is None and after_at is None:
                ref = getattr(message, "reference", None)
                target_id = str(getattr(ref, "message_id", "") or "") or None

            denial = await self._read_denial(message, target_channel)
            if denial:
                return denial
            if target_id:
                if not hasattr(target_channel, "fetch_message"):
                    return "Error: message lookup is unavailable in this channel"
                try:
                    target = await target_channel.fetch_message(int(target_id))
                except discord.NotFound:
                    return f"Message {target_id} was not found in the authorized channel."
                except discord.Forbidden:
                    return "Error: Discord denied reading that message"
                return "Message lookup:\n" + self._format_message(target, target_channel)

            if not hasattr(target_channel, "history"):
                return "Error: bounded channel history is unavailable"
            # Text-only searches stay on a short recent window. Author or time
            # filters page further back, still capped so one call cannot walk
            # an entire channel.
            deep = bool(clean_author or before_at is not None or after_at is not None)
            if not clean_query and not deep:
                scan = search_limit
            elif deep:
                scan = 400
            else:
                scan = min(40, max(search_limit * 4, 15))
            hist_kwargs = {"limit": scan}
            if before_at is not None:
                hist_kwargs["before"] = before_at
            if after_at is not None:
                hist_kwargs["after"] = after_at
            if before_at is not None and after_at is not None:
                hist_kwargs["oldest_first"] = True
            results = []
            scanned = 0
            try:
                async for msg in target_channel.history(**hist_kwargs):
                    scanned += 1
                    if clean_author and not self._author_matches(msg, clean_author):
                        continue
                    if clean_query and clean_query not in (msg.content or "").lower():
                        continue
                    results.append(self._format_message(msg, target_channel))
                    if len(results) >= search_limit:
                        break
            except Exception as e:
                logger.warning("search_messages: channel scan failed: %s", e)
                return "Error searching messages: Discord history lookup failed"

            if not results:
                if not clean_query and not deep:
                    return "No recent messages found in this channel"
                window = " in the requested time window" if before_at or after_at else ""
                who = f" from '{clean_author[:80]}'" if clean_author else ""
                what = f" matching '{str(query or '')[:100]}'" if clean_query else ""
                return f"No messages found{what}{who}{window} in this channel (scanned {scanned})"
            reached_cap = scanned >= scan and len(results) < search_limit
            heading = (
                f"Recent messages ({len(results)}):\n"
                if not clean_query and not deep
                else "Search results:\n"
            )
            if reached_cap:
                heading += (
                    f"Scanned {scanned} messages in this channel and stopped. "
                    "Pass a tighter before/after to look further.\n"
                )
            marker = getattr(self.bot, "mark_message_tainted", None)
            if callable(marker):
                marker(message)
            return heading + "\n---\n".join(results)
        except Exception as e:
            return f"Error searching messages: {e}"

class CreateInviteTool(Tool):
    """Create an invite link for a server Maxwell is in."""
    tool_name = "create_invite"
    returns_result = True
    ends_turn = False

    def get_description(self):
        return (
            "Create a discord.gg invite for a server this bot is in. "
            "Pass server= name or ID when it is not the current room, "
            "including from DMs. Optional channel_id to pick the channel. "
            "The person asking still needs create_instant_invite there. "
            "Params: server, channel_id, max_uses (default 1), "
            "max_age (seconds, default 86400)."
        )

    async def execute(
        self,
        message: Message,
        max_uses: str = "1",
        max_age: str = "86400",
        server: str | None = None,
        channel_id: str | None = None,
        **kwargs,
    ) -> str:
        target = str(
            server or kwargs.get("guild") or kwargs.get("guild_id") or ""
        ).strip()
        channel_id = str(
            channel_id or kwargs.get("channel") or ""
        ).strip()
        guild = None
        if target:
            # Only search servers the requester can actually see. Matching
            # against every bot guild leaks unrelated names and IDs through
            # both the match and the ambiguity error.
            author_id = getattr(getattr(message, "author", None), "id", None)
            current_guild = getattr(message, "guild", None)
            visible = []
            for candidate in list(self.bot.guilds or []):
                if current_guild is not None and getattr(candidate, "id", None) == getattr(
                    current_guild, "id", None
                ):
                    visible.append(candidate)
                    continue
                getter = getattr(candidate, "get_member", None)
                if author_id is not None and callable(getter):
                    try:
                        if getter(author_id) is not None:
                            visible.append(candidate)
                    except Exception:
                        pass
            guild, err = _find_guild(visible, target)
            if guild is None:
                return err
        elif getattr(message, "guild", None):
            guild = message.guild
        else:
            return (
                "Error: say which server (name or ID). "
                "I can only make invites for servers I am in."
            )
        try:
            uses = int(max_uses)
            age = int(max_age)
        except (TypeError, ValueError):
            return "Error: max_uses and max_age must be numbers"
        if uses < 1 or uses > 100:
            return "Error: max_uses must be between 1 and 100"
        if age < 0 or age > 604800:
            return "Error: max_age must be between 0 and 604800 seconds"
        missing = _missing_cap(guild, "create_instant_invite", message)
        if missing:
            return missing
        channel = None
        if channel_id:
            channel, error = await _get_guild_channel(self.bot, channel_id)
            if error:
                return error
            ch_guild = getattr(channel, "guild", None)
            if getattr(ch_guild, "id", None) != getattr(guild, "id", None):
                return f"Error: that channel is not in {guild.name}"
        else:
            msg_guild = getattr(message, "guild", None)
            msg_channel = getattr(message, "channel", None)
            if (
                getattr(msg_guild, "id", None) == getattr(guild, "id", None)
                and hasattr(msg_channel, "create_invite")
            ):
                channel = msg_channel
            else:
                channel, err = _pick_invite_channel(guild)
                if channel is None:
                    return err
        missing = _missing_cap(
            guild, "create_instant_invite", message, channel=channel
        )
        if missing:
            return missing
        if not hasattr(channel, "create_invite"):
            return "Error: Cannot create invites from this channel type"
        try:
            invite = await channel.create_invite(max_uses=uses, max_age=age)
            return (
                f"Invite created for {guild.name}: {invite.url} "
                f"(max uses: {uses}, expires in: {age}s)"
            )
        except discord.Forbidden:
            return (
                f"Error: I don't have permission to create invites in {guild.name}"
            )
        except Exception as e:
            return f"Error creating invite: {e}"


def _pick_invite_channel(guild) -> tuple[Any, str]:
    """First channel in `guild` this bot can actually create an invite from."""
    me = _guild_me(guild)
    seen: set[int] = set()
    candidates: list[Any] = []
    for attr in ("system_channel", "rules_channel"):
        ch = getattr(guild, attr, None)
        cid = getattr(ch, "id", None)
        if ch is not None and cid not in seen:
            seen.add(cid)
            candidates.append(ch)
    text_channels = getattr(guild, "text_channels", None)
    if text_channels is None:
        text_channels = [
            ch
            for ch in (getattr(guild, "channels", None) or [])
            if hasattr(ch, "create_invite")
        ]
    for ch in text_channels:
        cid = getattr(ch, "id", None)
        if cid in seen:
            continue
        seen.add(cid)
        candidates.append(ch)
    for ch in candidates:
        if not hasattr(ch, "create_invite"):
            continue
        if me is not None:
            perms_for = getattr(ch, "permissions_for", None)
            if callable(perms_for):
                perms = None
                with contextlib.suppress(Exception):
                    perms = perms_for(me)
                if perms is not None and not (
                    getattr(perms, "administrator", False)
                    or getattr(perms, "create_instant_invite", False)
                ):
                    continue
        return ch, ""
    name = getattr(guild, "name", "that server")
    return None, f"Error: no channel in {name} I can create an invite from"


class BotInviteUrlTool(Tool):
    """Generate Discord OAuth links to add this bot as an app or server bot."""
    tool_name = "bot_invite_url"
    returns_result = True
    ends_turn = False

    def get_description(self):
        return (
            "Generate Discord OAuth links so someone can add this bot. "
            "Call this when they ask how to add Maxwell, add as app, "
            "add to my apps, add to a server, or want an install/invite "
            "link for THIS bot. kind=app (Add to my apps), kind=server "
            "(Add to a server), or kind=both (default). "
            "Not create_invite — that makes a discord.gg for a server I am in."
        )

    async def execute(
        self,
        message: Message,
        kind: str = "both",
        permissions: str = "",
        guild_id: str = "",
        **kwargs,
    ) -> str:
        client_id = application_client_id(self.bot)
        if not client_id:
            return (
                "Error: Discord application id is unknown. "
                "Set DISCORD_CLIENT_ID or wait until the bot is logged in."
            )
        try:
            urls = bot_oauth_install_urls(
                client_id,
                kind=kind,
                permissions=permissions,
                guild_id=guild_id,
            )
        except ValueError as exc:
            return f"Error: {exc}"
        lines: list[str] = []
        if "app" in urls:
            lines.append("Add as app (Add to my apps / user install):")
            lines.append(urls["app"])
        if "server" in urls:
            if lines:
                lines.append("")
            lines.append("Add to a server (guild bot):")
            lines.append(urls["server"])
        lines.append("")
        lines.append(
            "These are Discord OAuth install pages, not a discord.gg invite."
        )
        return "\n".join(lines)

class NoResponseTool(Tool):
    """Silently skip sending any reply to the current message"""
    tool_name = 'no_response'
    returns_result = False
    ends_turn = True


    def get_description(self):
        return (
            "Stay silent for unrelated chatter, spam, or an already answered message. "
            "Give a reason: unrelated, spam, already_answered, user_requested_silence, or other. "
            "A reaction/GIF can be a complete response to casual pings. Silence is allowed when requested or when you cannot send here."
        )

    async def execute(self, message: Message, reason: str = "other", **kwargs) -> str:
        journal = getattr(self.bot, "_request_journal", None)
        row = await journal.submit(journal.get, getattr(message, "id", "")) if journal is not None else None
        if row and row.get("status") == "delivered":
            return "__NO_RESPONSE__"
        directed = getattr(self.bot, "_directly_addressed", None)
        control = getattr(self.bot, "_control", None) or {}
        from discord_expressions import cannot_send
        if (
            reason != "user_requested_silence"
            and not cannot_send(message, self.bot)
            and parse_bool(control.get("require_direct_response"), True)
            and (
                bool(row and row.get("directed"))
                or (callable(directed) and directed(message))
            )
        ):
            return (
                "Error: this direct request has not received an answer. "
                "Use send_message for a substantive answer, or react(finish=true) for a casual acknowledgement."
            )
        allowed = {"unrelated", "spam", "already_answered", "user_requested_silence", "other"}
        reason = str(reason or "other")
        if reason not in allowed:
            reason = "other"
        record = getattr(self.bot, "_record_request_outcome", None)
        if callable(record):
            await record(message, "suppressed", reason=f"model_no_response:{reason}")
        return "__NO_RESPONSE__"
