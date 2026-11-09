"""Gateway task scheduling does not leak unstarted recovery coroutines."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot import MaxwellBot


@pytest.mark.parametrize("scheduler,worker,args", [
    ("_schedule_recovery", "_recover_missed_messages", ()),
    ("_schedule_shard_resync", "_resync_shard_state", (3,)),
])
def test_gateway_scheduler_without_running_loop_does_not_create_coroutine(scheduler, worker, args):
    callback = AsyncMock()
    bot = SimpleNamespace(**{worker: callback})

    getattr(MaxwellBot, scheduler)(bot, *args)

    callback.assert_not_called()


@pytest.mark.parametrize("scheduler,worker,args,task_key", [
    ("_schedule_recovery", "_recover_missed_messages", (), "_recovery_task"),
    ("_schedule_shard_resync", "_resync_shard_state", (3,), "_shard_resync_tasks"),
])
def test_gateway_scheduler_tracks_one_worker_and_rearms_after_completion(scheduler, worker, args, task_key):
    async def scenario():
        release = asyncio.Event()
        callback = AsyncMock(side_effect=lambda *args: None)

        async def run(*args):
            await callback(*args)
            await release.wait()

        tracked = []
        bot = SimpleNamespace(_tasks=tracked, _track_task=tracked.append, **{worker: run})
        schedule = getattr(MaxwellBot, scheduler)
        schedule(bot, *args)
        schedule(bot, *args)
        first = tracked[0]
        assert len(tracked) == 1
        if task_key == "_recovery_task":
            assert bot._recovery_task is first
        else:
            assert bot._shard_resync_tasks[3] is first
        release.set()
        await first
        callback.assert_awaited_once_with(*args)
        schedule(bot, *args)
        assert len(tracked) == 2
        await tracked[1]
        assert callback.await_count == 2

    asyncio.run(scenario())
