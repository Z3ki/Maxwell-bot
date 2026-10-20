import asyncio
import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from plugins.maxwell_extras.task_jobs import format_job
from plugins.schedules.cron import (
    CronError,
    next_after,
    parse,
    parse_at,
    upcoming,
    zone,
)
from plugins.schedules.runner import GRACE_SECONDS, ScheduleRunner
from plugins.schedules.store import ScheduleLimitError, ScheduleStore
from plugins.schedules.tool import ScheduleTool

UID = "111111111111111111"
UTC = timezone.utc
NOW = datetime(2026, 10, 20, 14, 37, tzinfo=UTC)


def _runs(expr, tz="UTC", count=3, after=NOW):
    zone_info = zone(tz)
    return [
        d.astimezone(zone_info).strftime("%m-%d %H:%M")
        for d in upcoming(parse(expr), after, zone_info, count)
    ]


def test_cron_basics_and_aliases():
    assert _runs("*/15 * * * *") == ["10-20 14:45", "10-20 15:00", "10-20 15:15"]
    assert _runs("0 9 * * 1-5", "America/New_York") == [
        "10-21 09:00",
        "10-22 09:00",
        "10-23 09:00",
    ]
    assert _runs("@weekly", count=1) == ["10-25 00:00"]
    assert _runs("0 8 * jan-mar MON", count=1) == ["01-04 08:00"]


def test_cron_day_fields_follow_vixie_rules():
    # Both restricted: either matches. 23rd and 30th are Fridays, Nov 1 is the 1st.
    assert _runs("0 0 1,15 * 5") == ["10-23 00:00", "10-30 00:00", "11-01 00:00"]
    # Stepped day-of-month with a starred weekday still filters days.
    assert _runs("0 0 */2 * *") == ["10-21 00:00", "10-23 00:00", "10-25 00:00"]


def test_cron_daylight_saving_edges():
    spring = datetime(2026, 3, 7, 12, tzinfo=UTC)
    assert _runs("30 2 * * *", "America/New_York", 2, spring) == [
        "03-09 02:30",
        "03-10 02:30",
    ]
    autumn = datetime(2026, 10, 31, 12, tzinfo=UTC)
    assert _runs("30 1 * * *", "America/New_York", 2, autumn) == [
        "11-01 01:30",
        "11-02 01:30",
    ]


@pytest.mark.parametrize(
    "bad", ["* * *", "60 * * * *", "5-1 * * * *", "@reboot", "a b c d e", "*/0 * * * *"]
)
def test_cron_rejects_garbage(bad):
    with pytest.raises(CronError):
        parse(bad)


def test_cron_that_never_fires():
    assert next_after(parse("0 0 30 2 *"), NOW, zone("UTC")) is None


def test_parse_at():
    assert parse_at("in 45m", zone("UTC"), NOW) == datetime(
        2026, 10, 20, 15, 22, tzinfo=UTC
    )
    assert parse_at("2026-10-21T09:00", zone("Europe/London"), NOW) == datetime(
        2026, 10, 21, 8, tzinfo=UTC
    )
    with pytest.raises(CronError):
        parse_at("tomorrowish", zone("UTC"), NOW)
    with pytest.raises(CronError):
        zone("Mars/Base")


def _store(tmp_path):
    return ScheduleStore(tmp_path / "schedules.sqlite3")


def _add(store, **overrides):
    values = {
        "user_id": UID,
        "name": "news",
        "kind": "cron",
        "spec": "0 9 * * *",
        "tz": "UTC",
        "prompt": "summarise news",
        "channel_id": "",
        "guild_id": "",
        "dm": True,
        "next_run": time.time() - 5,
    }
    values.update(overrides)
    return store.add(**values)


def test_store_scopes_by_owner_and_limits(tmp_path, monkeypatch):
    store = _store(tmp_path)
    row = _add(store)
    assert store.get("999", row["id"]) is None
    assert not store.delete("999", row["id"])
    monkeypatch.setenv("MAXWELL_SCHEDULE_MAX_PER_USER", "2")
    _add(store)
    with pytest.raises(ScheduleLimitError):
        _add(store)
    assert store.delete_user(UID) == 2
    assert store.list_user(UID) == []


