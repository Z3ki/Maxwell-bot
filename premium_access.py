"""Request-local plan snapshot for presentation; actions recheck the durable grant."""

import asyncio
from contextvars import ContextVar

current_plan = ContextVar("maxwell_current_plan", default=None)

# Offered only on Premium turns. The tools recheck the durable grant themselves.
# delegate_task drops out of this set while subagents_require_premium is off.
PREMIUM_TOOLS = frozenset({"delegate_task", "schedule", "storage"})


def subagents_require_premium(bot) -> bool:
    """Whether delegate_task still checks the Premium grant.

    Default is open. The control key is the way back, not a code edit.
    """
    from control_defaults import DEFAULT_CONTROL

    control = getattr(bot, "_control", None) or {}
    return bool(
        control.get(
            "subagents_require_premium",
            DEFAULT_CONTROL["subagents_require_premium"],
        )
    )


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


def premium_withheld_note(bot, visible_names, user_id) -> str:
    """Tell the model why a registered Premium tool is absent from this turn."""
    tools = getattr(bot, "tools", None) or {}
    if "delegate_task" in visible_names or "delegate_task" not in tools:
        return ""
    control = getattr(bot, "_control", None) or {}
    if not control.get("premium_task_workers_enabled", True):
        return (
            "\n\nSubagents are switched off on this bot. "
            "delegate_task is registered but not available this turn."
        )
    if not subagents_require_premium(bot):
        return ""
    if premium_for_current_request(user_id):
        return ""
    return (
        "\n\nSubagents, scheduled wake-ups and persistent /storage (plus a personal public web "
        "folder) need an operator-granted Premium plan. This requester does not have Premium, so "
        "delegate_task, schedule and storage are not in this turn's tool catalog. If they ask for a "
        "subagent, a background worker, a recurring or timed run, or files that outlive the shell "
        "sandbox, say that Premium is the reason. Do not say the tool failed to load or needs a restart. "
        "Stating this gate is not an advertisement: do not mention prices or ask them to upgrade."
    )
