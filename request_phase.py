"""Explicit lifecycle for durable inbound requests.

Persisted rows still store the phase name as text. Callers pass those names;
this module is the only place that decides which names exist and which
moves are legal. External journal dicts are unchanged.
"""

from __future__ import annotations

from enum import Enum


class RequestPhase(Enum):
    RECEIVED = "received"
    QUEUED = "queued"
    DEFERRED = "deferred"
    RUNNING = "running"
    DELIVERED = "delivered"
    SUPPRESSED = "suppressed"
    FAILED = "failed"
    SUPERSEDED = "superseded"

    @property
    def pending(self) -> bool:
        return self in PENDING

    @property
    def terminal(self) -> bool:
        return self in TERMINAL


PENDING = frozenset(
    {
        RequestPhase.RECEIVED,
        RequestPhase.QUEUED,
        RequestPhase.DEFERRED,
        RequestPhase.RUNNING,
    }
)
TERMINAL = frozenset(
    {
        RequestPhase.DELIVERED,
        RequestPhase.SUPPRESSED,
        RequestPhase.FAILED,
        RequestPhase.SUPERSEDED,
    }
)
PENDING_STATUSES = tuple(phase.value for phase in RequestPhase if phase.pending)
TERMINAL_STATUSES = frozenset(phase.value for phase in TERMINAL)
REOPEN_REASON = "edited_mention"


def parse(status: str) -> RequestPhase:
    try:
        return RequestPhase(status)
    except ValueError:
        raise ValueError(f"Unknown request status: {status}") from None


def sql_list(phases: tuple[str, ...] | frozenset[str]) -> str:
    return ", ".join(f"'{name}'" for name in phases)


def transition(current: str, target: str, *, reason: str = "") -> RequestPhase:
    """Return the next phase or raise ValueError.

    Unfinished work may move to any known phase, including itself (effect
    checkpoints keep the phase and flip ``effects_started``). A finished
    request stays put, except a newly added direct mention reopens it as
    ``received``.
    """
    src = parse(current)
    dst = parse(target)
    if src == dst or src.pending:
        return dst
    if dst is RequestPhase.RECEIVED and reason == REOPEN_REASON:
        return dst
    raise ValueError(f"Illegal request transition: {src.value} -> {dst.value}")


def can_begin(status: str, effects_started: bool) -> bool:
    return parse(status).pending and not effects_started


def recover_target(status: str, effects_started: bool) -> str | None:
    """Startup recovery target, or None when the row must be left alone."""
    phase = parse(status)
    if phase in {RequestPhase.RECEIVED, RequestPhase.QUEUED, RequestPhase.RUNNING}:
        return (
            RequestPhase.FAILED.value
            if effects_started
            else RequestPhase.DEFERRED.value
        )
    if phase is RequestPhase.DEFERRED and effects_started:
        return RequestPhase.FAILED.value
    return None
