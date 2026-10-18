"""Request and whole-turn budgets shared by normal chat and task workers."""

from __future__ import annotations

import math

from control_defaults import DEFAULT_CONTROL

LIVE_TURN_TIMEOUT_DEFAULT_SECONDS = DEFAULT_CONTROL["live_turn_timeout_seconds"]

# Default whole-job budget for subagents, and the most a model may request.
WORKER_TIMEOUT_DEFAULT_SECONDS = 1200
WORKER_TIMEOUT_MAX_SECONDS = 1800


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
        return getter(
            "live_turn_timeout_seconds", LIVE_TURN_TIMEOUT_DEFAULT_SECONDS, 1, 7200
        )
    return _bounded(
        configured
        or (getattr(bot, "_control", {}) or {}).get(
            "live_turn_timeout_seconds", LIVE_TURN_TIMEOUT_DEFAULT_SECONDS
        ),
        LIVE_TURN_TIMEOUT_DEFAULT_SECONDS,
    )


def worker_timeout_seconds(bot):
    """Operator ceiling for all workers, including personal connections."""
    configured = getattr(
        getattr(bot, "config", None), "AI_WORKER_TURN_TIMEOUT_SECONDS", 0
    )
    return _bounded(
        configured
        or (getattr(bot, "_control", {}) or {}).get(
            "task_workers_timeout_seconds", WORKER_TIMEOUT_DEFAULT_SECONDS
        ),
        WORKER_TIMEOUT_DEFAULT_SECONDS,
    )


def requested_worker_timeout_seconds(default, timeout_minutes=None):
    """Job budget: the operator default, or a model-chosen value up to 30 min."""
    if timeout_minutes is None:
        return default
    try:
        minutes = float(timeout_minutes)
    except (ValueError, TypeError):
        return default
    if not math.isfinite(minutes) or minutes <= 0:
        return default
    return max(60, min(minutes * 60, max(default, WORKER_TIMEOUT_MAX_SECONDS)))


def request_turn_timeout_seconds(bot, provider=None, *, premium=False):
    # Detached workers own their deadline; slow jobs never extend a chat turn.
    return turn_timeout_seconds(bot, provider)
