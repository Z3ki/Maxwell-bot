import asyncio
import pytest
from datetime import datetime, timezone
from attention import AttentionSignal, should_notify
from approval import ApprovalGate
from event_dispatch import EventDispatcher
from memory_controls import MemoryRecord, parse_memory_command


def test_memory_commands_and_layers():
    assert parse_memory_command("remember that I prefer X") == ("save", "I prefer X")
    assert parse_memory_command("forget old preference") == ("forget", "old preference")
    assert MemoryRecord("x", layer="project").normalized()["layer"] == "project"

def test_typed_dispatch():
    seen = []
    dispatcher = EventDispatcher()

    async def record(event):
        seen.append(event.payload)

    dispatcher.subscribe("webhook", record)
    asyncio.run(dispatcher.dispatch_payload("webhook", {"typed": True}, source="webhook"))
    assert seen == [{"typed": True}]

def test_attention_requires_action_and_respects_quiet_hours():
    now = datetime(2026, 1, 1, 23, tzinfo=timezone.utc)
    assert not should_notify(AttentionSignal(urgency=.8, value=1, actionable=True), now)
    assert should_notify(AttentionSignal(urgency=.99, value=0, actionable=True), now)

def test_approval_is_requester_bound_and_single_use():
    gate = ApprovalGate()
    gate.draft("r1", "delete", {"id": 1}, "u1")
    assert not gate.approve("r1", "u2")
    assert gate.approve("r1", "u1")
    assert gate.consume("r1").action == "delete"
    with pytest.raises(PermissionError): gate.consume("r1")
