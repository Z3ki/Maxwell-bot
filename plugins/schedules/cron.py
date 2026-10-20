"""Five-field cron, by hand, because croniter isn't in the image.

Standard Vixie semantics: minute hour day-of-month month day-of-week, lists,
ranges, steps, month/day names, 0 and 7 both Sunday, and the usual @aliases.
When both day fields are restricted a day matches if EITHER does (yes, that's
what cron does, and yes, everyone gets it wrong once).

Times are evaluated as wall-clock time in an IANA zone. A wall time that the
spring-forward gap skips never fires that day; the repeated hour in autumn
fires once (first pass). Same as cronie, minus the surprise.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_ALIASES = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
}
_MONTHS = {name: i + 1 for i, name in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
)}
_DAYS = {name: i for i, name in enumerate(["sun", "mon", "tue", "wed", "thu", "fri", "sat"])}
_BOUNDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
_NAMES = (None, None, None, _MONTHS, _DAYS)
_LABELS = ("minute", "hour", "day of month", "month", "day of week")
# Far enough to find Feb 29 on a Monday-or-whatever; anything later is "never".
_HORIZON_DAYS = 366 * 8


class CronError(ValueError):
    """Shown to the model verbatim, so keep it useful."""


@dataclass(frozen=True)
class Cron:
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    days_any: bool
    weekdays_any: bool
    text: str

    def day_matches(self, moment: datetime) -> bool:
        dom = moment.day in self.days
        dow = (moment.isoweekday() % 7) in self.weekdays
        # Vixie: either field starring means AND (the starred set is full or
        # stepped, so it still filters); both restricted means OR.
        if self.days_any or self.weekdays_any:
            return dom and dow
        return dom or dow


def _value(token: str, index: int) -> int:
    names = _NAMES[index]
    lowered = token.lower()
    if names and lowered in names:
        return names[lowered]
    if not token.isdigit():
        raise CronError(f"'{token}' is not valid in the {_LABELS[index]} field")
    return int(token)


def _field(text: str, index: int) -> frozenset[int]:
    low, high = _BOUNDS[index]
    out: set[int] = set()
    for part in text.split(","):
        if not part:
            raise CronError(f"empty list item in the {_LABELS[index]} field")
        base, _, step_text = part.partition("/")
        step = 1
        if step_text:
            if not step_text.isdigit() or int(step_text) < 1:
                raise CronError(f"bad step '/{step_text}' in the {_LABELS[index]} field")
            step = int(step_text)
        if base == "*":
            start, end = low, high
            if index == 4:
                end = 6
        elif "-" in base:
            left, _, right = base.partition("-")
            start, end = _value(left, index), _value(right, index)
        else:
            start = _value(base, index)
            # "5/15" means 5,20,35,50 in cron; a bare "5" is just 5.
            end = high if step_text else start
        if not (low <= start <= high and low <= end <= high):
            raise CronError(f"{_LABELS[index]} must be {low}-{high}")
        if end < start:
            raise CronError(f"range {base} runs backwards in the {_LABELS[index]} field")
        out.update(range(start, end + 1, step))
    if index == 4 and 7 in out:
        out.discard(7)
        out.add(0)
    return frozenset(out)


def parse(expression: str) -> Cron:
    text = " ".join(str(expression or "").split()).lower()
    text = _ALIASES.get(text, text)
    if text.startswith("@"):
        raise CronError(f"unknown alias {text}; use @hourly, @daily, @weekly, @monthly or @yearly")
    parts = text.split(" ")
    if len(parts) != 5:
        raise CronError(
            "cron needs 5 fields: minute hour day-of-month month day-of-week "
            "(e.g. '0 9 * * 1-5' is 09:00 on weekdays)"
        )
    if not re.fullmatch(r"[a-z0-9*,/\- ]+", text):
        raise CronError("cron may only use digits, names, * , - and /")
    fields = [_field(part, index) for index, part in enumerate(parts)]
    # Vixie treats any day field starting with '*' (even */2) as unrestricted
    # for the either-day-matches rule.
    return Cron(
        minutes=fields[0],
        hours=fields[1],
        days=fields[2],
        months=fields[3],
        weekdays=fields[4],
        days_any=parts[2].startswith("*"),
        weekdays_any=parts[4].startswith("*"),
        text=text,
    )


def zone(name: str | None) -> ZoneInfo:
    value = str(name or "UTC").strip() or "UTC"
    if value.upper() in {"UTC", "Z", "GMT"}:
        return ZoneInfo("UTC")
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise CronError(f"unknown timezone '{value}'; use an IANA name like Europe/London") from exc


def _exists(local: datetime, tz: ZoneInfo) -> bool:
    aware = local.replace(tzinfo=tz)
    back = aware.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None)
    return back == local


def next_after(cron: Cron, after: datetime, tz: ZoneInfo) -> datetime | None:
    """First fire time strictly after ``after`` (aware), as an aware UTC datetime."""
    local = after.astimezone(tz).replace(tzinfo=None, second=0, microsecond=0) + timedelta(minutes=1)
    horizon = local + timedelta(days=_HORIZON_DAYS)
    while local < horizon:
        if local.month not in cron.months:
            year, month = (local.year + 1, 1) if local.month == 12 else (local.year, local.month + 1)
            local = datetime(year, month, 1)
            continue
        if not cron.day_matches(local):
            local = datetime(local.year, local.month, local.day) + timedelta(days=1)
            continue
        if local.hour not in cron.hours:
            local = local.replace(minute=0) + timedelta(hours=1)
            continue
        if local.minute not in cron.minutes:
            later = [m for m in cron.minutes if m > local.minute]
            if later:
                local = local.replace(minute=min(later))
            else:
                local = local.replace(minute=0) + timedelta(hours=1)
            continue
        if _exists(local, tz):
            return local.replace(tzinfo=tz).astimezone(timezone.utc)
        local += timedelta(minutes=1)
    return None


def upcoming(cron: Cron, after: datetime, tz: ZoneInfo, count: int) -> list[datetime]:
    out: list[datetime] = []
    moment = after
    for _ in range(count):
        found = next_after(cron, moment, tz)
        if found is None:
            break
        out.append(found)
        moment = found
    return out


def shortest_gap(cron: Cron, after: datetime, tz: ZoneInfo, samples: int = 60) -> timedelta | None:
    """Smallest spacing between the next ``samples`` runs. None = fewer than two runs."""
    runs = upcoming(cron, after, tz, samples)
    if len(runs) < 2:
        return None
    return min(b - a for a, b in zip(runs, runs[1:]))


_RELATIVE = re.compile(r"^\+?\s*(?:in\s+)?(\d{1,6})\s*(m|min|mins|minutes?|h|hr|hrs|hours?|d|days?)$")


def parse_at(text: str, tz: ZoneInfo, now: datetime) -> datetime:
    """One-shot time: 'in 30m', '+2h', '1d', or ISO 8601 (local to tz if naive)."""
    raw = " ".join(str(text or "").split()).lower()
    match = _RELATIVE.match(raw)
    if match:
        amount, unit = int(match[1]), match[2][0]
        delta = {"m": timedelta(minutes=amount), "h": timedelta(hours=amount), "d": timedelta(days=amount)}[unit]
        return (now + delta).astimezone(timezone.utc).replace(second=0, microsecond=0)
    try:
        moment = datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise CronError(
            "at must be ISO 8601 like 2026-10-21T09:00 (in the schedule's timezone) or relative like 'in 45m'"
        ) from exc
    if moment.tzinfo is None:
        if not _exists(moment, tz):
            raise CronError("that local time does not exist (daylight-saving gap)")
        moment = moment.replace(tzinfo=tz)
    return moment.astimezone(timezone.utc)


def describe(cron: Cron) -> str:
    """Short human hint; the expression itself stays the source of truth."""
    reverse = {v: k for k, v in _ALIASES.items() if k not in {"@midnight", "@annually"}}
    return reverse.get(cron.text, cron.text)
