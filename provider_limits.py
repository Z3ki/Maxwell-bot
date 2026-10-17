"""Request and whole-turn budgets shared by normal chat and task workers."""

from __future__ import annotations

import math


def _bounded(value, default, ceiling=7200):
    try:
        number = float(value)
        return max(10, min(number, ceiling)) if math.isfinite(number) else default
    except (ValueError, TypeError):
        return default


def request_timeout_seconds(bot, provider=None):
    policy_limit = getattr(
        getattr(provider, "policy", None), "max_request_seconds", None
    )
    if policy_limit:
        return _bounded(policy_limit, 300)
    config = getattr(bot, "config", None)
    control = getattr(bot, "_control", {}) or {}
    return _bounded(
        control.get(
            "ai_timeout_seconds", getattr(config, "AI_REQUEST_TIMEOUT_SECONDS", 3600)
        ),
        3600,
    )


def turn_timeout_seconds(bot, provider=None):
    personal_limit = getattr(provider, "turn_timeout_seconds", None)
    if personal_limit:
        return _bounded(personal_limit, 900)
    configured = getattr(getattr(bot, "config", None), "AI_TURN_TIMEOUT_SECONDS", 0)
    getter = getattr(bot, "_inbound_setting", None)
    if not configured and callable(getter):
        return getter("live_turn_timeout_seconds", 180, 1, 7200)
    return _bounded(
        configured
        or (getattr(bot, "_control", {}) or {}).get("live_turn_timeout_seconds", 180),
        180,
    )


def worker_timeout_seconds(bot):
    """Operator ceiling for all workers, including personal connections."""
    configured = getattr(
        getattr(bot, "config", None), "AI_WORKER_TURN_TIMEOUT_SECONDS", 0
    )
    return _bounded(
        configured
        or (getattr(bot, "_control", {}) or {}).get(
            "task_workers_timeout_seconds", 600
        ),
        600,
    )


def request_turn_timeout_seconds(bot, provider=None, *, premium=False):
    timeout = turn_timeout_seconds(bot, provider)
    if premium and (getattr(bot, "_control", {}) or {}).get(
        "premium_task_workers_enabled", True
    ):
        # Leave room for the coordinator to report after a worker deadline.
        timeout = max(timeout, worker_timeout_seconds(bot) + 30)
    return timeout
