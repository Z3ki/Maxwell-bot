"""Guardrails for autonomous, unsolicited actions."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, time

@dataclass(frozen=True)
class AttentionPolicy:
    quiet_start: time = time(22)
    quiet_end: time = time(8)
    cooldown_seconds: int = 3600
    minimum_score: float = 0.7

@dataclass(frozen=True)
class AttentionSignal:
    urgency: float = 0.0
    value: float = 0.0
    actionable: bool = False
    present: bool = True
    last_sent_at: datetime | None = None

def score(signal: AttentionSignal) -> float:
    return max(0.0, min(1.0, 0.6 * signal.urgency + 0.4 * signal.value))

def should_notify(signal: AttentionSignal, now: datetime, policy: AttentionPolicy = AttentionPolicy()) -> bool:
    if not signal.actionable or not signal.present: return False
    if policy.quiet_start <= now.time() or now.time() < policy.quiet_end:
        return signal.urgency >= 0.95
    if signal.last_sent_at and (now - signal.last_sent_at).total_seconds() < policy.cooldown_seconds: return False
    return score(signal) >= policy.minimum_score
