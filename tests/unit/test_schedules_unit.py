import asyncio
import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plugins.maxwell_extras.task_jobs import format_job
from plugins.schedules.cron import (
    CronError,
    next_after,
    parse,
    parse_at,
    shortest_gap,
    upcoming,
    zone,
)
from plugins.schedules.planning import plan_at, plan_cron
from plugins.schedules.runner import (
    GRACE_SECONDS,
    ScheduleRunner,
    Unavailable,
    delayed_job_policy_reason,
)
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


def test_spring_forward_compression_is_below_the_interval_floor():
    now = datetime(2026, 1, 15, 12, tzinfo=UTC)
    with pytest.raises(CronError, match="once every"):
        plan_cron("0,50 1,3 * * *", "America/New_York", now)


def test_shortest_gap_finds_spring_forward_without_walking_years():
    now = datetime(2026, 1, 15, 12, tzinfo=UTC)
    started = time.perf_counter()
    gap = shortest_gap(parse("0,50 1,3 * * *"), now, zone("America/New_York"))
    assert time.perf_counter() - started < 1
    assert gap is not None
    assert gap < timedelta(minutes=15)


def test_fall_back_second_pass_stays_in_the_future():
    # 2026-11-01 06:10 UTC is 01:10 EST, the second pass of the repeated hour.
    tz = zone("America/New_York")
    now = datetime(2026, 11, 1, 6, 10, tzinfo=UTC)
    assert next_after(parse("*/15 * * * *"), now, tz) == datetime(
        2026, 11, 1, 7, tzinfo=UTC
    )
    assert next_after(parse("30 1 * * *"), now, tz) == datetime(
        2026, 11, 2, 6, 30, tzinfo=UTC
    )
    # The first pass still fires while that wall time is ahead.
    first = datetime(2026, 11, 1, 5, 10, tzinfo=UTC)
    assert next_after(parse("30 1 * * *"), first, tz) == datetime(
        2026, 11, 1, 5, 30, tzinfo=UTC
    )
    runs = upcoming(parse("*/15 * * * *"), now, tz, 4)
    assert runs[0] > now
    assert all(later > earlier for earlier, later in zip(runs, runs[1:]))
    text, stamp = plan_cron("30 1 * * *", "America/New_York", now)
    assert text == "30 1 * * *"
    assert stamp > now.timestamp()


