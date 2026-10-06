"""Tool implementations for the discord_messages plugin.

Moved out of the historical bot_tools.py monolith. Shared helpers live in
``tooling.helpers``.
"""
from __future__ import annotations

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
    """React to a message with an emoji"""
    tool_name = 'react'
    returns_result = True
    ends_turn = False


    _CUSTOM_EMOJI_RE = re.compile(
        r"^<(?P<animated>a?):(?P<name>[A-Za-z0-9_]{2,32}):(?P<id>\d{15,25})>$"
    )
    _BROKEN_CUSTOM_EMOJI_RE = re.compile(r"^<a?:(?P<name>[A-Za-z0-9_]{2,32}):?>$")
    _ALIAS_RE = re.compile(r"^:(?P<name>[A-Za-z0-9_]{2,32}):$")
    _BARE_CUSTOM_NAME_RE = re.compile(r"^[A-Za-z0-9_]{2,32}$")

    def get_description(self):
        return (
            "React to the current message with an emoji. "
            "Standard emoji: 👍 🐱 🔥. Custom emoji: use an available guild emoji name like dave, or a full <:name:id> emoji. "
            "Params: emoji (required)."
        )

    def _available_custom_emoji_hint(self, guild_id: str | None) -> str:
        if not guild_id:
            return "No guild custom emojis are available here; use a normal Unicode emoji like 👍."
        names = sorted((self.bot._guild_emojis.get(guild_id, {}) or {}).keys())[:20]
        if not names:
            return "This guild has no custom emojis cached; use a normal Unicode emoji like 👍."
        return "Available custom emoji names: " + ", ".join(names)

    async def execute(
        self, message: Message, emoji: str | None = None, **kwargs
    ) -> str:
        if not emoji:
            return "Error: emoji parameter is required"

        raw = str(emoji).strip()
        if not raw:
            return "Error: emoji parameter is required"
        # Embed unfurls swap the live message for a data snapshot. Reacting
        # to that snapshot used to raise AttributeError and crash the tool.
        if not callable(getattr(message, "add_reaction", None)):
            return "Error: cannot react to this message"

        guild = message.guild
        guild_id = str(guild.id) if guild else None
        guild_emojis = self.bot._guild_emojis.get(guild_id, {}) if guild_id else {}

        full_match = self._CUSTOM_EMOJI_RE.match(raw)
        if full_match and guild:
            emoji_id = int(full_match.group("id"))
            for e in guild.emojis:
                if int(e.id) == emoji_id:
                    try:
                        await message.add_reaction(e)
                        return f"Reacted with {e}"
                    except discord.HTTPException as ex:
                        return f"Error: Could not add reaction — {ex}"
            # Full emoji strings can still be valid if Discord lets this bot use
            # the emoji cross-guild. Try it, but don't try malformed nonsense.
            try:
                await message.add_reaction(raw)
                return f"Reacted with {raw}"
            except discord.NotFound:
                return f"Error: Emoji '{raw}' not found or invalid"
            except discord.HTTPException as e:
                return f"Error: Could not add reaction — {e}"

        alias_match = self._ALIAS_RE.match(raw)
        broken_match = self._BROKEN_CUSTOM_EMOJI_RE.match(raw)
        custom_name_match = alias_match if alias_match is not None else broken_match
        if custom_name_match is not None:
            lookup = custom_name_match.group("name").lower()
        else:
            lookup = raw.lower()

        if guild and self._BARE_CUSTOM_NAME_RE.match(lookup):
            if lookup in guild_emojis:
                for e in guild.emojis:
                    if e.name.lower() == lookup:
                        try:
                            await message.add_reaction(e)
                            return f"Reacted with {e}"
                        except discord.HTTPException as ex:
                            return f"Error: Could not add reaction — {ex}"
            if (
                alias_match
                or broken_match
                or raw == lookup
                or self._BARE_CUSTOM_NAME_RE.match(raw)
            ):
                # Discord treats unknown custom names as a 400. Returning a local
                # error keeps the LLM from faceplanting into Unknown Emoji loops.
                return f"Error: custom emoji '{lookup}' is not available in this guild. {self._available_custom_emoji_hint(guild_id)}"

        if (
            alias_match
            or broken_match
            or (not guild and self._BARE_CUSTOM_NAME_RE.match(lookup))
        ):
            return f"Error: custom emoji '{lookup}' is not available here. {self._available_custom_emoji_hint(guild_id)}"

        # Fallback: Unicode emoji or another Discord-supported reaction string.
        try:
            await message.add_reaction(raw)
            return f"Reacted with {raw}"
        except discord.NotFound:
            return f"Error: Emoji '{raw}' not found or invalid"
        except discord.HTTPException as e:
            return f"Error: Could not add reaction — {e}"

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
            owner = journal.find_response(message_id) if journal is not None else None
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
                owner = journal.find_response(message_id) if journal is not None else None
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
            "Create a poll. Params: question (required), options (required, comma-separated, e.g. 'Yes,No,Maybe'), "
            "duration_hours (optional, default 24)."
        )

    async def execute(
        self,
        message: Message,
        question: str | None = None,
        options: str | None = None,
        duration_hours: str = "24",
        **kwargs,
    ) -> str:
        if not question or not options:
            return "Error: question and options are required"
        try:
            option_list = [o.strip() for o in options.split(",") if o.strip()]
            if len(option_list) < 2:
                return "Error: Need at least 2 options for a poll"
            if len(option_list) > 10:
                return "Error: Maximum 10 options allowed"

            hours = int(duration_hours)
            if hours < 1 or hours > 168:
                return "Error: duration_hours must be between 1 and 168"
            poll = discord.Poll(
                question=question,
                duration=timedelta(hours=hours),
            )
            for opt in option_list:
                poll.add_answer(text=opt)

            # Step aside for the live progress message before posting
            # the poll. The poll itself is the user-visible action.
            self._signal_streaming(message)
            await message.channel.send(poll=poll)
            return f"Poll created: '{question}' with options: {', '.join(option_list)}"
        except ValueError:
            return "Error: duration_hours must be a number"
        except Exception as e:
            return f"Error creating poll: {e}"

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
            "Send a message to the current chat. Default: one call per turn with the full reply. "
            "You can call this more than once if you actually want separate Discord messages; do not split a normal reply. "
            "Content supports Discord markdown: **bold**, *italic*, `code`, ```code blocks```, > quotes, bullet lists. "
            "Params: content (required), reply (optional bool, default true — Discord "
            "quote-reply is on; pass false only for a standalone line with no quote), "
            "reply_to (optional short quote or who said it, like nah or alice — not an id). "
            "channel_id/user_id to another channel or server is not available from DMs. "
            "From a server, that is admin-only — everyone else must reply in this chat."
        )

    @staticmethod
    def _chunks(text: str, limit: int = 1900) -> list[str]:
        # Discord hard-fails over 2000 chars. Keep this dumb and reliable; fancy
        # code-fence stitching lives in bot.py, but tools must not explode.
        chunks = []
        remaining = text
        while remaining:
            if len(remaining) <= limit:
                chunks.append(remaining)
                break
            cut = remaining.rfind("\n", 0, limit)
            if cut < limit // 2:
                cut = limit
            chunks.append(remaining[:cut].rstrip())
            remaining = remaining[cut:].lstrip()
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

            guild = getattr(target_channel, "guild", None)
            stickers = []
            if self.bot and hasattr(self.bot, "_render_custom_emojis"):
                text = self.bot._render_custom_emojis(text, guild)
            if self.bot and hasattr(self.bot, "_extract_stickers_from_text"):
                text, stickers = self.bot._extract_stickers_from_text(text, guild)

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
                            mark_effect(message)
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
                                    return "Error: missing permissions to send message"
                                record = getattr(self.bot, "_record_request_outcome", None)
                                if callable(record):
                                    record(message, "delivered", reason="partial_delivery")
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
                            record_delivery(message, sent)
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
                                    record(message, "delivered", reason="partial_delivery")
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
            return "Error: missing permissions to send message"
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
            "(inline), or path (existing file / container path). "
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
                if len(str(content)) > ((self.MAX_SIZE + 2) // 3) * 4:
                    return "Error: file is too large"
                blob = base64.b64decode(str(content), validate=True)
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

        file = File(BytesIO(blob), filename=safe_name)
        sent, error = await deliver_attachment(message, file, label="file")
        if error:
            return error
        record_delivery = getattr(self.bot, "_record_delivery", None)
        if callable(record_delivery) and sent is not None:
            record_delivery(message, sent)

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
            "in this server, or by the current request's reply reference; otherwise "
            "search/list bounded recent history in this channel. Only channels both "
            "you and Maxwell can view and read are available. Results include the "
            "real ID, author, channel, timestamp, and a short content preview. "
            "Params: message_id, message_link, reply_reference, query, limit (1-10)."
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
    def _read_denial(message, channel) -> str | None:
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

    async def execute(
        self,
        message: Message,
        query: str | None = None,
        limit: str = "5",
        message_id: str | None = None,
        message_link: str | None = None,
        reply_reference: str | None = None,
        **kwargs,
    ) -> str:
        chan = getattr(message, "channel", None)
        if not chan:
            return "Error: Channel context unavailable"
        try:
            search_limit = max(1, min(int(limit), 10))
            clean_query = str(query or "").strip().lower()
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
            elif not clean_query:
                ref = getattr(message, "reference", None)
                target_id = str(getattr(ref, "message_id", "") or "") or None

            denial = self._read_denial(message, target_channel)
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
            # This channel only, with a hard maximum scan of 40 messages.
            scan = search_limit if not clean_query else min(40, max(search_limit * 4, 15))
            results = []
            try:
                async for msg in target_channel.history(limit=scan):
                    if clean_query and clean_query not in (msg.content or "").lower():
                        continue
                    results.append(self._format_message(msg, target_channel))
                    if len(results) >= search_limit:
                        break
            except Exception as e:
                logger.warning("search_messages: channel scan failed: %s", e)
                return "Error searching messages: Discord history lookup failed"

            if not results:
                if not clean_query:
                    return "No recent messages found in this channel"
                return (
                    f"No messages found matching '{str(query or '')[:100]}' "
                    "in this channel"
                )
            heading = (
                f"Recent messages ({len(results)}):\n"
                if not clean_query
                else "Search results:\n"
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
            guild, err = _find_guild(list(self.bot.guilds or []), target)
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
            "An unanswered direct ping or DM normally requires send_message, not no_response."
        )

    async def execute(self, message: Message, reason: str = "other", **kwargs) -> str:
        journal = getattr(self.bot, "_request_journal", None)
        row = journal.get(getattr(message, "id", "")) if journal is not None else None
        if row and row.get("status") == "delivered":
            return "__NO_RESPONSE__"
        directed = getattr(self.bot, "_directly_addressed", None)
        control = getattr(self.bot, "_control", None) or {}
        if (
            parse_bool(control.get("require_direct_response"), True)
            and (
                bool(row and row.get("directed"))
                or (callable(directed) and directed(message))
            )
        ):
            return (
                "Error: this direct request has not received an answer. "
                "Use send_message to answer or acknowledge it."
            )
        allowed = {"unrelated", "spam", "already_answered", "user_requested_silence", "other"}
        reason = str(reason or "other")
        if reason not in allowed:
            reason = "other"
        record = getattr(self.bot, "_record_request_outcome", None)
        if callable(record):
            record(message, "suppressed", reason=f"model_no_response:{reason}")
        return "__NO_RESPONSE__"
