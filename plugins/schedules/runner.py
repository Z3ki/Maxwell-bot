"""Fires due schedules as detached background jobs.

A wake-up is a delegate_task job started on the owner's behalf: same Premium
check, same worker tools and budget, same /tasks cancel, same file keeping
and delivery. The only new thing is a fake "message" so TaskJobs knows who
and where; it has no reply target, so the result is a plain channel post.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone

import discord

from .cron import CronError, next_after, parse, zone
from .store import ScheduleStore, daily_run_limit

logger = logging.getLogger(__name__)

# Bot was down or the user's worker slot stayed busy this long: skip the run
# rather than fire a "good morning" at 3pm.
GRACE_SECONDS = 3600
RETRY_SECONDS = 120
CHANNEL_RETRY_SECONDS = 600
MAX_FAILURES = 5


class Unavailable(Exception):
    def __init__(self, reason: str, *, permanent: bool):
        super().__init__(reason)
        self.reason = reason
        self.permanent = permanent


def delayed_job_policy_reason(bot, message) -> str:
    """Policy can change between creating a schedule and firing it.

    Same delayed-turn gates as queued chat: a later blacklist, ignore list,
    blocked/allowed channel, or solo restriction must stop the job. Mention
    and DM reply flags stay out of this — a schedule is not a mention reply.
    """
    control = getattr(bot, "_control", None) or {}
    author = getattr(message, "author", None)
    uid = str(getattr(author, "id", "") or "")
    is_admin = getattr(bot, "_is_admin", None)
    admin = bool(
        uid and callable(is_admin) and is_admin(getattr(author, "id", uid))
    )
    if uid and not admin:
        if uid in set(getattr(bot, "_blacklist", None) or ()):
            return "ignored_author"
        ignored = {str(item) for item in (control.get("ignore_users") or [])}
        if uid in ignored:
            return "ignored_author"
    cid = str(getattr(getattr(message, "channel", None), "id", "") or "")
    blocked = {str(item) for item in (control.get("blocked_channels") or [])}
    if cid in blocked:
        return "blocked_channel"
    if getattr(message, "guild", None) is not None:
        allowed = {str(item) for item in (control.get("allowed_channels") or [])}
        if allowed and cid and cid not in allowed:
            return "channel_not_allowed"
        solo = getattr(bot, "_solo_blocks", None)
        if callable(solo) and solo(message):
            return "solo_restriction"
    return ""


class ScheduledMessage:
    """Who and where for a scheduled job. Not a Discord message; never replied to."""

    def __init__(self, *, author, channel, guild, schedule_id: str):
        now = datetime.now(timezone.utc)
        self.author = author
        self.channel = channel
        self.guild = guild
        self.guild_id = getattr(guild, "id", None)
        try:
            low = int(schedule_id, 16) & 0x3FFFFF
        except ValueError:
            low = 0
        # Shaped like a snowflake so anything that int()s an id survives,
        # unique per schedule per millisecond so job dedup never merges runs.
        self.id = discord.utils.time_snowflake(now) | low
        self.created_at = now
        self.content = ""
        self.clean_content = ""
        self.attachments = []
        self.embeds = []
        self.stickers = []
        self.mentions = []
        self.role_mentions = []
        self.channel_mentions = []
        self.mention_everyone = False
        self.reference = None
        self.webhook_id = None
        self.tts = False
        self.type = discord.MessageType.default
        self.jump_url = ""
        self.response_visibility = "public"
        self.user_install = False
        self.scheduled_run = schedule_id


def task_text(row: dict, now: float) -> str:
    tz = zone(row["tz"])
    local = datetime.fromtimestamp(now, timezone.utc).astimezone(tz)
    when = f"cron '{row['spec']}'" if row["kind"] == "cron" else "one time"
    return (
        f"Scheduled wake-up \"{row['name']}\" that the user set up earlier ({when}, {row['tz']}). "
        f"It is now {local:%A %Y-%m-%d %H:%M} {row['tz']}. Do the instructions below now. "
        "Your final message is posted to the user in chat exactly as written, so write it to them "
        "directly, keep it under 1500 characters and leave out working notes. If there is nothing "
        "worth reporting, say so in one line.\n\nInstructions from the user:\n" + row["prompt"]
    )


def advance(row: dict, now: float) -> float | None:
    if row["kind"] != "cron":
        return None
    try:
        found = next_after(parse(row["spec"]), datetime.fromtimestamp(now, timezone.utc), zone(row["tz"]))
    except CronError:
        return None
    return found.timestamp() if found else None


class ScheduleRunner:
    def __init__(self, bot, store: ScheduleStore):
        self.bot = bot
        self.store = store
        self._running = False

    async def tick(self) -> None:
        if self._running:
            return
        self._running = True
        try:
            now = time.time()
            for row in await asyncio.to_thread(self.store.due, now):
                try:
                    await self.fire(row, now)
                except Exception:
                    logger.exception("schedule %s failed to fire", row.get("id"))
        finally:
            self._running = False

    async def _finish(self, row: dict, now: float, status: str, *, ran=False, job="", failed=False,
                      pause=False) -> None:
        next_run = None if pause else advance(row, now)
        if not pause and failed and row["failures"] + 1 >= MAX_FAILURES:
            next_run, status = None, f"paused after {MAX_FAILURES} failures: {status}"
        await asyncio.to_thread(
            self.store.record, row["id"], status=status, next_run=next_run,
            enabled=next_run is not None, ran=ran, job=job, failed=failed,
            expected_next=row["next_run"], claim_token=row["claim_token"],
        )

    async def fire(self, row: dict, now: float) -> None:
        claimed = await asyncio.to_thread(
            self.store.claim, row, now, GRACE_SECONDS + CHANNEL_RETRY_SECONDS
        )
        if claimed is None:
            return
        try:
            await self._fire(claimed, now)
        except Exception:
            await asyncio.to_thread(
                self.store.retry_later, claimed, now + CHANNEL_RETRY_SECONDS, "error: will retry"
            )
            raise

    async def _fire(self, row: dict, now: float) -> None:
        from plugins.maxwell_extras.task_workers import has_premium

        uid = row["user_id"]
        first = float(row["retry_since"])
        control = getattr(self.bot, "_control", None) or {}
        if not await asyncio.to_thread(has_premium, self.bot, uid):
            await self._finish(row, now, "paused: Premium is not active", pause=True)
            return
        if now - first > GRACE_SECONDS:
            await self._finish(row, now, "missed: Maxwell was offline or busy at the scheduled time")
            return
        if not control.get("bot_enabled", True) or not control.get("premium_task_workers_enabled", True):
            await asyncio.to_thread(
                self.store.retry_later, row, now + CHANNEL_RETRY_SECONDS,
                "retrying: background runs are switched off right now",
            )
            return
        used = await asyncio.to_thread(self.store.runs_since, uid, now - 86400)
        if used >= daily_run_limit():
            await self._finish(row, now, f"skipped: {daily_run_limit()} runs in 24 hours is the limit")
            return
        manager = getattr(self.bot, "_task_jobs", None)
        engine = (getattr(self.bot, "tools", None) or {}).get("delegate_task")
        if manager is None or engine is None:
            await self._finish(row, now, "skipped: background workers are unavailable", failed=True)
            return
        try:
            message = await self._message(row)
        except Unavailable as exc:
            if exc.permanent or row["failures"] + 1 >= MAX_FAILURES:
                await self._finish(row, now, f"paused: {exc.reason}", failed=True, pause=True)
            else:
                await asyncio.to_thread(
                    self.store.retry_later, row, now + CHANNEL_RETRY_SECONDS,
                    f"retrying: {exc.reason}", failed=True,
                )
            return
        reason = delayed_job_policy_reason(self.bot, message)
        if reason:
            await self._finish(row, now, f"paused: {reason}", pause=True)
            return

        scope = getattr(self.bot, "_dev_scope_allows", None)
        if callable(scope) and not scope(message):
            await self._finish(row, now, "paused: this bot only runs in its test servers", pause=True)
            return
        if not await asyncio.to_thread(self.store.claimed, row):
            return
        header = (
            f"⏰ **{row['name']}**" if row["dm"] else f"⏰ <@{uid}> **{row['name']}**"
        )
        result = await manager.submit(
            engine, message, [{"task": task_text(row, now)}], header=header, direct=True
        )
        try:
            data = json.loads(result)
        except (TypeError, ValueError):
            data = None
        if isinstance(data, dict) and data.get("id"):
            await self._finish(row, now, f"started job {data['id']}", ran=True, job=str(data["id"]))
            return
        text = str(result or "")
        if "already running or workers are busy" in text:
            # One job per user at a time; wait for theirs to finish.
            await asyncio.to_thread(
                self.store.retry_later, row, now + RETRY_SECONDS, "waiting: another background job is running"
            )
            return
        await self._finish(row, now, text[:180] or "error: the job did not start", failed=True)

    async def _message(self, row: dict) -> ScheduledMessage:
        bot = self.bot
        uid = int(row["user_id"])
        user = bot.get_user(uid)
        if user is None:
            try:
                user = await bot.fetch_user(uid)
            except discord.NotFound as exc:
                raise Unavailable("the account no longer exists", permanent=True) from exc
            except discord.HTTPException as exc:
                raise Unavailable("Discord did not answer", permanent=False) from exc
        if row["dm"]:
            try:
                channel = user.dm_channel or await user.create_dm()
            except discord.HTTPException as exc:
                raise Unavailable("could not open a DM", permanent=False) from exc
            return ScheduledMessage(author=user, channel=channel, guild=None, schedule_id=row["id"])
        cid = int(row["channel_id"])
        channel = bot.get_channel(cid)
        if channel is None:
            try:
                channel = await bot.fetch_channel(cid)
            except (discord.NotFound, discord.Forbidden) as exc:
                raise Unavailable("the channel is gone or Maxwell lost access", permanent=True) from exc
            except discord.HTTPException as exc:
                raise Unavailable("Discord did not answer", permanent=False) from exc
        guild = getattr(channel, "guild", None)
        if guild is None:
            return ScheduledMessage(author=user, channel=channel, guild=None, schedule_id=row["id"])
        member = guild.get_member(uid)
        if member is None:
            try:
                member = await guild.fetch_member(uid)
            except discord.NotFound as exc:
                raise Unavailable("you are no longer in that server", permanent=True) from exc
            except discord.HTTPException as exc:
                raise Unavailable("Discord did not answer", permanent=False) from exc
        if not channel.permissions_for(member).view_channel:
            raise Unavailable("you can no longer see that channel", permanent=True)
        if isinstance(channel, discord.Thread) and channel.is_private():
            # Thread permissions inherit the parent's view_channel bit; that
            # bit alone does not grant access to a private thread.
            if not channel.permissions_for(member).manage_threads and channel.get_member(uid) is None:
                try:
                    await channel.fetch_member(uid)
                except (discord.NotFound, discord.Forbidden) as exc:
                    raise Unavailable("you can no longer see that private thread", permanent=True) from exc
                except discord.HTTPException as exc:
                    raise Unavailable("Discord did not answer", permanent=False) from exc
        me = getattr(guild, "me", None)
        if me is not None:
            mine = channel.permissions_for(me)
            can_send = (
                mine.send_messages_in_threads if isinstance(channel, discord.Thread) else mine.send_messages
            )
            if not (mine.view_channel and can_send):
                raise Unavailable("Maxwell cannot post in that channel", permanent=True)
        return ScheduledMessage(author=member, channel=channel, guild=guild, schedule_id=row["id"])
