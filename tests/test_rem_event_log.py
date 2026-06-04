import asyncio

from memory import RemEventLog


def test_rem_event_log_record_drain_cap_and_strip_reasoning(tmp_path):
    async def run():
        log = RemEventLog(str(tmp_path), max_events=2)
        await log.record(
            {
                "role": "user",
                "channel_id": "c",
                "user_id": "u1",
                "user_name": "a",
                "content": "one",
                "auto_mode": False,
            }
        )
        await log.record(
            {
                "role": "assistant",
                "channel_id": "c",
                "user_id": "bot",
                "user_name": "Maxwell",
                "content": "two <think>secret</think> ok",
                "auto_mode": True,
            }
        )
        await log.record(
            {
                "role": "user",
                "channel_id": "c",
                "user_id": "u2",
                "user_name": "b",
                "content": "three",
                "auto_mode": False,
                "message_id": "333",
                "mentions": [{"id": "bot", "name": "Maxwell"}],
                "reply_to_message_id": "222",
                "reply_to_author": "Maxwell",
                "reply_to_author_id": "bot",
                "reply_to_self": True,
            }
        )
        events = await log.drain_slice(None)
        assert [e["content"] for e in events] == ["two ok", "three"]
        assert events[1]["message_id"] == "333"
        assert events[1]["mentions"] == [{"id": "bot", "name": "Maxwell"}]
        assert events[1]["reply_to_message_id"] == "222"
        assert events[1]["reply_to_author_id"] == "bot"
        assert events[1]["reply_to_self"] is True
        assert "secret" not in events[0]["content"]
        assert await log.size() == 2
        await log.flush()
        loaded = RemEventLog(str(tmp_path), max_events=2)
        loaded.load_from_disk()
        assert [e["content"] for e in await loaded.drain_slice(None)] == [
            "two ok",
            "three",
        ]

    asyncio.run(run())


def test_rem_event_log_drain_since_and_blacklist_exclusion_by_caller(tmp_path):
    async def run():
        log = RemEventLog(str(tmp_path), max_events=10)
        await log.record(
            {
                "ts": "2026-01-01T00:00:00+00:00",
                "role": "user",
                "channel_id": "c",
                "user_id": "allowed",
                "user_name": "a",
                "content": "keep",
                "auto_mode": False,
            }
        )
        # Blacklisted users are filtered by bot.py before record() is called, so no event is appended here.
        assert [
            e["content"] for e in await log.drain_slice("2025-12-31T23:59:59+00:00")
        ] == ["keep"]
        assert await log.drain_slice("2026-01-01T00:00:00+00:00") == []

    asyncio.run(run())


def test_rem_event_log_flush_waits_for_in_flight_save(tmp_path):
    class SlowLog(RemEventLog):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.snapshots = []

        async def _atomic_save(self, snapshot):
            self.started.set()
            await self.release.wait()
            self.snapshots.append(snapshot)

    async def run():
        log = SlowLog(str(tmp_path), max_events=10)
        await log.record(
            {
                "role": "user",
                "channel_id": "c",
                "user_id": "u",
                "user_name": "a",
                "content": "keep",
            }
        )
        log._do_save()
        await log.started.wait()

        flush_task = asyncio.create_task(log.flush())
        await asyncio.sleep(0)
        assert not flush_task.done()

        log.release.set()
        await flush_task
        assert log.snapshots and log.snapshots[-1][0]["content"] == "keep"

    asyncio.run(run())
