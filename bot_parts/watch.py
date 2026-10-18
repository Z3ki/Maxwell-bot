"""Conversation-watch state, debounce and live-reply decisions.

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

class WatchMixin:


    def _conversation_watch_seconds(self) -> float:
        fallback = float(DEFAULT_CONTROL["conversation_watch_seconds"])
        raw = (getattr(self, "_control", None) or {}).get(
            "conversation_watch_seconds", fallback
        )
        try:
            return max(0.0, min(float(raw), 3600.0))
        except (TypeError, ValueError):
            return fallback

    def _conversation_watch_enabled(self) -> bool:
        """Master switch for the post-reply 'should I talk?' poll.

        Off means he only answers hard pings (@ / reply / DM). Seconds=0
        is the older disable and still works.
        """
        control = getattr(self, "_control", None) or {}
        if not parse_bool(control.get("conversation_watch_enabled", True), True):
            return False
        getter = getattr(self, "_conversation_watch_seconds", None)
        if callable(getter):
            try:
                return float(getter()) > 0
            except (TypeError, ValueError):
                return True
        raw = control.get(
            "conversation_watch_seconds",
            DEFAULT_CONTROL["conversation_watch_seconds"],
        )
        try:
            return max(0.0, min(float(raw), 3600.0)) > 0
        except (TypeError, ValueError):
            return True

    def _watch_state(self, channel_id):
        """Per-room watch memory, created on demand and bounded.

        Rooms are evicted oldest-first once the registry is full; a room that
        comes back simply starts fresh, which costs one default-length watch
        window and nothing else.
        """
        cid = str(channel_id or "").strip()
        states = getattr(self, "_watch_states", None)
        if states is None:
            self._watch_states = {}
            states = self._watch_states
        state = states.get(cid)
        if state is None:
            state = watch_policy.WatchState()
            states[cid] = state
            if len(states) > _MAX_WATCH_STATES:
                for stale in list(states)[: len(states) - _MAX_WATCH_STATES // 2]:
                    if stale != cid:
                        states.pop(stale, None)
        return state

    def _arm_conversation_watch(self, channel_id) -> None:
        if not MaxwellBot._conversation_watch_enabled(self):
            return
        base = self._conversation_watch_seconds()
        if base <= 0:
            return
        cid = str(channel_id or "").strip()
        if not cid:
            return
        now = asyncio.get_running_loop().time()
        state = self._watch_state(cid)
        # How long this room earns depends on how the room is going: a live
        # exchange holds the watch longer than the configured base, a room
        # that has ignored his last few turns holds it for much less. The
        # configured value is the centre of that range, not a flat timer.
        seconds = watch_policy.window_seconds(state, base, now)
        watch = getattr(self, "_conversation_watch", None)
        if watch is None:
            self._conversation_watch = {}
            watch = self._conversation_watch
        watch[cid] = now + seconds
        if len(watch) > 200:
            self._conversation_watch = {k: exp for k, exp in watch.items() if exp > now}
        # _watch_states is deliberately NOT pruned alongside this. A room whose
        # watch just expired is the likeliest to be re-armed in a minute, and
        # its silent-streak history is exactly what should shape the next
        # window. _watch_state bounds that registry on its own.

    def _watch_address_signal(self, message) -> "watch_policy.AddressSignal":
        """Read who this message is aimed at, from Discord metadata.

        The one text-derived signal is whether his current display name shows
        up — which follows a rename, because the names come from the live
        client, not from a list in the source.
        """
        signal = watch_policy.AddressSignal()
        with contextlib.suppress(Exception):
            signal.direct = bool(self._directly_addressed(message))
        with contextlib.suppress(Exception):
            signal.soft = bool(self._soft_addressed(message))
        with contextlib.suppress(Exception):
            signal.reply_to_other = bool(self._replying_to_other(message))
        if not signal.reply_to_other:
            ref = getattr(getattr(message, "reference", None), "resolved", None)
            ref_id = getattr(getattr(ref, "author", None), "id", None)
            me_id = getattr(getattr(self, "user", None), "id", None)
            if ref_id is not None and me_id is not None and ref_id != me_id:
                signal.reply_to_other = True
        with contextlib.suppress(Exception):
            signal.mentions_other = bool(self._addressing_someone_else(message))
        signal.from_bot = bool(getattr(getattr(message, "author", None), "bot", False))
        text = str(getattr(message, "content", "") or "")
        signal.text_length = len(text.strip())
        signal.is_question = text.rstrip().endswith("?")
        signal.has_media = bool(
            getattr(message, "attachments", None) or getattr(message, "embeds", None)
        )
        me = getattr(self, "user", None)
        names = [
            getattr(me, "display_name", None),
            getattr(me, "name", None),
            getattr(self, "bot_name", None),
        ]
        signal.names_him = watch_policy.name_mentioned(text, [n for n in names if n])
        return signal

    def _note_watch_message(self, message) -> None:
        """Fold one observed line into its room's pace and engagement."""
        channel = getattr(message, "channel", None)
        cid = str(getattr(channel, "id", "") or "")
        if not cid:
            return
        try:
            now = asyncio.get_running_loop().time()
        except RuntimeError:
            return
        state = self._watch_state(cid)
        if not getattr(getattr(message, "author", None), "bot", False):
            state.observe_message(now)
        with contextlib.suppress(Exception):
            if self._directly_addressed(message):
                state.observe_engagement(now)

    def _note_watch_silence(self, message) -> None:
        """Record a watch turn that ended in no_response.

        This is the feedback that makes the window adaptive: rooms where he
        keeps choosing silence stop being watched so aggressively. The
        opposite signal — him actually speaking — is recorded by
        _arm_watch_from_own_message, which fires for every post he makes.
        """
        cid = str(getattr(getattr(message, "channel", None), "id", "") or "")
        if not cid:
            return
        state = self._watch_state(cid)
        state.turns += 1
        # Only a soft follow-up counts against the room. Declining to answer a
        # direct ping is a different decision and must not make the room look
        # like it is ignoring him.
        if not getattr(message, "_watch_followup", False):
            return
        state.observe_silence()

    def _conversation_watch_active(self, channel_id) -> bool:
        if not MaxwellBot._conversation_watch_enabled(self):
            return False
        watch = getattr(self, "_conversation_watch", None) or {}
        key = str(channel_id or "").strip()
        exp = watch.get(key)
        if exp is None:
            return False
        now = asyncio.get_running_loop().time()
        if now >= exp:
            watch.pop(key, None)
            return False
        return True

    def _conversation_watch_prompt(self, message, channel_id) -> list[str]:
        """Tell Maxwell when this room is still on watch."""
        lines: list[str] = []
        checker = getattr(self, "_conversation_watch_active", None)
        watching = False
        if callable(checker):
            with contextlib.suppress(Exception):
                watching = bool(checker(channel_id))
        if watching:
            lines.append(
                "Conversation watch is on in this room: you may speak without an @. "
                "Read the room naturally. no_response is the default when the line has nothing to do with you. "
                "Respond if someone asks a question, reacts to something you said, "
                "discusses something you are actively involved in, or makes a comment "
                "that naturally invites a reply from you in this current thread. "
                "Stay silent for side conversations between other people, unrelated banter, or spam. "
                "Be casual, concise, and natural."
            )
            # The old prompt spelled out each addressing case in its own
            # paragraph, which repeated the rule three times and left the
            # model to reconcile them. One computed line of facts about the
            # actual message reads better and covers the combinations prose
            # kept missing (named-but-not-pinged, broadcast, and so on).
            with contextlib.suppress(Exception):
                signal = self._watch_address_signal(message)
                state = self._watch_state(channel_id)
                now = asyncio.get_running_loop().time()
                pressure = watch_policy.reply_pressure(
                    signal, state, now, self._conversation_watch_seconds()
                )
                lines.append(watch_policy.describe_signal(signal, state, pressure, now))
                # The gate already refused everything well below the bar, so
                # a line that got here and is still only just over it is the
                # marginal case — say so rather than leaving him to guess.
                if (
                    not signal.direct
                    and pressure < self._watch_pressure_threshold() + 0.15
                ):
                    lines.append(
                        "This one barely cleared the bar for being worth "
                        "considering. Unless it is plainly for you, "
                        "no_response is the right answer."
                    )
        if getattr(message, "_watch_followup", False):
            lines.append(
                "Soft follow-up: they did not @ you or Discord-reply this time. "
                "Default is no_response when unrelated. Join in naturally if the conversation is ongoing with you. "
                "If the room has moved on to an unrelated topic or side chatter between others, "
                "stay silent. To Discord-reply to an earlier line, send_message with reply_to. Keep your replies concise and conversational."
            )
        burst_lines = self._watch_burst_prompt_lines(message)
        if burst_lines:
            lines.extend(burst_lines)
        typer = getattr(self, "_typing_prompt_lines", None)
        if callable(typer):
            with contextlib.suppress(Exception):
                lines.extend(typer(channel_id) or [])
        return lines

    def _watch_burst_prompt_lines(self, message) -> list[str]:
        """Quiet-window lines so watch isn't answering the last 'lol' in a vacuum."""
        burst = list(getattr(message, "_watch_burst", None) or [])
        if len(burst) < 2:
            return []
        rendered: list[str] = []
        for item in burst[-12:]:
            author = getattr(item, "author", None)
            name = (
                str(
                    getattr(author, "display_name", None)
                    or getattr(author, "name", None)
                    or "someone"
                ).strip()
                or "someone"
            )
            text = " ".join(str(getattr(item, "content", "") or "").split())[:240]
            if text:
                rendered.append(f"{name}: {text}")
        if len(rendered) < 2:
            return []
        return [
            "Quiet burst in this room (oldest→newest). This is the current "
            "moment — stay on it. The last line is [RESPOND TO THIS]:\n"
            + "\n".join(rendered)
        ]

    def _watch_pressure_threshold(self) -> float:
        """How much a soft line has to be asking for him before it costs a turn."""
        raw = (getattr(self, "_control", None) or {}).get(
            "conversation_watch_pressure",
            watch_policy.REPLY_PRESSURE_THRESHOLD,
        )
        try:
            return max(0.0, min(float(raw), 1.0))
        except (TypeError, ValueError):
            return watch_policy.REPLY_PRESSURE_THRESHOLD

    def _watch_reply_pressure(self, message, channel_id=None) -> float:
        """0..1 for this line — see watch_policy.reply_pressure."""
        if channel_id is None:
            channel_id = getattr(getattr(message, "channel", None), "id", "")
        signal = self._watch_address_signal(message)
        state = self._watch_state(channel_id)
        now = asyncio.get_running_loop().time()
        return watch_policy.reply_pressure(
            signal, state, now, self._conversation_watch_seconds()
        )

    def _busy_reason(self, channel_id=None) -> str:
        """What Maxwell is currently in the middle of, or "" if nothing.

        The watch is a background behaviour, so it yields to anything in the
        foreground. A turn that is generating an image holds its channel for
        a minute or more; every watch line that landed in the meantime used
        to queue behind it and then arrive in a clump the moment it finished.
        Anything running anywhere counts — one turn is one Maxwell, and a
        room he is not even in is still him being busy.
        """
        cid = str(channel_id or "").strip()
        if cid and self._channel_turn_active(cid):
            return "a turn is in flight in this room"
        queue = getattr(self, "_reply_queue", None)
        if queue is not None:
            if cid and queue.depth(cid):
                return "replies are already queued in this room"
            if queue.any_active():
                return "a turn is in flight in another room"
        if getattr(self, "_replying_channels", None):
            return "a turn is in flight in another room"
        active = getattr(self, "_active_requests", None) or {}
        if any(task is not None and not task.done() for task in active.values()):
            return "a request is still running"
        with contextlib.suppress(Exception):
            sleeping, _ = self._is_sleeping()
            if sleeping:
                return "he is asleep"
        return ""

    def _should_live_reply(self, message) -> bool:
        """Hard ping always. Soft lines have to earn the turn.

        A hard ping is a request and goes through whatever else is happening.
        Everything else has to clear two bars first: he has to be free, and
        the line itself has to look like it is asking him something. Before
        this, every human line in a watched room became a full LLM turn — and
        a model handed a turn and asked "should you reply?" says yes far more
        often than the room wanted.
        """
        author = getattr(message, "author", None)
        channel = getattr(message, "channel", None)
        if author is None or channel is None:
            return self._directly_addressed(message)
        if message_is_discord_system_event(message) and not self._directly_addressed(
            message
        ):
            return False
        if getattr(author, "bot", False):
            return False
        if self._directly_addressed(message):
            return True
        cid = getattr(channel, "id", "")
        if not self._conversation_watch_active(cid):
            return False
        busy = self._busy_reason(cid)
        if busy:
            logger.debug("Watch: skipping soft line in %s (%s)", cid, busy)
            return False
        pressure = 0.0
        with contextlib.suppress(Exception):
            pressure = self._watch_reply_pressure(message, cid)
        if not watch_policy.should_consider_reply(
            pressure, self._watch_pressure_threshold()
        ):
            logger.debug(
                "Watch: soft line in %s below bar (pressure %.2f)", cid, pressure
            )
            # A line he never even considered is not the room ignoring him,
            # so it must not count against the room's patience — that is
            # recorded only for turns he actually took.
            return False
        return True

    async def _arm_watch_from_own_message(self, message) -> None:
        """Any post from Maxwell keeps that whole room on watch."""
        channel = getattr(message, "channel", None)
        cid = getattr(channel, "id", "")
        # Record it before arming: speaking clears the silent streak, so the
        # window this arms is computed with his patience restored.
        with contextlib.suppress(Exception):
            self._watch_state(cid).observe_spoke(asyncio.get_running_loop().time())
        self._arm_conversation_watch(cid)

    def _watch_debounce_seconds(self, channel_id=None) -> float:
        """How long to let a burst settle before answering it.

        The configured value is the baseline for a room with no measured
        pace. Once a room's rhythm is known the wait tracks it: in a channel
        where people post every half second, a flat 1s lands mid-thought and
        he answers a fragment; in a slow channel the same 1s is needless
        delay. Still clamped to the same 0.05–5s envelope.
        """
        raw = (getattr(self, "_control", None) or {}).get(
            "conversation_watch_debounce_seconds", 1.0
        )
        try:
            base = max(0.05, min(float(raw), 5.0))
        except (TypeError, ValueError):
            base = 1.0
        if channel_id is None:
            return base
        try:
            state = self._watch_state(channel_id)
        except Exception:
            return base
        return max(0.05, min(watch_policy.debounce_seconds(state, base), 5.0))

    def _release_other_directed(self, bucket, replacing) -> None:
        """Keep another person's pending reply. Same-user bursts still collapse."""
        previous = (bucket or {}).get("latest_directed")
        if previous is None or previous is replacing:
            return
        prev_uid = str(getattr(getattr(previous, "author", None), "id", "") or "")
        next_uid = str(getattr(getattr(replacing, "author", None), "id", "") or "")
        if replacing is not None and prev_uid and prev_uid != next_uid:
            content = (bucket or {}).get("content") or getattr(previous, "content", "") or ""
            self._track_task(
                asyncio.create_task(
                    self._dispatch_reply(previous, content, directed=True),
                    name="watch-other-user",
                )
            )
            return
        MaxwellBot._record_request_outcome(self, previous, "superseded", "watch_coalesced")

    def _cancel_watch_debounce(self, channel_id, *, replacing=None) -> None:
        bucket = (getattr(self, "_watch_debounce", None) or {}).pop(
            str(channel_id or ""), None
        )
        task = (bucket or {}).get("task")
        if bucket:
            if replacing is not None:
                self._release_other_directed(bucket, replacing)
            else:
                MaxwellBot._record_request_outcome(self, bucket.get("latest_directed"), "superseded", "watch_cancelled")
        if task is not None and not task.done():
            cancel_once(task)

    def _queue_watch_reply(
        self, message, content: str, *, directed: bool | None = None
    ) -> None:
        """Start or reset the 1s watch wait. One flush, one request."""
        if directed is None:
            directed = self._watch_followup_is_directed(
                message
            ) or self._directly_addressed(message)
        channel = getattr(message, "channel", None)
        cid = str(getattr(channel, "id", "") or "")
        if not cid:
            return
        debounce = getattr(self, "_watch_debounce", None)
        if debounce is None:
            self._watch_debounce = {}
            debounce = self._watch_debounce
        bucket = debounce.get(cid)
        if bucket is None:
            if not directed:
                return
            bucket = {"first_seen": time.time()}
            debounce[cid] = bucket
        elif "first_seen" not in bucket:
            bucket["first_seen"] = time.time()
        bucket["latest"] = message
        burst = list(bucket.get("burst") or [])
        last = burst[-1] if burst else None
        last_id = getattr(last, "id", None) if last is not None else None
        this_id = getattr(message, "id", None)
        if last is None or this_id is None or this_id != last_id:
            burst.append(message)
        if len(burst) > 24:
            burst = burst[-24:]
        bucket["burst"] = burst
        if directed:
            self._release_other_directed(bucket, message)
            bucket["latest_directed"] = message
            bucket["content"] = content
            MaxwellBot._record_request_outcome(self, message, "queued", "watch_debounce")
        old = bucket.get("task")
        if old is not None and not old.done():
            cancel_once(old)
        delay = self._watch_debounce_seconds(cid)
        # Prevent indefinite starvation: if a burst keeps coming for > 3.0s, cap the remaining delay
        first_seen = bucket.get("first_seen", time.time())
        elapsed = max(0.0, time.time() - first_seen)
        if elapsed > 3.0:
            delay = min(delay, 0.2)
        elif elapsed + delay > 3.0:
            delay = max(0.1, 3.0 - elapsed)
        bucket["task"] = self._track_task(
            asyncio.create_task(
                self._flush_watch_reply(cid, delay),
                name=f"watch-debounce-{cid}",
            )
        )

    def _touch_watch_debounce(self, message) -> None:
        """A new line in a waiting room stretches the quiet timer."""
        cid = str(getattr(getattr(message, "channel", None), "id", "") or "")
        if cid and cid in (getattr(self, "_watch_debounce", None) or {}):
            self._queue_watch_reply(
                message, getattr(message, "content", "") or "", directed=False
            )

    async def _flush_watch_reply(self, channel_id: str, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        bucket = (getattr(self, "_watch_debounce", None) or {}).pop(
            str(channel_id), None
        )
        if not bucket:
            return
        if getattr(self, "_draining", False):
            return
        target = bucket.get("latest_directed") or bucket.get("latest")
        if target is None:
            return
        content = getattr(target, "content", "") or bucket.get("content") or ""
        directed = self._directly_addressed(target)
        if not directed and not MaxwellBot._conversation_watch_enabled(self):
            await MaxwellBot._record_request_outcome(self, target, "suppressed", "watch_disabled")
            return
        # Busy is re-checked here, not just when the line arrived: an image
        # generation or a long tool call can start during the debounce wait,
        # and a soft follow-up that waits it out then lands on top of the
        # result is exactly the flood this is meant to prevent. A hard ping
        # still goes through — that one was asked for.
        if not directed:
            busy = self._busy_reason(channel_id)
            if busy:
                await MaxwellBot._record_request_outcome(self, target, "suppressed", "watch_busy")
                logger.info(
                    "Watch debounce: dropping soft follow-up in %s (%s)",
                    channel_id,
                    busy,
                )
                return
        with contextlib.suppress(Exception):
            target._watch_followup = True
            target._watch_burst = list(bucket.get("burst") or [])
        logger.info(
            "Watch debounce: one reply in %s after %.1fs quiet",
            channel_id,
            delay,
        )
        # No lock acquire here. The reply queue serializes per channel and
        # waits its turn rather than timing out and requeueing through this
        # same debounce — the old loop that could end in zero replies.
        await self._dispatch_reply(target, content, directed=directed)

    async def _maybe_live_reply(self, message, content: str) -> None:
        """Direct mentions reply immediately; soft chatter waits debounce quiet timer."""
        same_user_check = getattr(self, "_is_same_user_interrupt", None)
        same_user_interrupt = bool(same_user_check(message)) if callable(same_user_check) else False
        if same_user_interrupt or self._directly_addressed(message):
            if same_user_interrupt and not self._note_same_user_latest(message):
                await self._record_request_outcome(message, "superseded", "same_user_interrupt")
                return
            self._cancel_watch_debounce(
                getattr(getattr(message, "channel", None), "id", ""),
                replacing=message,
            )
            await self._dispatch_reply(message, content, directed=True)
            return
        if self._should_live_reply(message):
            self._queue_watch_reply(message, content, directed=True)
            return
        self._touch_watch_debounce(message)
