"""Tool batches preserve dependencies and still parallelize independent reads."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot import MaxwellBot


def _call(name):
    return {
        "id": name,
        "type": "function",
        "function": {"name": name, "arguments": "{}"},
    }


def test_long_tool_output_reaches_native_followup_without_middle_clipping(monkeypatch):
    output = "HEAD\n" + "x" * 200_000 + "\nTAIL"

    async def execute(*args, **kwargs):
        return output

    monkeypatch.setattr(MaxwellBot, "_execute_tool_by_name", execute)
    monkeypatch.setattr(MaxwellBot, "_remember_tool_call", AsyncMock())
    owner = SimpleNamespace(_control={}, tools={})
    message = SimpleNamespace(id=7, channel=SimpleNamespace(id=99), guild=None)
    _, results = asyncio.run(MaxwellBot._process_native_tool_calls(
        owner, message, "", [_call("shell")],
    ))
    assert results == [output]
    assert owner._last_native_followup_messages[-1]["content"] == output


def test_mutations_finish_before_a_batched_reply_is_deferred(monkeypatch):
    async def run():
        events = []
        owner = SimpleNamespace(_control={}, tools={})
        message = SimpleNamespace(id=7, channel=SimpleNamespace(id=99), guild=None)

        async def execute(self, message, name, *_args, **_kwargs):
            events.append(f"start:{name}")
            await asyncio.sleep(0)
            events.append(f"end:{name}")
            return f"Tool {name}: done"

        monkeypatch.setattr(MaxwellBot, "_execute_tool_by_name", execute)
        monkeypatch.setattr(MaxwellBot, "_remember_tool_call", AsyncMock())
        _cleaned, results = await MaxwellBot._process_native_tool_calls(
            owner,
            message,
            "",
            [
                _call("create_site"),
                _call("send_message"),
                _call("edit_site"),
            ],
        )
        assert events == [
            "start:create_site",
            "end:create_site",
            "start:edit_site",
            "end:edit_site",
        ]
        assert any("Tool send_message: Deferred" in line for line in results)

    asyncio.run(run())


def test_fetched_content_finishes_tainting_the_turn_before_shell(monkeypatch):
    async def run():
        tainted = False
        owner = SimpleNamespace(
            _control={},
            tools={
                "fetch_url": SimpleNamespace(side_effects=False),
                "shell": SimpleNamespace(side_effects=True),
            },
        )
        message = SimpleNamespace(id=7, channel=SimpleNamespace(id=99), guild=None)

        async def execute(self, message, name, *_args, **_kwargs):
            nonlocal tainted
            if name == "fetch_url":
                await asyncio.sleep(0)
                tainted = True
                return "Tool fetch_url: untrusted source content"
            assert tainted, "shell dispatched before the fetched-content security check"
            return "Tool shell: refused: this turn read untrusted content"

        monkeypatch.setattr(MaxwellBot, "_execute_tool_by_name", execute)
        monkeypatch.setattr(MaxwellBot, "_remember_tool_call", AsyncMock())
        _cleaned, results = await MaxwellBot._process_native_tool_calls(
            owner, message, "", [_call("fetch_url"), _call("shell")]
        )
        summary = "\n".join(results)
        assert "Tool shell: refused" in summary
        assert "AssertionError" not in summary

    asyncio.run(run())

def test_multiple_send_messages_run_in_same_batch_and_only_first_can_reply(monkeypatch):
    async def run():
        executed = []
        owner = SimpleNamespace(_control={}, tools={})
        message = SimpleNamespace(id=7, channel=SimpleNamespace(id=99), guild=None)

        async def execute(self, message, name, params, *_args, **_kwargs):
            executed.append((name, dict(params)))
            return "__MESSAGE_SENT__\n" + str(params.get("content") or "")

        monkeypatch.setattr(MaxwellBot, "_execute_tool_by_name", execute)
        monkeypatch.setattr(MaxwellBot, "_remember_tool_call", AsyncMock())
        calls = [
            {
                "id": "first",
                "type": "function",
                "function": {
                    "name": "send_message",
                    "arguments": '{"content":"first"}',
                },
            },
            {
                "id": "second",
                "type": "function",
                "function": {
                    "name": "send_message",
                    "arguments": '{"content":"second","reply":true,"reply_to":"same parent"}',
                },
            },
        ]

        _cleaned, results = await MaxwellBot._process_native_tool_calls(
            owner, message, "", calls
        )

        assert [name for name, _params in executed] == [
            "send_message",
            "send_message",
        ]
        # The first call keeps the tool's normal reply=True default by leaving
        # the argument absent. The second is normalized by the runtime.
        assert executed[0][1] == {"content": "first"}
        assert executed[1][1]["content"] == "second"
        assert executed[1][1]["reply"] is False
        assert "reply_to" not in executed[1][1]
        assert sum("__MESSAGE_SENT__" in line for line in results) == 2

    asyncio.run(run())


def test_failed_first_send_does_not_consume_the_reply_slot(monkeypatch):
    async def run():
        executed = []
        owner = SimpleNamespace(_control={}, tools={})
        message = SimpleNamespace(id=7, channel=SimpleNamespace(id=99), guild=None)

        async def execute(self, message, name, params, *_args, **_kwargs):
            executed.append(dict(params))
            if len(executed) == 1:
                return "Tool send_message: Error - send failed"
            return "__MESSAGE_SENT__\nsecond"

        monkeypatch.setattr(MaxwellBot, "_execute_tool_by_name", execute)
        monkeypatch.setattr(MaxwellBot, "_remember_tool_call", AsyncMock())
        calls = [
            {
                "id": "first",
                "type": "function",
                "function": {
                    "name": "send_message",
                    "arguments": '{"content":"first"}',
                },
            },
            {
                "id": "second",
                "type": "function",
                "function": {
                    "name": "send_message",
                    "arguments": '{"content":"second"}',
                },
            },
        ]

        await MaxwellBot._process_native_tool_calls(owner, message, "", calls)

        assert executed[0] == {"content": "first"}
        # Because nothing was delivered yet, the second call keeps the default
        # reply behavior instead of being forced standalone.
        assert executed[1] == {"content": "second"}

    asyncio.run(run())


def test_later_round_send_posts_to_the_channel(monkeypatch, tmp_path):
    """A second model round may send again, but not as another quote-reply."""

    async def run():
        from message_pipeline import RequestJournal

        executed = []
        journal = RequestJournal(tmp_path / "requests.sqlite")
        journal.accept(7, 99, directed=True)
        journal.update(7, "delivered", effects_started=True, response_id="555")
        owner = SimpleNamespace(_control={}, tools={}, _request_journal=journal)
        message = SimpleNamespace(id=7, channel=SimpleNamespace(id=99), guild=None)

        async def execute(self, message, name, params, *_args, **_kwargs):
            executed.append(dict(params))
            return "__MESSAGE_SENT__\n" + str(params.get("content") or "")

        monkeypatch.setattr(MaxwellBot, "_execute_tool_by_name", execute)
        monkeypatch.setattr(MaxwellBot, "_remember_tool_call", AsyncMock())
        calls = [
            {
                "id": "second-round",
                "type": "function",
                "function": {
                    "name": "send_message",
                    "arguments": (
                        '{"content":"second line","reply":true,'
                        '"reply_to":"absolutely not generating that"}'
                    ),
                },
            }
        ]

        await MaxwellBot._process_native_tool_calls(owner, message, "", calls)

        assert executed == [{"content": "second line", "reply": False}]

    asyncio.run(run())


@pytest.mark.parametrize("side_effects", [False, True])
@pytest.mark.parametrize("succeeds", [False, True])
def test_registered_terminal_stops_later_tools_only_after_success(
    monkeypatch, side_effects, succeeds
):
    from maxwell_core.tools import registry as registry_mod
    from maxwell_core.tools.registry import ToolRegistry
    from maxwell_core.tools.spec import ToolSpec

    terminal = SimpleNamespace(side_effects=side_effects)
    registry = ToolRegistry()
    registry.register(ToolSpec(
        name="custom_finish", plugin="example", tool=terminal, ends_turn=True
    ))
    monkeypatch.setattr(registry_mod, "_GLOBAL", registry)
    executed = []

    async def execute(self, message, name, *_args, **_kwargs):
        executed.append(name)
        await asyncio.sleep(0)
        outcome = "done" if succeeds or name != "custom_finish" else "Error - failed"
        return f"Tool {name}: {outcome}"

    monkeypatch.setattr(MaxwellBot, "_execute_tool_by_name", execute)
    monkeypatch.setattr(MaxwellBot, "_remember_tool_call", AsyncMock())
    owner = SimpleNamespace(_control={}, tools={
        "custom_finish": terminal, "typing": SimpleNamespace(side_effects=False)
    })
    message = SimpleNamespace(id=7, channel=SimpleNamespace(id=99), guild=None)

    _, results = asyncio.run(MaxwellBot._process_native_tool_calls(
        owner, message, "", [_call("custom_finish"), _call("typing")]
    ))

    assert executed == (["custom_finish"] if succeeds else ["custom_finish", "typing"])
    if succeeds:
        assert "Skipped" in results[1]
    else:
        assert "Error - failed" in results[0]


def test_registered_terminal_waits_for_batched_result_inspection(monkeypatch):
    from maxwell_core.tools import registry as registry_mod
    from maxwell_core.tools.registry import ToolRegistry
    from maxwell_core.tools.spec import ToolSpec

    registry = ToolRegistry()
    registry.register(ToolSpec(
        name="custom_finish", plugin="example", tool=SimpleNamespace(), ends_turn=True
    ))
    monkeypatch.setattr(registry_mod, "_GLOBAL", registry)
    executed = []

    async def execute(self, message, name, *_args, **_kwargs):
        executed.append(name)
        return f"Tool {name}: done"

    monkeypatch.setattr(MaxwellBot, "_execute_tool_by_name", execute)
    monkeypatch.setattr(MaxwellBot, "_remember_tool_call", AsyncMock())
    owner = SimpleNamespace(_control={}, tools={})
    message = SimpleNamespace(id=7, channel=SimpleNamespace(id=99), guild=None)

    _, results = asyncio.run(MaxwellBot._process_native_tool_calls(
        owner, message, "", [_call("shell"), _call("custom_finish")]
    ))

    assert executed == ["shell"]
    assert "Deferred" in results[1]
