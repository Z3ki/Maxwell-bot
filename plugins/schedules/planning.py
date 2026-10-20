"""Validation shared by the schedule tool (bot) and the dashboard (API).

No discord import here on purpose: the API process loads this too, and
dragging the whole client library in to check a cron string is silly.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .cron import CronError, describe, next_after, parse, parse_at, shortest_gap, zone
from .store import min_interval_minutes

PROMPT_LIMIT = 2000
NAME_LIMIT = 60
AT_HORIZON = timedelta(days=366)


def plan_cron(spec: str, tz_name: str, now: datetime) -> tuple[str, float]:
    """Validate a cron schedule; returns (normalised text, first run ts)."""
    cron = parse(spec)
    tz = zone(tz_name)
    first = next_after(cron, now, tz)
    if first is None:
        raise CronError("that cron expression never fires")
    gap = shortest_gap(cron, now, tz)
    floor = min_interval_minutes()
    if gap is not None and gap < timedelta(minutes=floor):
        raise CronError(f"schedules may run at most once every {floor} minutes")
    return cron.text, first.timestamp()


def plan_at(text: str, tz_name: str, now: datetime) -> tuple[str, float]:
    tz = zone(tz_name)
    moment = parse_at(text, tz, now)
    if moment < now + timedelta(seconds=30):
        raise CronError("that time is in the past")
    if moment > now + AT_HORIZON:
        raise CronError("one-time runs can be at most a year ahead")
    return moment.isoformat(), moment.timestamp()


def next_run(row: dict, now: datetime) -> float | None:
    """When a paused schedule would fire next if resumed now."""
    if row["kind"] == "cron":
        found = next_after(parse(row["spec"]), now, zone(row["tz"]))
        return found.timestamp() if found else None
    when = datetime.fromisoformat(row["spec"]).timestamp()
    return when if when > now.timestamp() else None


def summary(row: dict) -> str:
    """Plain-text 'when' for web pages; the tool uses Discord timestamps."""
    if row["kind"] == "cron":
        return f"{describe(parse(row['spec']))} ({row['tz']})"
    return f"once at {row['spec']} ({row['tz']})"
