"""Tool implementations for the discord_messages plugin.

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

class ReactTool(Tool):
    """React to a message with an emoji"""
    tool_name = 'react'
    returns_result = False
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
        return "Edit your own message. Params: message_id (required), content (required, new text)."

    async def execute(
        self,
        message: Message,
        message_id: str | None = None,
        content: str | None = None,
        **kwargs,
    ) -> str:
        if not message_id or not content:
            return "Error: message_id and content are required"
        try:
            msg = await message.channel.fetch_message(int(message_id))
            if msg.author.id != self.bot.user.id:
                return "Error: I can only edit my own messages"
            await msg.edit(content=content)
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
    returns_result = False
    ends_turn = False


    def get_description(self):
        return (
            "Delete a message. Your own messages always. Someone else's needs "
            "manage_messages. Params: message_id (required), channel_id (optional, "
            "defaults to the current channel)."
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
            channel, error = await _get_guild_channel(self.bot, channel_id)
            if error:
                return error
        if channel is None or not hasattr(channel, "fetch_message"):
            return "Error: channel is unavailable"
        try:
            msg = await channel.fetch_message(int(str(message_id).strip()))
            mine = self.bot.user and msg.author.id == self.bot.user.id
            guild = getattr(channel, "guild", None)
            if not mine:
                if guild is None:
                    return "Error: I can only delete my own messages here"
                missing = _missing_cap(
                    guild, "manage_messages", message, channel=channel
                )
                if missing:
                    return missing
            await msg.delete()
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
            if (
                src_guild
                and dest_guild
                and getattr(src_guild, "id", None) != getattr(dest_guild, "id", None)
            ):
                return "Error: refusing to forward across servers"

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
        # Intentionally NOT admin-gated. send_file is an output channel —
        # the model already has shell + every other tool to produce content,
        # and gating the return path on `_is_admin` was just a barrier that
        # blocked non-admin users from receiving files. The path-mode
        # allowlist (_allowed_send_file_bases) is the real safety boundary.
        # Path mode: send a file that already exists on disk (or in the shell
        # container — we docker-cp it out as a fallback for container paths).
        if path:
            # Normalize container paths (/home/maxwell/...) to the host bind
            # mount so the allowlist and resolver see a real host path.
            resolved_input = self._resolve_send_file_path(path)
            # First, the fast path: a regular host file the model knows about.
            host_path, host_error = await self._try_read_host_file(resolved_input)
            if host_path is not None:
                target = host_path
                tmp_to_clean = None
            else:
                # Fallback: the model passed a container-only path (anything
                # inside the maxwell-shell container). Try docker cp it out.
                # Allowed for any path inside the container — the model
                # already has shell access, and refusing "any file" creates
                # an artificial one-step barrier that breaks the round-trip.
                target, cp_error = await self._docker_cp_from_shell(path)
                if target is None:
                    return (
                        f"Error: could not read file at '{path}'. "
                        f"Host: {host_error or 'not found'}. "
                        f"Container: {cp_error or 'not found or not readable'}."
                    )
                tmp_to_clean = target

            try:
                blob = await asyncio.to_thread(target.read_bytes)
            except Exception as e:
                return f"Error reading file from disk: {e}"
            finally:
                if tmp_to_clean is not None:
                    with contextlib.suppress(Exception):
                        shutil.rmtree(tmp_to_clean.parent, ignore_errors=True)
            safe_name = _safe_attachment_filename(
                filename or target.name, default="file"
            )
            return await self._send_blob(message, blob, safe_name)

        # Inline-content mode (original behavior).
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
                blob = base64.b64decode(str(content), validate=True)
            elif mode in {"text", "utf8", "utf-8"}:
                blob = str(content).encode("utf-8")
            else:
                return "Error: encoding must be text or base64"
        except Exception as e:
            return f"Error: could not decode file content: {e}"

        return await self._send_blob(message, blob, safe_name)

    def _allowed_send_file_bases(self) -> list[str]:
        # Do NOT allow the full data/ tree (admins.json, cookies, traces, etc.).
        # Only export-safe subtrees and workspace dirs the tools themselves create.
        bases: list[str] = []
        data_dir = os.path.abspath(
            getattr(
                getattr(getattr(self, "bot", None), "config", None), "DATA_DIR", "data"
            )
            or "data"
        )
        bases.extend(
            os.path.join(data_dir, sub)
            for sub in ("exports", "public_files", "attachments")
        )
        site_dir = getattr(getattr(self, "bot", None), "config", None)
        if site_dir:
            site_path = getattr(site_dir, "MAXWELL_SITE_DIR", "")
            if site_path:
                bases.append(os.path.abspath(site_path))
        # Shell tool working dir (volume mounted into container as /home/maxwell).
        shell_host = os.path.join(os.path.dirname(__file__), "shelldocker")
        bases.append(os.path.abspath(shell_host))
        return bases

    def _resolve_send_file_path(self, raw_path: str) -> str:
        """Map a path the model might pass to the actual host path.

        Accepts both forms:
          * host paths: /root/maxwell/shelldocker/foo.png (or any allowed base)
          * container paths: /home/maxwell/foo.png  -> shelldocker/foo.png

        Returns the resolved absolute host path, or the original input if no
        remap is needed (let the existing _is_path_allowed check decide).
        """
        cleaned = str(raw_path or "").strip()
        if not cleaned:
            return cleaned
        # Normalize container-side /home/maxwell/<x> to the host bind mount.
        # Match /home/maxwell, /home/maxwell/, or just home/maxwell (defensive).
        m = re.match(r"^/?home/maxwell/?(.*)$", cleaned)
        if m:
            shell_host = os.path.abspath(
                os.path.join(os.path.dirname(__file__), "shelldocker")
            )
            rel = m.group(1).lstrip("/")
            return os.path.join(shell_host, rel) if rel else shell_host
        return cleaned

    async def _try_read_host_file(
        self, resolved_path: str
    ) -> tuple[Path | None, str | None]:
        """Read a file from the host if it exists in an allowed base.

        Returns (Path, None) on success, (None, error_string) on miss.
        """
        allowed_bases = self._allowed_send_file_bases()
        for base in allowed_bases:
            if _is_path_allowed(resolved_path, base):
                try:
                    p = Path(resolved_path).resolve()
                    if p.is_file():
                        return p, None
                except OSError:
                    continue
        return None, "not in an allowed host directory or not found"

    async def _docker_cp_from_shell(
        self, container_path: str
    ) -> tuple[Path | None, str | None]:
        """docker-cp a file out of the maxwell-shell container to a local temp
        path, then return that local Path. Used as a fallback when the model
        passes a path that only exists inside the container.

        Path safety: we only allow reads from inside the running
        maxwell-shell container. The container's root is bounded by the
        sandbox flags (no host FS mount by default; even in MAXWELL_SHELL_FULL_HOST
        mode, /host is a separate root).
        """
        if not container_path or not isinstance(container_path, str):
            return None, "empty path"
        clean = container_path.strip()
        if not clean.startswith("/"):
            clean = "/" + clean  # require absolute inside container
        # No traversal escapes from the container root; this is read-only.
        if ".." in clean.split("/"):
            return None, "path traversal not allowed"

        # Confirm the container is running.
        try:
            shell_tool = self.bot.tools.get("shell") if self.bot else None
            container_name = (
                getattr(shell_tool, "CONTAINER_NAME", "maxwell-shell")
                if shell_tool
                else "maxwell-shell"
            )
        except Exception:
            container_name = "maxwell-shell"

        tmp_dir = tempfile.mkdtemp(prefix="maxwell_sendfile_")
        local_path = os.path.join(tmp_dir, os.path.basename(clean) or "file")
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker",
                "cp",
                f"{container_name}:{clean}",
                local_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _stdout, stderr = await communicate_process(proc, timeout=15)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(Exception):
                    await proc.wait()
                with contextlib.suppress(Exception):
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                return None, "docker cp timed out"
            except asyncio.CancelledError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(Exception):
                    await proc.wait()
                with contextlib.suppress(Exception):
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                raise
            if proc.returncode != 0:
                with contextlib.suppress(Exception):
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                return None, (
                    stderr.decode(errors="replace").strip()
                    or f"docker cp exit {proc.returncode}"
                )
            if not os.path.isfile(local_path):
                with contextlib.suppress(Exception):
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                return None, "docker cp reported success but file is missing"
            return Path(local_path), None
        except FileNotFoundError:
            with contextlib.suppress(Exception):
                shutil.rmtree(tmp_dir, ignore_errors=True)
            return None, "docker is not installed or not on PATH"
        except Exception as e:
            with contextlib.suppress(Exception):
                shutil.rmtree(tmp_dir, ignore_errors=True)
            return None, f"docker cp failed: {e}"

    async def _send_blob(self, message: Message, blob: bytes, safe_name: str) -> str:
        if len(blob) > self.MAX_SIZE:
            return f"Error: file is too large (max {self.MAX_SIZE // 1024 // 1024} MB)"

        file = File(BytesIO(blob), filename=safe_name)
        sent = None
        try:
            try:
                sent = await message.reply(file=file)
            except (discord.NotFound, discord.HTTPException) as exc:
                code = getattr(exc, "code", None)
                parent_gone = isinstance(exc, discord.NotFound) or code in {
                    10008,
                    50035,
                }
                if code == 50035 and "message_reference" not in str(exc).lower():
                    raise
                if not parent_gone:
                    raise
                sent = await message.channel.send(file=file)
            record_delivery = getattr(self.bot, "_record_delivery", None)
            if callable(record_delivery) and sent is not None:
                record_delivery(message, sent)
        except discord.Forbidden:
            return "Error: no permission to send files here"
        except discord.HTTPException as e:
            return f"Error sending file: {e}"
        except Exception as e:
            return f"Error sending file: {e}"

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
            channel, error = await _get_guild_channel(self.bot, channel_id)
            if error:
                return error
        guild = getattr(channel, "guild", None)
        if guild is None:
            return "Error: pin only works in servers"
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
                return f"Unpinned message {message_id}"
            await msg.pin(reason=_mod_reason(message))
            return f"Pinned message {message_id}"
        except discord.NotFound:
            return f"Error: message {message_id} not found"
        except discord.Forbidden:
            return "Error: Discord denied pinning that message"
        except Exception as e:
            return f"Error pinning message: {e}"

class PurgeMessagesTool(Tool):
    tool_name = 'purge_messages'
    returns_result = True
    ends_turn = False

    def get_description(self):
        return (
            "Bulk-delete recent messages in a channel. Requires manage_messages. "
            "Params: limit (1-100, default 20), channel_id (optional), user_id (optional filter)."
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
            channel, error = await _get_guild_channel(self.bot, channel_id)
            if error:
                return error
        guild = getattr(channel, "guild", None)
        if guild is None:
            return "Error: purge only works in servers"
        missing = _missing_cap(
            guild, "manage_messages", message, channel=channel
        )
        if missing:
            return missing
        if not hasattr(channel, "purge"):
            return "Error: this channel type cannot be purged"
        try:
            cap = max(1, min(int(limit or 20), 100))
        except (TypeError, ValueError):
            return "Error: limit must be a number"
        uid = _parse_snowflake(user_id)

        def _check(msg):
            if uid is None:
                return True
            return getattr(getattr(msg, "author", None), "id", None) == uid

        try:
            deleted = await channel.purge(
                limit=cap, check=_check, reason=_mod_reason(message)
            )
            return f"Purged {len(deleted)} messages in {_channel_label(channel)}"
        except discord.Forbidden:
            return f"Error: Discord denied purging {_channel_label(channel)}"
        except Exception as e:
            return f"Error purging messages: {e}"

class SearchMessagesTool(Tool):
    """Search for messages in the server"""
    tool_name = 'search_messages'
    returns_result = True
    ends_turn = False


    def get_description(self):
        return (
            "Search recent messages in this channel only. "
            "Params: query (required), limit (optional, default 5, max 10)."
        )

    async def execute(
        self, message: Message, query: str | None = None, limit: str = "5", **kwargs
    ) -> str:
        chan = getattr(message, "channel", None)
        if not message.guild and not chan:
            return "Error: Channel context unavailable"
        try:
            search_limit = max(1, min(int(limit), 10))
            results = []
            clean_query = str(query or "").strip().lower()

            if not chan or not hasattr(chan, "history"):
                return "Error: Channel context unavailable"

            # This channel only. A guild-wide history walk (8 rooms × 50)
            # is how selfbots get 429'd and flagged.
            scan = (
                search_limit
                if not clean_query
                else min(40, max(search_limit * 4, 15))
            )
            try:
                async for msg in chan.history(limit=scan):
                    if clean_query and clean_query not in (msg.content or "").lower():
                        continue
                    snippet = msg.content[:150] + (
                        "..." if len(msg.content) > 150 else ""
                    )
                    label = getattr(chan, "name", "chat")
                    results.append(
                        f"[#{label} - {msg.id}] {msg.author.display_name}: {snippet}"
                    )
                    if len(results) >= search_limit:
                        break
            except Exception as e:
                logger.warning("search_messages: channel scan failed: %s", e)
                return f"Error searching messages: {e}"

            if not results:
                if not clean_query:
                    return "No recent messages found in this channel"
                return f"No messages found matching '{query}' in this channel"
            heading = (
                f"Recent messages ({len(results)}):\n"
                if not clean_query
                else "Search results:\n"
            )
            return heading + "\n".join(results)
        except Exception as e:
            return f"Error searching messages: {e}"

class CreateInviteTool(Tool):
    """Create an invite link for the server"""
    tool_name = 'create_invite'
    returns_result = True
    ends_turn = False


    def get_description(self):
        return (
            "Create a server invite link. Only works in servers. "
            "Params: max_uses (optional, default 1), max_age (optional, seconds, default 86400)."
        )

    async def execute(
        self, message: Message, max_uses: str = "1", max_age: str = "86400", **kwargs
    ) -> str:
        if not message.guild:
            return "Error: Cannot create invites in DMs"
        try:
            uses = int(max_uses)
            age = int(max_age)
            if uses < 1 or uses > 100:
                return "Error: max_uses must be between 1 and 100"
            if age < 0 or age > 604800:
                return "Error: max_age must be between 0 and 604800 seconds"
            channel = cast(Any, message.channel)
            if not hasattr(channel, "create_invite"):
                return "Error: Cannot create invites from this channel type"
            invite = await channel.create_invite(max_uses=uses, max_age=age)
            return (
                f"Invite created: {invite.url} (max uses: {uses}, expires in: {age}s)"
            )
        except discord.Forbidden:
            return "Error: I don't have permission to create invites here"
        except ValueError:
            return "Error: max_uses and max_age must be numbers"
        except Exception as e:
            return f"Error creating invite: {e}"

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