@pytest.mark.parametrize(
    "bad",
    [
        "* * *",
        "60 * * * *",
        "5-1 * * * *",
        "@reboot",
        "a b c d e",
        "*/0 * * * *",
        "*/ * * * *",
        "5/ * * * *",
        "0 9 1-5/ * *",
    ],
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


def test_delayed_job_policy_reason_matches_queued_chat_gates():
    author = SimpleNamespace(id=int(UID))
    channel = SimpleNamespace(id=99)
    guild_message = SimpleNamespace(
        author=author, channel=channel, guild=SimpleNamespace(id=1)
    )
    dm = SimpleNamespace(author=author, channel=channel, guild=None)
    deny = SimpleNamespace(_control={}, _blacklist={UID}, _is_admin=lambda _uid: False)
    assert delayed_job_policy_reason(deny, guild_message) == "ignored_author"
    deny._blacklist = set()
    deny._control = {"ignore_users": [UID]}
    assert delayed_job_policy_reason(deny, guild_message) == "ignored_author"
    deny._control = {"blocked_channels": ["99"]}
    assert delayed_job_policy_reason(deny, guild_message) == "blocked_channel"
    deny._control = {"allowed_channels": ["1"]}
    assert delayed_job_policy_reason(deny, guild_message) == "channel_not_allowed"
    deny._control = {}
    deny._solo_blocks = lambda _msg: True
    assert delayed_job_policy_reason(deny, guild_message) == "solo_restriction"
    assert delayed_job_policy_reason(deny, dm) == ""
    admin = SimpleNamespace(
        _control={"ignore_users": [UID]},
        _blacklist={UID},
        _is_admin=lambda _uid: True,
    )
    assert delayed_job_policy_reason(admin, guild_message) == ""


def test_runner_pauses_when_owner_is_later_blacklisted(tmp_path):
    store = _store(tmp_path)
    row = _add(store)
    bot = _bot()
    bot._blacklist = {UID}
    asyncio.run(ScheduleRunner(bot, store).tick())
    assert bot._task_jobs.calls == []
    saved = store.get(UID, row["id"])
    assert not saved["enabled"]
    assert saved["last_status"] == "paused: ignored_author"
    assert saved["runs"] == 0


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


def test_runner_retries_a_one_shot_while_workers_are_off(tmp_path):
    store = _store(tmp_path)
    row = _add(store, kind="once", spec=datetime.now(UTC).isoformat())
    bot = _bot()
    bot._control = {"premium_task_workers_enabled": False}
    asyncio.run(ScheduleRunner(bot, store).tick())
    saved = store.get(UID, row["id"])
    assert not bot._task_jobs.calls
    assert saved["enabled"] and saved["runs"] == 0
    assert saved["next_run"] > time.time()
    assert saved["last_status"].startswith("retrying")


def test_runner_waits_for_a_busy_worker_then_gives_up(tmp_path, monkeypatch):
    now = NOW.timestamp()
    monkeypatch.setattr(time, "time", lambda: now)
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
    assert saved["retry_since"] == row["next_run"]
    # Restarting the runner must not restart the grace window.
    now += GRACE_SECONDS + 1
    asyncio.run(ScheduleRunner(_bot(jobs=busy), store).tick())
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


def test_record_does_not_resume_a_schedule_paused_mid_run(tmp_path):
    store = ScheduleStore(tmp_path / "schedules.sqlite3")
    row = store.add(
        user_id=UID,
        name="digest",
        kind="cron",
        spec="0 9 * * *",
        tz="UTC",
        prompt="send it",
        channel_id="9",
        guild_id="5",
        dm=False,
        next_run=time.time() + 60,
    )
    assert store.update(UID, row["id"], enabled=False, last_status="paused by user")
    store.record(
        row["id"],
        status="ran",
        next_run=time.time() + 86400,
        enabled=True,
        ran=True,
        expected_next=row["next_run"],
    )
    after = store.get(UID, row["id"])
    assert after["enabled"] is False
    assert after["next_run"] == row["next_run"]


def test_one_time_in_repeated_autumn_hour_uses_the_future_pass():
    tz = zone("America/New_York")
    first_pass = datetime(2026, 11, 1, 5, 10, tzinfo=UTC)
    second_pass = datetime(2026, 11, 1, 6, 10, tzinfo=UTC)
    assert parse_at("2026-11-01T01:30", tz, first_pass) == datetime(
        2026, 11, 1, 5, 30, tzinfo=UTC
    )
    assert parse_at("2026-11-01T01:30", tz, second_pass) == datetime(
        2026, 11, 1, 6, 30, tzinfo=UTC
    )


def test_relative_one_shot_preserves_elapsed_minutes():
    now = NOW.replace(second=45, microsecond=123456)
    expected = now + timedelta(minutes=1)
    spec, stamp = plan_at("in 1m", "UTC", now)
    assert datetime.fromisoformat(spec) == expected
    assert stamp == expected.timestamp()


def test_one_shot_at_exact_first_fold_uses_future_second_fold():
    now = datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
    assert parse_at("2026-11-01T01:30", zone("America/New_York"), now) == datetime(
        2026, 11, 1, 6, 30, tzinfo=UTC
    )


def test_store_claims_are_exclusive_and_expire(tmp_path, monkeypatch):
    now = NOW.timestamp()
    monkeypatch.setattr(time, "time", lambda: now)
    store = _store(tmp_path)
    other_store = ScheduleStore(store.path)
    row = _add(store)
    other = _add(store, name="other")
    claimed = store.claim(row, now, 60)
    assert claimed is not None and store.claimed(claimed)
    assert other_store.claim(row, now, 60) is None
    # Serialize admission for the same owner so the daily cap cannot race.
    assert other_store.claim(other, now, 60) is None
    now += 61
    recovered = other_store.claim(row, now, 60)
    assert recovered is not None
    assert recovered["retry_since"] == row["next_run"]
    assert recovered["claim_token"] != claimed["claim_token"]
    assert not store.claimed(claimed)
    assert not store.retry_later(claimed, now + 120, "stale retry")
    assert store.get(UID, row["id"])["claim_token"] == recovered["claim_token"]


@pytest.mark.parametrize("change", ["pause", "delete", "edit", "reschedule", "resume"])
def test_runner_rejects_stale_due_snapshots(tmp_path, monkeypatch, change):
    now = NOW.timestamp()
    monkeypatch.setattr(time, "time", lambda: now)
    store = _store(tmp_path)
    row = _add(store)
    if change == "pause":
        store.update(UID, row["id"], enabled=False)
    elif change == "delete":
        store.delete(UID, row["id"])
    elif change == "edit":
        store.update(UID, row["id"], prompt="new instructions")
    elif change == "reschedule":
        store.update(UID, row["id"], next_run=now + 100)
    else:
        store.update(UID, row["id"], enabled=False)
        store.update(UID, row["id"], enabled=True, next_run=row["next_run"])
    bot = _bot()
    asyncio.run(ScheduleRunner(bot, store).fire(row, now))
    assert bot._task_jobs.calls == []


def test_two_runners_cannot_submit_the_same_firing(tmp_path, monkeypatch):
    now = NOW.timestamp()
    monkeypatch.setattr(time, "time", lambda: now)
    store = _store(tmp_path)
    row = _add(store)

    async def run():
        entered, release = asyncio.Event(), asyncio.Event()

        class BlockingJobs(FakeJobs):
            async def submit(self, *args, **kwargs):
                result = await super().submit(*args, **kwargs)
                if len(self.calls) == 1:
                    entered.set()
                    await release.wait()
                return result

        jobs = BlockingJobs()
        first = asyncio.create_task(
            ScheduleRunner(_bot(jobs=jobs), store).fire(row, now)
        )
        await asyncio.wait_for(entered.wait(), timeout=5)
        try:
            await ScheduleRunner(_bot(jobs=jobs), ScheduleStore(store.path)).fire(
                row, now
            )
        finally:
            release.set()
            await first
        assert len(jobs.calls) == 1

    asyncio.run(run())
    assert store.get(UID, row["id"])["runs"] == 1


@pytest.mark.parametrize("change", ["pause", "delete", "edit"])
def test_runner_rechecks_claim_after_preparing_delivery(tmp_path, monkeypatch, change):
    now = NOW.timestamp()
    monkeypatch.setattr(time, "time", lambda: now)
    store = _store(tmp_path)
    row = _add(store)
    bot = _bot()
    runner = ScheduleRunner(bot, store)
    original = runner._message

    async def prepare(current):
        message = await original(current)
        if change == "pause":
            store.update(UID, row["id"], enabled=False, last_status="paused by user")
        elif change == "delete":
            store.delete_user(UID)
        else:
            store.update(UID, row["id"], prompt="new instructions")
        return message

    monkeypatch.setattr(runner, "_message", prepare)
    asyncio.run(runner.tick())
    assert bot._task_jobs.calls == []
    if change == "delete":
        assert store.history(UID) == []


def test_retry_cannot_stomp_a_concurrent_reschedule(tmp_path, monkeypatch):
    now = NOW.timestamp()
    monkeypatch.setattr(time, "time", lambda: now)
    store = _store(tmp_path)
    row = _add(store)
    claimed = store.claim(row, now, 60)
    store.update(UID, row["id"], next_run=now + 5000, last_status="rescheduled")
    assert not store.retry_later(claimed, now + 120, "waiting")
    saved = store.get(UID, row["id"])
    assert saved["next_run"] == now + 5000
    assert saved["last_status"] == "rescheduled"


def test_accepted_job_is_counted_without_overwriting_mid_submit_edits(
    tmp_path, monkeypatch
):
    now = NOW.timestamp()
    monkeypatch.setattr(time, "time", lambda: now)
    store = _store(tmp_path)
    row = _add(store)

    class EditingJobs(FakeJobs):
        async def submit(self, *args, **kwargs):
            store.update(UID, row["id"], enabled=False, last_status="paused by user")
            return await super().submit(*args, **kwargs)

    asyncio.run(ScheduleRunner(_bot(jobs=EditingJobs()), store).tick())
    saved = store.get(UID, row["id"])
    assert not saved["enabled"] and saved["next_run"] == row["next_run"]
    assert saved["last_status"] == "paused by user"
    assert saved["runs"] == 1 and saved["last_job"] == "abcdef123456"
    assert store.runs_since(UID, now - 86400) == 1


def test_channel_retry_preserves_grace_across_runner_restart(tmp_path, monkeypatch):
    now = NOW.timestamp()
    monkeypatch.setattr(time, "time", lambda: now)
    store = _store(tmp_path)
    row = _add(store)
    bot = _bot()
    runner = ScheduleRunner(bot, store)
    monkeypatch.setattr(
        runner,
        "_message",
        AsyncMock(side_effect=Unavailable("temporary", permanent=False)),
    )
    asyncio.run(runner.tick())
    saved = store.get(UID, row["id"])
    assert saved["retry_since"] == row["next_run"] and saved["failures"] == 1
    now += GRACE_SECONDS + 1
    asyncio.run(ScheduleRunner(bot, store).tick())
    saved = store.get(UID, row["id"])
    assert saved["last_status"].startswith("missed") and saved["retry_since"] is None
    assert bot._task_jobs.calls == []


@pytest.mark.parametrize("access", ["removed", "member", "moderator"])
def test_private_thread_requires_current_access(tmp_path, monkeypatch, access):
    import discord
    from plugins.schedules import runner as runner_module

    now = NOW.timestamp()
    monkeypatch.setattr(time, "time", lambda: now)
    store = _store(tmp_path)
    row = _add(store, dm=False, channel_id="77", guild_id="5")
    member = SimpleNamespace(id=int(UID))
    permissions = SimpleNamespace(
        view_channel=True, manage_threads=access == "moderator"
    )

    class PrivateThread:
        id = 77
        guild = SimpleNamespace(id=5, get_member=lambda uid: member, me=None)
        fetch_member = AsyncMock(
            return_value=SimpleNamespace(id=int(UID)),
            side_effect=(
                discord.NotFound(
                    SimpleNamespace(status=404, reason="missing"), "missing"
                )
                if access == "removed"
                else None
            ),
        )

        def is_private(self):
            return True

        def get_member(self, uid):
            return None

        def permissions_for(self, user):
            return permissions

    channel = PrivateThread()
    monkeypatch.setattr(runner_module.discord, "Thread", PrivateThread)
    bot = _bot()
    bot.get_channel = lambda cid: channel
    asyncio.run(ScheduleRunner(bot, store).tick())
    saved = store.get(UID, row["id"])
    if access == "removed":
        assert bot._task_jobs.calls == []
        assert not saved["enabled"] and "private thread" in saved["last_status"]
    else:
        assert len(bot._task_jobs.calls) == 1 and saved["runs"] == 1
    if access == "moderator":
        channel.fetch_member.assert_not_awaited()
    else:
        channel.fetch_member.assert_awaited_once_with(int(UID))


def test_tool_handles_schedule_deleted_during_edit(tmp_path, monkeypatch):
    store = _store(tmp_path)
    row = _add(store)
    original = store.update

    def delete_then_update(uid, sid, **fields):
        store.delete(uid, sid)
        return original(uid, sid, **fields)

    monkeypatch.setattr(store, "update", delete_then_update)
    result = asyncio.run(
        ScheduleTool(_bot(), store).execute(
            _message(), action="edit", id=row["id"], name="new"
        )
    )
    assert result == "Error: no schedule with that id"


def test_store_migrates_existing_schedule_database(tmp_path, monkeypatch):
    now = NOW.timestamp()
    monkeypatch.setattr(time, "time", lambda: now)
    path = tmp_path / "schedules.sqlite3"
    db = sqlite3.connect(path)
    try:
        with db:
            db.execute(
                "CREATE TABLE schedules ("
                "id TEXT PRIMARY KEY, user_id TEXT NOT NULL, name TEXT NOT NULL, "
                "kind TEXT NOT NULL, spec TEXT NOT NULL, tz TEXT NOT NULL, prompt TEXT NOT NULL, "
                "channel_id TEXT NOT NULL DEFAULT '', guild_id TEXT NOT NULL DEFAULT '', "
                "dm INTEGER NOT NULL DEFAULT 0, enabled INTEGER NOT NULL DEFAULT 1, "
                "next_run REAL, last_run REAL, last_status TEXT NOT NULL DEFAULT '', "
                "last_job TEXT NOT NULL DEFAULT '', runs INTEGER NOT NULL DEFAULT 0, "
                "failures INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, updated_at REAL NOT NULL)"
            )
            db.execute(
                "INSERT INTO schedules(id, user_id, name, kind, spec, tz, prompt, dm, next_run, "
                "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "01234567",
                    UID,
                    "legacy",
                    "cron",
                    "@daily",
                    "UTC",
                    "original",
                    1,
                    now - 5,
                    now,
                    now,
                ),
            )
    finally:
        db.close()
    store = ScheduleStore(path)
    row = store.get(UID, "01234567")
    assert row["name"] == "legacy" and row["prompt"] == "original"
    assert row["revision"] == 0 and row["retry_since"] is None
    claimed = store.claim(row, now, 60)
    assert claimed is not None
    assert store.retry_later(claimed, now + 120, "waiting")
    assert ScheduleStore(path).get(UID, row["id"])["retry_since"] == now - 5


