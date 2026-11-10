"""Explicit phases for detached worker jobs.

Rows still persist plain strings. ``TaskJobStore.update`` refuses a move
that this table does not list. Same-phase writes stay legal so tool-call
checkpoints can repeat the current phase.
"""

from __future__ import annotations

from enum import Enum


class JobPhase(Enum):
    QUEUED = "queued"
    RUNNING = "running"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    INCOMPLETE = "incomplete"
    FAILED = "failed"
    CANCELLED = "cancelled"
    STOPPED = "stopped"
    INTERRUPTED = "interrupted"


class DeliveryPhase(Enum):
    PENDING = "pending"
    ATTEMPTING = "attempting"
    SENT = "sent"
    SUPPRESSED = "suppressed"
    EXPIRED = "expired"
    FAILED = "failed"
    UNKNOWN = "unknown"


LIVE_STATES = ("queued", "running", "cancelling")
LIVE = frozenset(JobPhase(name) for name in LIVE_STATES)
_STOP = frozenset({JobPhase.CANCELLED, JobPhase.STOPPED, JobPhase.INTERRUPTED})
_FINISH = frozenset({JobPhase.COMPLETED, JobPhase.INCOMPLETE, JobPhase.FAILED})


def parse_job(state: str) -> JobPhase:
    try:
        return JobPhase(state)
    except ValueError:
        raise ValueError(f"Unknown job state: {state}") from None


def parse_delivery(state: str) -> DeliveryPhase:
    try:
        return DeliveryPhase(state)
    except ValueError:
        raise ValueError(f"Unknown delivery state: {state}") from None


def job_transition(current: str, target: str) -> str:
    src = parse_job(current)
    dst = parse_job(target)
    if src == dst:
        return dst.value
    if src in LIVE and dst is not JobPhase.QUEUED:
        return dst.value
    if src in _FINISH and dst in _STOP | {JobPhase.CANCELLING}:
        return dst.value
    if src is JobPhase.CANCELLING and dst in _STOP:
        return dst.value
    raise ValueError(f"Illegal job transition: {src.value} -> {dst.value}")


def delivery_transition(current: str, target: str) -> str:
    src = parse_delivery(current)
    dst = parse_delivery(target)
    if src == dst:
        return dst.value
    allowed = {
        DeliveryPhase.PENDING: {
            DeliveryPhase.ATTEMPTING,
            DeliveryPhase.SUPPRESSED,
            DeliveryPhase.EXPIRED,
            DeliveryPhase.FAILED,
            DeliveryPhase.SENT,
        },
        DeliveryPhase.ATTEMPTING: {
            DeliveryPhase.SENT,
            DeliveryPhase.SUPPRESSED,
            DeliveryPhase.EXPIRED,
            DeliveryPhase.FAILED,
            DeliveryPhase.UNKNOWN,
        },
    }
    if dst in allowed.get(src, ()):
        return dst.value
    raise ValueError(f"Illegal delivery transition: {src.value} -> {dst.value}")