def test_store_update_is_owner_scoped_and_whitelisted(tmp_path):
    store = _store(tmp_path)
    row = _add(store)
    assert store.update("999", row["id"], name="stolen") is None
    with pytest.raises(ValueError):
        store.update(UID, row["id"], channel_id="999")
    store.record(row["id"], status="boom", next_run=None, enabled=False, failed=True)
    paused = store.update(UID, row["id"], name="digest", enabled=False)
    assert (paused["name"], paused["enabled"], paused["failures"]) == (
        "digest",
        False,
        1,
    )
    resumed = store.update(UID, row["id"], enabled=True, next_run=123.0)
    assert (resumed["enabled"], resumed["failures"], resumed["next_run"]) == (
        True,
        0,
        123.0,
    )
    assert store.get(UID, row["id"]) == resumed
    assert store.get("999", row["id"]) is None


def test_store_record_counts_and_respects_mid_run_edits(tmp_path):
    store = _store(tmp_path)
    row = _add(store, next_run=100.0)
    assert store.record(
        row["id"],
        status="ok",
        next_run=200.0,
        enabled=True,
        ran=True,
        job="j1",
        expected_next=100.0,
    )
    done = store.get(UID, row["id"])
    assert (done["runs"], done["failures"], done["next_run"], done["last_job"]) == (
        1,
        0,
        200.0,
        "j1",
    )
    # The user moved it while the job ran; the runner must not stomp that.
    store.update(UID, row["id"], next_run=999.0)
    store.record(
        row["id"],
        status="late",
        next_run=300.0,
        enabled=False,
        failed=True,
        expected_next=200.0,
    )
    kept = store.get(UID, row["id"])
    assert (kept["next_run"], kept["enabled"], kept["failures"], kept["runs"]) == (
        999.0,
        True,
        1,
        1,
    )
    assert sorted(h["status"] for h in store.history(UID)) == ["late", "ok"]
    assert not store.record("missing", status="x", next_run=None, enabled=False)


class FakeJobs:
    def __init__(self, result=None):
        self.calls = []
        self.result = result or json.dumps({"id": "abcdef123456", "state": "queued"})

    async def submit(
        self, engine, message, tasks, timeout_minutes=None, *, header="", direct=False
    ):
        self.calls.append((message, tasks, header, direct))
        return self.result


def _bot(plan="premium", jobs=None):
    user = SimpleNamespace(id=int(UID), dm_channel=SimpleNamespace(id=4242))
    return SimpleNamespace(
        _message_quota=SimpleNamespace(plan_for=lambda uid: plan),
        _control={},
        tools={"delegate_task": object()},
        _task_jobs=jobs or FakeJobs(),
        get_user=lambda uid: user,
    )


def test_runner_starts_a_job_and_advances(tmp_path):
    store = _store(tmp_path)
    row = _add(store)
    bot = _bot()
    asyncio.run(ScheduleRunner(bot, store).tick())
    message, tasks, header, direct = bot._task_jobs.calls[0]
    assert direct and "news" in header and "<@" not in header
    assert "summarise news" in tasks[0]["task"]
    assert message.channel.id == 4242 and message.author.id == int(UID)
    saved = store.get(UID, row["id"])
    assert saved["runs"] == 1 and saved["last_job"] == "abcdef123456"
    assert saved["enabled"] and saved["next_run"] > time.time()


def test_runner_one_shot_disables_itself(tmp_path):
    store = _store(tmp_path)
    row = _add(store, kind="once", spec=datetime.now(UTC).isoformat())
    asyncio.run(ScheduleRunner(_bot(), store).tick())
    saved = store.get(UID, row["id"])
    assert saved["runs"] == 1 and not saved["enabled"] and saved["next_run"] is None


def test_runner_pauses_without_premium(tmp_path):
    store = _store(tmp_path)
    row = _add(store)
    bot = _bot(plan="free")
    asyncio.run(ScheduleRunner(bot, store).tick())
    saved = store.get(UID, row["id"])
    assert not bot._task_jobs.calls and not saved["enabled"]
    assert "Premium" in saved["last_status"]