def test_stale_request_cannot_recreate_schedule_after_deletion(tmp_path):
    store = ScheduleStore(tmp_path / "plugins/schedules/schedules.sqlite3")
    deleted_at = 1_000.0
    prefs = tmp_path / "plugins/maxwell_extras/user_preferences.json"
    prefs.parent.mkdir(parents=True)
    prefs.write_text(
        json.dumps({"users": {UID: {"deleted_at": deleted_at}}}),
        encoding="utf-8",
    )
    with pytest.raises(ScheduleLimitError, match="privacy deletion"):
        _add(store, started_at=deleted_at - 1)
    assert store.list_user(UID) == []
    row = _add(store, started_at=deleted_at + 1)
    with pytest.raises(ScheduleLimitError, match="privacy deletion"):
        store.update(UID, row["id"], name="revived", started_at=deleted_at - 1)
    assert store.get(UID, row["id"])["name"] == "news"


def test_prompt_edit_during_submit_still_advances_the_firing(tmp_path):
    now = time.time()
    store = _store(tmp_path)
    row = _add(store, next_run=now - 5, kind="once", spec="2026-10-21T09:00:00+00:00")
    claimed = store.claim(row, now, 60)
    assert claimed is not None
    assert (
        store.update(UID, row["id"], prompt="edited prompt")["prompt"]
        == "edited prompt"
    )
    assert store.record(
        row["id"],
        status="started job abc",
        next_run=None,
        enabled=False,
        ran=True,
        job="abc",
        expected_next=row["next_run"],
        claim_token=claimed["claim_token"],
    )
    saved = store.get(UID, row["id"])
    assert saved["runs"] == 1
    assert saved["enabled"] is False
    assert saved["next_run"] is None
    assert saved["prompt"] == "edited prompt"
