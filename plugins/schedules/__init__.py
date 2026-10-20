"""Premium schedules: cron and one-time wake-ups that run as background jobs."""

from __future__ import annotations


def setup(bot, ctx):
    from .runner import ScheduleRunner
    from .store import ScheduleStore
    from .tool import ScheduleTool

    store = ScheduleStore(ctx.store_path("schedules.sqlite3"))
    runner = ScheduleRunner(bot, store)
    bot._schedules = store
    ctx.register_service("schedules", store)
    # Minute resolution is the contract; 20s keeps a fire within ~half a minute.
    ctx.every(20.0, runner.tick)
    return [ScheduleTool(bot, store)]


async def teardown(bot):
    if getattr(bot, "_schedules", None) is not None:
        bot._schedules = None