def test_runner_waits_for_a_busy_worker_then_gives_up(tmp_path):
    store = _store(tmp_path)
    row = _add(store)
    busy = FakeJobs(
        "Error: your worker job is already running or workers are busy; continue chatting"
    )
    runner = ScheduleRunner(_bot(jobs=busy), store)
    asyncio.run(runner.tick())
    saved = store.get(UID, row["id"])
    assert saved["enabled"] and saved["next_run"] > time.time() + 60
    assert saved["last_status"].startswith("waiting")
    # Still busy past the grace window: skip this run, keep the schedule.
    runner._first_try[row["id"]] = time.time() - GRACE_SECONDS - 5
    asyncio.run(runner.fire(store.get(UID, row["id"]), time.time()))
    saved = store.get(UID, row["id"])
    assert (
        saved["last_status"].startswith("missed")
        and saved["enabled"]
        and saved["runs"] == 0
    )


def test_runner_daily_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("MAXWELL_SCHEDULE_DAILY_RUNS", "1")
    store = _store(tmp_path)
    first = _add(store)
    second = _add(store, name="other")
    bot = _bot()
    asyncio.run(ScheduleRunner(bot, store).tick())
    assert len(bot._task_jobs.calls) == 1
    statuses = {
        store.get(UID, first["id"])["last_status"],
        store.get(UID, second["id"])["last_status"],
    }
    assert any(status.startswith("skipped") for status in statuses)


def _message(guild=None, channel_id=77, private=False):
    return SimpleNamespace(
        author=SimpleNamespace(id=int(UID)),
        guild=guild,
        channel=SimpleNamespace(id=channel_id),
        response_visibility="private" if private else "public",
    )


def test_tool_create_list_edit_pause_delete(tmp_path):
    store = _store(tmp_path)
    tool = ScheduleTool(_bot(), store)

    def run(message=None, **kwargs):
        return asyncio.run(
            tool.execute(message or _message(guild=SimpleNamespace(id=5)), **kwargs)
        )

    created = run(
        action="create",
        name="digest",
        cron="0 9 * * 1-5",
        timezone="Europe/London",
        prompt="news digest",
    )
    assert created.startswith("Created schedule") and "<#77>" in created
    sid = created.split("`")[1]
    assert "digest" in run(action="list")
    assert "Updated" in run(action="edit", id=sid, cron="0 10 * * *")
    assert store.get(UID, sid)["spec"] == "0 10 * * *"
    assert run(action="pause", id=sid) == f"Paused `{sid}`."
    assert not store.get(UID, sid)["enabled"]
    assert run(action="resume", id=sid).startswith(f"Resumed `{sid}`")
    assert run(action="delete", id=sid).startswith("Deleted")
    assert run(action="list") == "No schedules."


def test_tool_validation_and_privacy_routing(tmp_path, monkeypatch):
    store = _store(tmp_path)
    tool = ScheduleTool(_bot(), store)

    def run(message, **kwargs):
        return asyncio.run(tool.execute(message, **kwargs))

    guild = _message(guild=SimpleNamespace(id=5))
    assert "at most once every 15 minutes" in run(
        guild, action="create", cron="*/5 * * * *", prompt="x"
    )
    assert "never fires" in run(guild, action="create", cron="0 0 30 2 *", prompt="x")
    assert "either cron" in run(
        guild, action="create", cron="@daily", at="in 1h", prompt="x"
    )
    assert "in the past" in run(
        guild, action="create", at="2020-01-01T00:00", prompt="x"
    )
    private = run(
        _message(guild=SimpleNamespace(id=5), private=True),
        action="create",
        at="in 2h",
        prompt="ping",
    )
    assert "your DMs" in private and "private or app context" in private
    free = ScheduleTool(_bot(plan="free"), store)
    refused = asyncio.run(
        free.execute(guild, action="create", cron="@daily", prompt="x")
    )
    assert refused.startswith("Error:") and "do not mention prices" in refused.lower()


def test_direct_job_report_is_posted_whole():
    report = "Here is today's digest.\n\n- one\n- two (details)"
    row = {
        "id": "j",
        "state": "completed",
        "direct": True,
        "reports": [{"report": report}],
    }
    assert format_job(row) == report
    normal = {"id": "j", "state": "completed", "reports": [{"report": report}]}
    assert "details" not in format_job(normal)
