"""Unit tests for explicit request and job phase transitions."""

import pytest

from plugins.maxwell_extras.job_phase import delivery_transition, job_transition
from request_phase import can_begin, recover_target, transition


@pytest.mark.parametrize(
    "current,target,reason",
    [
        ("received", "queued", ""),
        ("received", "deferred", "capacity"),
        ("received", "running", ""),
        ("queued", "superseded", "same_user_interrupt"),
        ("running", "delivered", "confirmed_send"),
        ("running", "failed", "interrupted_after_effect"),
        ("deferred", "running", ""),
        ("running", "running", ""),
        ("delivered", "delivered", "partial_delivery"),
        ("failed", "received", "edited_mention"),
        ("superseded", "received", "edited_mention"),
    ],
)
def test_legal_request_transitions(current, target, reason):
    assert transition(current, target, reason=reason).value == target


@pytest.mark.parametrize(
    "current,target,reason",
    [
        ("delivered", "queued", ""),
        ("failed", "deferred", "retry"),
        ("superseded", "running", ""),
        ("delivered", "received", "edited"),
        ("nope", "queued", ""),
        ("received", "nope", ""),
    ],
)
def test_illegal_request_transitions(current, target, reason):
    with pytest.raises(ValueError):
        transition(current, target, reason=reason)


def test_begin_and_recover_follow_the_phase_table():
    assert can_begin("queued", False)
    assert not can_begin("running", True)
    assert not can_begin("delivered", False)
    assert recover_target("running", False) == "deferred"
    assert recover_target("running", True) == "failed"
    assert recover_target("deferred", True) == "failed"
    assert recover_target("deferred", False) is None
    assert recover_target("delivered", False) is None


@pytest.mark.parametrize(
    "current,target",
    [
        ("queued", "running"),
        ("queued", "interrupted"),
        ("running", "cancelling"),
        ("running", "completed"),
        ("running", "incomplete"),
        ("running", "failed"),
        ("cancelling", "cancelled"),
        ("cancelling", "stopped"),
        ("cancelling", "interrupted"),
        ("completed", "cancelling"),
        ("incomplete", "stopped"),
        ("failed", "interrupted"),
        ("running", "running"),
    ],
)
def test_legal_job_transitions(current, target):
    assert job_transition(current, target) == target


@pytest.mark.parametrize(
    "current,target",
    [
        ("running", "queued"),
        ("completed", "running"),
        ("cancelled", "running"),
        ("interrupted", "queued"),
        ("queued", "bogus"),
    ],
)
def test_illegal_job_transitions(current, target):
    with pytest.raises(ValueError):
        job_transition(current, target)


@pytest.mark.parametrize(
    "current,target",
    [
        ("pending", "attempting"),
        ("pending", "suppressed"),
        ("attempting", "sent"),
        ("attempting", "unknown"),
        ("attempting", "failed"),
        ("sent", "sent"),
    ],
)
def test_legal_delivery_transitions(current, target):
    assert delivery_transition(current, target) == target


def test_sent_delivery_does_not_reopen():
    with pytest.raises(ValueError, match="Illegal delivery"):
        delivery_transition("sent", "attempting")
