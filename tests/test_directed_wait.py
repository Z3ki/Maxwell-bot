"""Directed waits must not consume a request before a visible reply."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot import MaxwellBot
from bot_parts.tool_calls import _tool_results_need_followup


def _call(name, **arguments):
    return {
        "id": name,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def _setup(monkeypatch, *, directed, response_id=None):
    row = {"directed": directed, "response_id": response_id}
    effects = []
    tools = {name: SimpleNamespace() for name in ("wait", "send_message")}
    owner = SimpleNamespace(
        _control={},
        tools=tools,
        config=SimpleNamespace(),
        _tool_breaker=SimpleNamespace(
            is_open=lambda name: False,
            record_failure=lambda name: None,
            record_success=lambda name: None,
        ),
        _message_tool_platform=lambda message: "discord",
    )
    message = SimpleNamespace(id=7, channel=SimpleNamespace(id=99), guild=None)

    async def state(self, message):
        return row

    async def invoke(self, message, name, handler, **params):
        effects.append((name, params))
        if name == "send_message":
            row["response_id"] = "sent-1"
            return "__MESSAGE_SENT__\n" + params["content"]
        return "Waited 0.0s"

    monkeypatch.setattr(MaxwellBot, "_request_state", state)
    monkeypatch.setattr(MaxwellBot, "_invoke_request_tool", invoke)
    monkeypatch.setattr(MaxwellBot, "_remember_tool_call", AsyncMock())
    return owner, message, row, effects


def test_directed_wait_first_returns_error_then_reply_is_sent(monkeypatch):
    owner, message, row, effects = _setup(monkeypatch, directed=True)

    async def run():
        _, results = await MaxwellBot._process_native_tool_calls(
            owner, message, "", [_call("wait", seconds=0)]
        )
        assert len(results) == 1
        assert results[0].startswith("Tool wait: Error")
        assert _tool_results_need_followup(results)
        assert owner._last_native_followup_messages[-1]["content"] == results[0]
        assert effects == []
        assert row["response_id"] is None

        _, reply_results = await MaxwellBot._process_native_tool_calls(
            owner, message, "", [_call("send_message", content="Here is the answer.")]
        )
        assert "__MESSAGE_SENT__" in reply_results[0]
        assert effects == [("send_message", {"content": "Here is the answer."})]
        assert row["response_id"] == "sent-1"

    asyncio.run(run())


def test_directed_wait_rejection_does_not_end_batch(monkeypatch):
    owner, message, row, effects = _setup(monkeypatch, directed=True)

    async def run():
        _, results = await MaxwellBot._process_native_tool_calls(
            owner,
            message,
            "",
            [_call("wait", seconds=0), _call("send_message", content="Answer.")],
        )
        assert results[0].startswith("Tool wait: Error")
        assert "__MESSAGE_SENT__" in results[1]
        assert effects == [("send_message", {"content": "Answer."})]
        assert row["response_id"] == "sent-1"

    asyncio.run(run())


def test_directed_wait_after_prior_visible_reply_is_unchanged(monkeypatch):
    owner, message, row, effects = _setup(monkeypatch, directed=True)

    async def run():
        await MaxwellBot._process_native_tool_calls(
            owner, message, "", [_call("send_message", content="Done.")]
        )
        _, results = await MaxwellBot._process_native_tool_calls(
            owner, message, "", [_call("wait", seconds=0)]
        )
        assert row["response_id"] == "sent-1"
        assert effects == [("send_message", {"content": "Done."}), ("wait", {"seconds": 0})]
        assert results == ["Tool wait: Waited 0.0s"]

    asyncio.run(run())


def test_ambient_wait_without_reply_is_unchanged(monkeypatch):
    owner, message, row, effects = _setup(monkeypatch, directed=False)

    async def run():
        _, results = await MaxwellBot._process_native_tool_calls(
            owner, message, "", [_call("wait", seconds=0)]
        )
        assert effects == [("wait", {"seconds": 0})]
        assert row["response_id"] is None
        assert results == ["Tool wait: Waited 0.0s"]

    asyncio.run(run())
