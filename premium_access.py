"""Request-local plan snapshot for presentation; actions recheck the durable grant."""

import asyncio
from contextvars import ContextVar

current_plan = ContextVar("maxwell_current_plan", default=None)


async def load_plan(bot, user_id):
    ledger = getattr(bot, "_message_quota", None)
    if ledger is None:
        return "free"
    try:
        return await asyncio.to_thread(ledger.plan_for, str(user_id))
    except Exception:
        return "free"


def premium_for_current_request(user_id):
    plan = current_plan.get()
    return bool(plan and plan == (str(user_id), "premium"))
