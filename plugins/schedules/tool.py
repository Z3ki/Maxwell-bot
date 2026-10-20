"""schedule: Premium users (and Maxwell on their behalf) set wake-ups."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import ClassVar

import discord

from tools import Tool

from .cron import CronError, describe, next_after, parse, zone
from .planning import NAME_LIMIT as _NAME_LIMIT
from .planning import PROMPT_LIMIT as _PROMPT_LIMIT
from .planning import plan_at, plan_cron
from .store import ScheduleLimitError, ScheduleStore, daily_run_limit, max_per_user, min_interval_minutes


def _inside_worker() -> bool:
    try:
        from plugins.maxwell_extras.task_workers import _inside_worker as flag
    except Exception:
        return False
    return bool(flag.get())


def _premium(bot, uid: str) -> bool:
    try:
        from plugins.maxwell_extras.task_workers import has_premium
    except Exception:
        return False
    return has_premium(bot, uid)


def _when(ts: float | None) -> str:
    if not ts:
        return "not scheduled"
    value = int(ts)
    return f"<t:{value}:F> (<t:{value}:R>)"


def target_for(message, deliver: str) -> tuple[bool, str, str, str]:
    """(dm, channel_id, guild_id, note). App-command contexts always use DMs."""
    channel = getattr(message, "channel", None)
    guild = getattr(message, "guild", None)
    private = getattr(message, "response_visibility", "public") == "private"
    app = bool(getattr(message, "user_install", False))
    if deliver == "dm" or isinstance(channel, discord.DMChannel) or guild is None:
        return True, "", "", ""
    if private or app:
        return True, "", "", " Results go to your DMs because this request came from a private or app context."
    return False, str(getattr(channel, "id", "") or ""), str(getattr(guild, "id", "") or ""), ""


class ScheduleTool(Tool):
    tool_name = "schedule"
    returns_result = True
    ends_turn = False
    is_destructive = False
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["create", "list", "edit", "pause", "resume", "delete"]},
            "id": {"type": "string", "description": "Schedule id for edit/pause/resume/delete"},
            "name": {"type": "string", "description": "Short label, e.g. 'morning news'"},
            "cron": {
                "type": "string",
                "description": "Repeating: 5-field cron 'min hour day month weekday' or @hourly/@daily/@weekly/@monthly",
            },
            "at": {
                "type": "string",
                "description": "One time: ISO 8601 local time like 2026-10-21T09:00, or relative like 'in 45m'",
            },
            "timezone": {"type": "string", "description": "IANA zone, e.g. America/New_York. Default UTC"},
            "prompt": {"type": "string", "description": "What Maxwell should do when it wakes up"},
            "deliver": {"type": "string", "enum": ["here", "dm"], "description": "Where results are posted"},
        },
        "required": ["action"],
        "additionalProperties": False,
    }

    def __init__(self, bot, store: ScheduleStore):
        super().__init__(bot)
        self.store = store

    def limits_for(self, user_id: str) -> dict:
        """Read-only schedule limits for get_configuration."""
        return {
            "schedules": {
                "available": _premium(self.bot, str(user_id)),
                "max_per_user": max_per_user(),
                "min_interval_minutes": min_interval_minutes(),
                "runs_per_day": daily_run_limit(),
            }
        }

    def get_description(self) -> str:
        return (
            "Premium wake-ups: Maxwell runs a prompt on a schedule as a background job and posts the "
            "result to the user (here or in DMs). Use for recurring checks, digests, site updates and "
            "one-time follow-ups. create needs prompt plus cron (repeating) or at (one time); pass the "
            "user's timezone (ask if unknown, default UTC). The prompt runs later without this chat, so "
            "make it self-contained: say exactly what to do and what to report. list shows ids, next "
            "runs and last results; edit/pause/resume/delete take id. "
            f"Limits: {max_per_user()} schedules per user, at most once every {min_interval_minutes()} "
            f"minutes, {daily_run_limit()} runs a day. Wake-ups use background worker tools "
            "(shell, web, sites, storage, GitHub), not chat tools. Results contain Discord <t:...> "
            "timestamps; pass them through unchanged."
        )

    async def execute(self, message, action: str = "list", id: str | None = None, name: str | None = None,
                      cron: str | None = None, at: str | None = None, timezone: str | None = None,
                      prompt: str | None = None, deliver: str | None = None, **kwargs) -> str:
        author = getattr(message, "author", None)
        uid = str(getattr(author, "id", "") or "")
        if not uid.isdigit():
            return "Error: schedules need an authenticated Discord user"
        if _inside_worker():
            return "Error: background workers cannot create or change schedules"
        action = str(action or "list").lower()
        # Anyone can see, stop or remove what they have; making or reviving needs Premium.
        if action not in {"list", "pause", "delete"} and not await asyncio.to_thread(_premium, self.bot, uid):
            return (
                "Error: schedules come with an operator-granted Premium plan; this user does not have it. "
                "Do not mention prices or ask them to upgrade."
            )
        try:
            return await self._run(message, uid, action, id, name, cron, at, timezone, prompt, deliver)
        except (CronError, ScheduleLimitError) as exc:
            return f"Error: {exc}"

    async def _run(self, message, uid, action, sid, name, cron, at, tz, prompt, deliver) -> str:
        now = datetime.now(timezone.utc)
        if action == "list":
            rows = await asyncio.to_thread(self.store.list_user, uid)
            if not rows:
                return "No schedules."
            return "\n".join(self._line(row) for row in rows)
        if action == "create":
            if bool(cron) == bool(at):
                return "Error: give either cron (repeating) or at (one time)"
            text = " ".join(str(prompt or "").split()) if prompt else ""
            if not text:
                return "Error: prompt is required: what should Maxwell do when it wakes up?"
            if len(str(prompt)) > _PROMPT_LIMIT:
                return f"Error: prompt must be under {_PROMPT_LIMIT} characters"
            tz_name = str(tz or "UTC").strip() or "UTC"
            zone(tz_name)
            kind = "cron" if cron else "once"
            spec, first = plan_cron(cron, tz_name, now) if cron else plan_at(at, tz_name, now)
            dm, channel_id, guild_id, note = target_for(message, str(deliver or "here").lower())
            label = " ".join(str(name or "").split())[:_NAME_LIMIT] or text[:40]
            row = await asyncio.to_thread(
                self.store.add, user_id=uid, name=label, kind=kind, spec=spec, tz=tz_name,
                prompt=str(prompt).strip(), channel_id=channel_id, guild_id=guild_id, dm=dm,
                next_run=first,
            )
            where = "in your DMs" if dm else f"in <#{channel_id}>"
            return (
                f"Created schedule `{row['id']}` \"{label}\" ({self._spec(row)}). "
                f"Next run {_when(first)}; results post {where}.{note}"
            )
        if not sid:
            return "Error: id is required"
        row = await asyncio.to_thread(self.store.get, uid, sid)
        if row is None:
            return "Error: no schedule with that id"
        if action == "delete":
            await asyncio.to_thread(self.store.delete, uid, sid)
            return f"Deleted schedule `{sid}` \"{row['name']}\"."
        if action == "pause":
            await asyncio.to_thread(self.store.update, uid, sid, enabled=False, last_status="paused by user")
            return f"Paused `{sid}`."
        if action == "resume":
            first = self._next(row, now)
            if first is None:
                return "Error: that one-time run is in the past; edit it with a new at"
            await asyncio.to_thread(
                self.store.update, uid, sid, enabled=True, next_run=first, last_status="resumed"
            )
            return f"Resumed `{sid}`. Next run {_when(first)}."
        if action == "edit":
            changes: dict = {}
            tz_name = str(tz or row["tz"]).strip() or "UTC"
            zone(tz_name)
            if cron and at:
                return "Error: give either cron or at, not both"
            if cron:
                changes["kind"], (changes["spec"], changes["next_run"]) = "cron", plan_cron(cron, tz_name, now)
            elif at:
                changes["kind"], (changes["spec"], changes["next_run"]) = "once", plan_at(at, tz_name, now)
            elif tz and row["kind"] == "cron":
                changes["spec"], changes["next_run"] = plan_cron(row["spec"], tz_name, now)
            if tz:
                changes["tz"] = tz_name
            if prompt is not None:
                if not str(prompt).strip() or len(str(prompt)) > _PROMPT_LIMIT:
                    return f"Error: prompt must be 1-{_PROMPT_LIMIT} characters"
                changes["prompt"] = str(prompt).strip()
            if name:
                changes["name"] = " ".join(str(name).split())[:_NAME_LIMIT]
            if not changes:
                return "Error: nothing to change"
            if "next_run" in changes:
                changes["enabled"] = True
            updated = await asyncio.to_thread(self.store.update, uid, sid, **changes)
            return "Updated. " + self._line(updated)
        return "Error: unknown action"

    @staticmethod
    def _spec(row: dict) -> str:
        if row["kind"] == "cron":
            return f"cron {describe(parse(row['spec']))} {row['tz']}"
        return f"once at {_when(datetime.fromisoformat(row['spec']).timestamp())}"

    @staticmethod
    def _next(row: dict, now: datetime) -> float | None:
        if row["kind"] == "cron":
            found = next_after(parse(row["spec"]), now, zone(row["tz"]))
            return found.timestamp() if found else None
        when = datetime.fromisoformat(row["spec"]).timestamp()
        return when if when > time.time() else None

    def _line(self, row: dict) -> str:
        state = "on" if row["enabled"] else "off"
        where = "DM" if row["dm"] else f"<#{row['channel_id']}>"
        last = f" · last: {row['last_status']}" if row.get("last_status") else ""
        nxt = f" · next {_when(row['next_run'])}" if row["enabled"] else ""
        prompt = row["prompt"] if len(row["prompt"]) <= 120 else row["prompt"][:117] + "…"
        return (
            f"`{row['id']}` [{state}] \"{row['name']}\" {self._spec(row)} → {where}{nxt}{last}\n"
            f"    prompt: {prompt}"
        )
