"""Tool batches preserve dependencies and still parallelize independent reads."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot import MaxwellBot


def _call(name):
    return {
        "id": name,
        "type": "function",
        "function": {"name": name, "arguments": "{}"},
    }


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
