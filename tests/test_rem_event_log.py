import asyncio

from memory import RemEventLog


def test_rem_event_log_record_drain_cap_and_strip_reasoning(tmp_path):
    async def run():
        log = RemEventLog(str(tmp_path), max_events=2)
        await log.record({"role": "user", "channel_id": "c", "user_id": "u1", "user_name": "a", "content": "one", "auto_mode": False})
        await log.record({"role": "assistant", "channel_id": "c", "user_id": "bot", "user_name": "Maxwell", "content": "two <think>secret</think> ok", "auto_mode": True})
        await log.record({"role": "user", "channel_id": "c", "user_id": "u2", "user_name": "b", "content": "three", "auto_mode": False})
        events = await log.drain_slice(None)
        assert [e["content"] for e in events] == ["two ok", "three"]
        assert "secret" not in events[0]["content"]
        assert await log.size() == 2
        await log.flush()
        loaded = RemEventLog(str(tmp_path), max_events=2)
        loaded.load_from_disk()
        assert [e["content"] for e in await loaded.drain_slice(None)] == ["two ok", "three"]
    asyncio.run(run())


def test_rem_event_log_drain_since_and_blacklist_exclusion_by_caller(tmp_path):
    async def run():
        log = RemEventLog(str(tmp_path), max_events=10)
        await log.record({"ts": "2026-01-01T00:00:00+00:00", "role": "user", "channel_id": "c", "user_id": "allowed", "user_name": "a", "content": "keep", "auto_mode": False})
        # Blacklisted users are filtered by bot.py before record() is called, so no event is appended here.
        assert [e["content"] for e in await log.drain_slice("2025-12-31T23:59:59+00:00")] == ["keep"]
        assert await log.drain_slice("2026-01-01T00:00:00+00:00") == []
    asyncio.run(run())
