"""Sleep/wake state and the sleep gate.

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

class SleepMixin:

    def _sleep_window_active(self) -> bool:
        """True while a sleep deadline is in the future. No auto-clear."""
        until = float(getattr(self, "_sleep_until", 0.0) or 0.0)
        if until <= 0:
            return False
        try:
            now = asyncio.get_running_loop().time()
        except RuntimeError:
            return False
        return now < until

    def _is_sleeping(self) -> tuple[bool, int]:
        """Return (sleeping, seconds_remaining) for manual sleep only.

        Night hours select the configured fallback model; they never block
        dispatch or alter Discord presence.
        """
        if self._sleep_until <= 0:
            return False, 0
        now = asyncio.get_running_loop().time()
        if now >= self._sleep_until:
            self._sleep_until = 0.0
            self._sleep_notified_at.clear()
            cancel = getattr(self, "_cancel_sleep_wake", None)
            if callable(cancel):
                cancel()
            schedule = getattr(self, "_schedule_sleep_presence", None)
            if callable(schedule):
                schedule(asleep=False)
            return False, 0
        return True, int(self._sleep_until - now)

    async def set_sleep(self, duration_minutes: int) -> str:
        """Set a sleep window. Max 60 minutes (clamped). Returns a
        human-readable confirmation for the model/command to relay.
        2026-07-19: this is the structural replacement for the bot's
        goodbye-spam behavior — instead of saying 'goodnight' in every
        reply when the conversation winds down, the model can take an
        actual off-switch.
        """
        if duration_minutes < 1:
            duration_minutes = 1
        if duration_minutes > 60:
            duration_minutes = 60
        now = asyncio.get_running_loop().time()
        self._sleep_until = now + duration_minutes * 60
        # Clear the dedup so the wake-up notice is fresh.
        self._sleep_notified_at.clear()
        # Drop every conversation watch and every follow-up already waiting
        # out its debounce. Otherwise going to sleep mid-conversation leaves
        # a queued turn that fires a second later, from someone asleep.
        dropper = getattr(self, "_drop_watches_for_sleep", None)
        if callable(dropper):
            dropper()
        arm = getattr(self, "_arm_sleep_wake", None)
        if callable(arm):
            arm()
        apply = getattr(self, "_apply_sleep_presence", None)
        if callable(apply):
            result = apply(asleep=True)
            if asyncio.iscoroutine(result):
                await result
        return f"sleeping for {duration_minutes}m"

    def _drop_watches_for_sleep(self) -> None:
        """Forget every armed watch and cancel every pending follow-up."""
        with contextlib.suppress(Exception):
            (getattr(self, "_conversation_watch", None) or {}).clear()
        for cid in list((getattr(self, "_watch_debounce", None) or {})):
            with contextlib.suppress(Exception):
                self._cancel_watch_debounce(cid)
        with contextlib.suppress(Exception):
            (getattr(self, "_watch_debounce", None) or {}).clear()

    async def clear_sleep(self) -> str:
        """Cancel any active sleep window. Idempotent."""
        if self._sleep_until <= 0:
            return "not sleeping"
        self._sleep_until = 0.0
        self._sleep_notified_at.clear()
        cancel = getattr(self, "_cancel_sleep_wake", None)
        if callable(cancel):
            cancel()
        apply = getattr(self, "_apply_sleep_presence", None)
        if callable(apply):
            result = apply(asleep=False)
            if asyncio.iscoroutine(result):
                await result
        return "sleep cleared, awake now"

    def _cancel_sleep_wake(self) -> None:
        task = getattr(self, "_sleep_wake_task", None)
        if task is not None and not task.done():
            cancel_once(task)
        self._sleep_wake_task = None

    def _arm_sleep_wake(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._cancel_sleep_wake()
        wake = getattr(self, "_sleep_wake_when_due", None)
        if not callable(wake):
            return
        self._sleep_wake_task = loop.create_task(wake())

    async def _sleep_wake_when_due(self) -> None:
        try:
            loop = asyncio.get_running_loop()
            while True:
                until = float(getattr(self, "_sleep_until", 0.0) or 0.0)
                if until <= 0:
                    return
                remaining = until - loop.time()
                if remaining <= 0:
                    break
                await asyncio.sleep(remaining)
            if self._sleep_until > 0:
                self._sleep_until = 0.0
                self._sleep_notified_at.clear()
                await self._apply_sleep_presence(asleep=False)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.debug("Sleep wake task failed: %s", e)

    def _schedule_sleep_presence(self, *, asleep: bool) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        apply = getattr(self, "_apply_sleep_presence", None)
        if not callable(apply):
            return

        async def _run() -> None:
            try:
                await apply(asleep=asleep)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.debug("Sleep presence update failed: %s", e)

        # Hold a strong reference until the task finishes. asyncio keeps only
        # a weak reference to running tasks, so dropping this one on the floor
        # lets the GC collect it mid-await and the presence update never lands.
        # getattr/isinstance because tests bind this method onto a stub object.
        tasks = getattr(self, "_detached_tasks", None)
        task = loop.create_task(_run())
        if isinstance(tasks, set):
            tasks.add(task)
            task.add_done_callback(tasks.discard)

    async def _apply_sleep_presence(self, *, asleep: bool) -> None:
        """Idle overlay while sleeping; restore _current_status on wake.

        Uses change_presence so activity/custom status stay on the same
        stack. Overlay mode avoids writing idle into _current_status.
        """
        changer = getattr(self, "change_presence", None)
        if not callable(changer):
            return
        builder = getattr(self, "_build_activities", None)
        activities = builder() if callable(builder) else []
        self._sleep_presence_overlay = True
        try:
            activity = activities[0] if activities else None
            if asleep:
                await changer(
                    status=discord.Status.idle,
                    activity=activity,
                )
            else:
                status = getattr(self, "_current_status", None) or discord.Status.online
                await changer(
                    status=status,
                    activity=activity,
                )
        except Exception as e:  # noqa: BLE001
            logger.debug("Sleep presence update failed: %s", e)
        finally:
            self._sleep_presence_overlay = False

    def _format_sleep_remaining(self, seconds_remaining: int) -> str:
        """Format the 'back in Xm Ys' string. Always non-zero; if the
        window is <60s we show seconds, otherwise minutes."""
        if seconds_remaining >= 60:
            minutes = seconds_remaining // 60
            secs = seconds_remaining % 60
            if secs:
                return f"{minutes}m {secs}s"
            return f"{minutes}m"
        return f"{max(1, seconds_remaining)}s"

    async def _check_sleep_gate(self, message: Any) -> bool:
        """Returns True if the dispatch should proceed, False if the
        message should be swallowed by the sleep gate.

        When sleeping:
          - only a hard ping (DM, @, reply to him) gets the notice. A line
            that merely landed in a room he had been talking in was never
            asking him anything, so answering it with "max is sleeping" is
            him talking while asleep — the exact behaviour sleep exists to
            stop.
          - notify once per 5 minutes per user (so a long sleep
            doesn't spam one line per ping).
          - post the remaining-time notice in the triggering message's
            channel. Never DM. Never open a DM as fallback.
          - log the swallow at INFO so the audit trail shows why no
            LLM reply went out.
        """
        if not self._control.get("enable_sleep", True):
            return True
        sleeping, secs = self._is_sleeping()
        if not sleeping:
            return True
        await MaxwellBot._record_request_outcome(self, message, "suppressed", "sleep_policy")
        uid = str(getattr(message.author, "id", "") or "")
        if not self._directly_addressed(message):
            logger.info(
                "Sleep gate: silently dropped unaddressed message from uid=%s in "
                "channel=%s",
                uid,
                getattr(message.channel, "id", "?"),
            )
            return False
        # Re-notify cadence: once per 5 minutes per user. If a user
        # already got a 'sleeping' note recently, stay silent.
        if uid:
            now = asyncio.get_running_loop().time()
            # ``loop.time()`` is a monotonic clock that starts near zero at
            # boot, so a ``0.0`` "never notified" sentinel would read as
            # "notified moments ago" for the first five minutes after the
            # host comes up and the notice would be silently swallowed.
            # Use ``None`` for "never" instead.
            last = self._sleep_notified_at.get(uid)
            if last is not None and now - last < 300:  # 5 minutes
                return False
            self._sleep_notified_at[uid] = now
        remaining = self._format_sleep_remaining(secs)
        body = (
            f"max is sleeping rn, back in ~{remaining}. "
            "please ping me again when i'm awake."
        )
        with contextlib.suppress(Exception):
            await message.channel.send(
                body,
                reference=message if hasattr(message, "id") else None,
            )
        logger.info(
            "Sleep gate: dropped message from uid=%s in channel=%s (back in %s)",
            uid,
            getattr(message.channel, "id", "?"),
            remaining,
        )
        return False
