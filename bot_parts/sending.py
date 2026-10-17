"""Outgoing message splitting, slowmode handling and send bookkeeping.

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

class SendingMixin:

    @staticmethod
    def _split_response(text: str, limit: int = 1900) -> list[str]:
        if len(text) <= limit:
            return [text]
        base_chunks = []
        current = ""
        for part in re.split(r"(\n+)", text):
            if len(current) + len(part) <= limit:
                current += part
            else:
                if current.strip():
                    base_chunks.append(current.strip())
                while len(part) > limit:
                    base_chunks.append(part[:limit].strip())
                    part = part[limit:]
                current = part
        if current.strip():
            base_chunks.append(current.strip())

        fixed: list[str] = []
        in_code_block = False
        for chunk in base_chunks:
            out = chunk
            if in_code_block:
                out = "```\n" + out
            # Whether we're still inside a fence after this chunk depends on
            # the fences the MODEL wrote in it, not on the count after our own
            # re-opener was prepended. Counting `out` conflated the two: a
            # chunk carrying the block's closing fence became even (re-opener +
            # closer), so the state was never cleared, the chunk went out
            # without its re-opener, and the NEXT chunk — ordinary prose — was
            # wrapped in ``` and rendered as code.
            if chunk.count("```") % 2 == 1:
                in_code_block = not in_code_block
            if in_code_block:
                out = out.rstrip() + "\n```"
            fixed.append(out)
        return fixed

    async def _respect_slowmode(self, channel) -> None:
        """Sleep if the channel's slowmode would block our next send.

        2026-07-21: Discord's per-channel slowmode (set on the channel by
        server admins) limits how often ANY user — including bots — can
        post. If the bot's last send in this channel was less than
        ``slowmode_delay`` seconds ago, the next POST would 429 and
        Discord's auto-retry would queue the reply for 2-12s. The user
        experience is "the bot is frozen" or "the bot is slowmoded even
        though it shouldn't be" (channel members don't realize admins
        set a slowmode that hits bots too).

        Fix: read ``channel.slowmode_delay`` (0 means no slowmode), check
        ``self._last_bot_send[channel_id]``, and sleep the delta so the
        POST lands inside the slowmode window. Capped at the slowmode
        itself so a 0s slowmode is free, a 10s slowmode waits at most
        10s, and a 1h slowmode waits at most 1h. Clamped to a 30s
        ceiling so a misconfigured 6h slowmode doesn't make the bot
        vanish from a channel — we just send and accept the 429.

        The ``_last_bot_send`` map is populated in ``_send_with_slowmode``
        after every successful send (so a failed 429 also re-arms the
        timer when it eventually succeeds).
        """
        try:
            slowmode = int(getattr(channel, "slowmode_delay", 0) or 0)
        except (TypeError, ValueError):
            slowmode = 0
        if slowmode <= 0:
            return
        # Don't make the bot vanish for 6h if a server admin sets an
        # absurd slowmode by mistake. The channel owner can disable it
        # with `,slowmode 0` (or via the channel settings).
        effective_cap = min(slowmode, 30)
        channel_id = str(getattr(channel, "id", ""))
        if not channel_id:
            return
        now = time.monotonic()
        last = self._last_bot_send.get(channel_id)
        if last is None:
            # Never sent here before: nothing to pace against. (Do not use a
            # 0.0 sentinel -- ``time.monotonic()`` is seconds since boot, so
            # right after startup it would look like we just sent.)
            return
        elapsed = now - last
        if elapsed >= effective_cap:
            return
        wait_s = effective_cap - elapsed
        logger.debug(
            "[SLOWMODE] channel=%s slowmode=%ss waiting %.2fs before send",
            channel_id,
            slowmode,
            wait_s,
        )
        await asyncio.sleep(wait_s)

    def _mark_bot_sent(self, channel) -> None:
        channel_id = str(getattr(channel, "id", "") or "")
        if not channel_id:
            return
        self._last_bot_send[channel_id] = time.monotonic()

    async def _finish_send(self, channel, request, sent):
        """Shared success tail of _send_with_slowmode: bookkeeping + return."""
        self._mark_bot_sent(channel)
        if request is not None:
            await MaxwellBot._record_delivery(self, request, sent)
        return sent

    async def _send_with_slowmode(
        self,
        channel,
        content: str | None = None,
        *,
        reply_to=None,
        file=None,
        **kwargs,
    ):
        """channel.send() / message.reply() wrapper that respects slowmode.

        Slowmode is a per-channel timer on POSTs, not on message contents,
        so it applies to BOTH the first chunk (often a ``reply()``) and
        follow-up chunks (always ``channel.send()``). Each call to this
        helper waits the channel's slowmode window, then dispatches.

        Returns the sent message on success, ``None`` on swallowable
        failure (Forbidden / NotFound on a plain channel.send). When
        ``reply_to`` is set and the parent message is gone, the send is
        retried as a plain ``channel.send`` so the reply still lands.

        Also drains the channel's typing indicator before dispatch so no
        typing event can trail the message (see typing_status).
        """
        await self._respect_slowmode(channel)
        request = _current_inbound.get()
        if request is not None:
            await MaxwellBot._mark_request_effect(self, request)
            state = _current_inbound_effects.get()
            if state is not None:
                state["send_attempted"] = True
        # Never forward a second `content`/`file` into discord.py — send()
        # takes those as keywords, and a leaked kwarg becomes
        # "got multiple values for keyword argument 'content'".
        kwargs.pop("content", None)
        kwargs.pop("file", None)
        stickers = kwargs.pop("stickers", None)
        # A YouTube unfurl can replace the live message with a data snapshot
        # that has no reply(). The answer still has to land in the channel.
        if reply_to is not None and not callable(getattr(reply_to, "reply", None)):
            logger.warning(
                "reply target has no reply(); sending without a reference in channel %s",
                getattr(channel, "id", "?"),
            )
            reply_to = None
        # Discord's reply-author notification is independent of mentions in
        # the response text. Notify the reply author without parsing @mentions.
        kwargs["allowed_mentions"] = discord.AllowedMentions(
            everyone=False, roles=False, users=False,
            replied_user=reply_to is not None and kwargs.get("mention_author", True),
        )
        # Drain typing BEFORE the message goes out. A typing event that
        # lands after the send re-lights "Maxwell is typing..." for a full
        # expiry window after he has already answered.
        await typing_suspend(self, channel)
        if reply_to is not None:
            # Catch Forbidden (no perms) and every flavour of "the parent
            # message is gone" so the response still reaches the user.
            try:
                if stickers:
                    sent = await reply_to.reply(
                        content=content, file=file, stickers=stickers, **kwargs
                    )
                else:
                    sent = await reply_to.reply(content=content, file=file, **kwargs)
            except discord.Forbidden:
                kwargs["allowed_mentions"] = discord.AllowedMentions.none()
                logger.warning(
                    "reply failed (forbidden) in channel %s; falling back to send",
                    getattr(channel, "id", "?"),
                )
                try:
                    if stickers:
                        sent = await channel.send(
                            content=content, file=file, stickers=stickers, **kwargs
                        )
                    else:
                        sent = await channel.send(content=content, file=file, **kwargs)
                except (discord.Forbidden, discord.NotFound) as exc:
                    logger.warning(
                        "fallback send failed (%s) in channel %s",
                        exc.__class__.__name__,
                        getattr(channel, "id", "?"),
                    )
                    return None
                return await MaxwellBot._finish_send(self, channel, request, sent)
            except (discord.NotFound, discord.HTTPException) as exc:
                # A deleted parent does NOT come back as a 404. Discord
                # answers the send with 400 "Invalid Form Body / In
                # message_reference: Unknown message" (error code 50035),
                # which discord.py raises as a plain HTTPException — so the
                # old NotFound-only handler never fired and the whole turn
                # died with an unhandled exception (pm2 bot-error.log
                # 2026-07-21 12:47 and 2026-07-23 09:24). Anything else keeps
                # propagating; only the unknown-reference case is swallowed.
                if not _is_unknown_reference_error(exc):
                    raise
                kwargs["allowed_mentions"] = discord.AllowedMentions.none()
                logger.warning(
                    "reply parent is gone (%s), falling back to channel.send in channel %s",
                    getattr(exc, "code", None) or exc.__class__.__name__,
                    getattr(channel, "id", "?"),
                )
                try:
                    if stickers:
                        sent = await channel.send(
                            content=content, file=file, stickers=stickers, **kwargs
                        )
                    else:
                        sent = await channel.send(content=content, file=file, **kwargs)
                except (discord.Forbidden, discord.NotFound) as exc:
                    logger.warning(
                        "fallback send failed (%s) in channel %s",
                        exc.__class__.__name__,
                        getattr(channel, "id", "?"),
                    )
                    return None
            return await MaxwellBot._finish_send(self, channel, request, sent)
        try:
            if stickers:
                sent = await channel.send(
                    content=content, file=file, stickers=stickers, **kwargs
                )
            else:
                sent = await channel.send(content=content, file=file, **kwargs)
        except (discord.Forbidden, discord.NotFound) as exc:
            logger.warning(
                "send failed (%s) in channel %s",
                exc.__class__.__name__,
                getattr(channel, "id", "?"),
            )
            return None
        return await MaxwellBot._finish_send(self, channel, request, sent)
