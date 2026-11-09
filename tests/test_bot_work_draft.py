"""Long work drafts use request authorization and truthful worker receipts."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from bot import MaxwellBot


DRAFT = "```python\n" + "working notes\n" * 110 + "```"


@pytest.mark.parametrize(
    ("receipt", "accepted"),
    [
        ('{"id":"job-1","state":"queued"}', True),
        ("Error: workers are busy; no task started", False),
        ("refused: workers are disabled", False),
        ("", False),
    ],
)
def test_work_draft_claims_delegation_only_after_acceptance(monkeypatch, receipt, accepted):
    tool = SimpleNamespace(execute=AsyncMock(return_value=receipt))
    owner = SimpleNamespace(
        tools={"delegate_task": tool},
        _message_tool_platform=lambda _message: "discord",
    )
    message = SimpleNamespace(id=7, content="build the requested project")
    checkpoint = AsyncMock()
    authorize = Mock(return_value=None)
    monkeypatch.setattr(MaxwellBot, "_turn_tool_names", lambda *_args: {"delegate_task"})
    monkeypatch.setattr(MaxwellBot, "_mark_request_effect", checkpoint)
    monkeypatch.setattr(MaxwellBot, "_authorize_tool_execution", authorize)

    reply = asyncio.run(MaxwellBot._keep_work_draft_inside_subagent(
        owner, message, message.content, DRAFT
    ))

    assert ("Handed that to a subagent" in reply) is accepted
    assert DRAFT not in reply
    checkpoint.assert_awaited_once_with(owner, message)
    authorize.assert_called_once_with(
        owner, message, "delegate_task", tool,
        {"tasks": [{"task": message.content}]},
    )
    tool.execute.assert_awaited_once_with(message, tasks=[{"task": message.content}])


def test_work_draft_does_not_bypass_execution_authorization(monkeypatch):
    tool = SimpleNamespace(execute=AsyncMock())
    owner = SimpleNamespace(
        tools={"delegate_task": tool},
        _message_tool_platform=lambda _message: "discord",
    )
    message = SimpleNamespace(id=7, content="build the requested project")
    monkeypatch.setattr(MaxwellBot, "_turn_tool_names", lambda *_args: {"delegate_task"})
    monkeypatch.setattr(MaxwellBot, "_mark_request_effect", AsyncMock())
    monkeypatch.setattr(
        MaxwellBot, "_authorize_tool_execution", Mock(return_value="refused: tool disabled")
    )

    reply = asyncio.run(MaxwellBot._keep_work_draft_inside_subagent(
        owner, message, message.content, DRAFT
    ))

    assert "couldn't start a subagent" in reply
    assert "Handed that" not in reply
    tool.execute.assert_not_awaited()
