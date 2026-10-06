"""Prompt and dispatch regressions for action verification and retired agents."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot import MaxwellBot, _tool_results_need_followup
from maxwell_core.prompts.protocols import TOOL_PROTOCOL, MAXWELL_BASE_KNOWLEDGE
from plugins.discord_messages.impl import ReactTool, DeleteMessageTool
from plugins.discord_presence.impl import ChangePresenceTool, SetNicknameTool
from plugins.runtime_controls.impl import ClearSleepTool
from tool_schemas import returns_result
from api.state import _sanitize_control


def _call(name, index):
    return {
        "id": f"call_{index}",
        "type": "function",
        "function": {"name": name, "arguments": "{}"},
    }


@pytest.mark.parametrize("action", ["shell", "delete_message", "react", "unknown_action"])
@pytest.mark.parametrize("ending", ["send_message", "no_response", "sleep"])
@pytest.mark.parametrize("ending_first", [False, True])
def test_actions_are_observed_before_a_terminal_reply(
    monkeypatch, action, ending, ending_first
):
    async def run():
        executed = []
        owner = SimpleNamespace(_control={}, tools={})
        message = SimpleNamespace(id=7, channel=SimpleNamespace(id=99), guild=None)

        async def execute(self, message, name, *_args, **_kwargs):
            executed.append(name)
            return f"Tool {name}: Error - operation failed"

        monkeypatch.setattr(MaxwellBot, "_execute_tool_by_name", execute)
        monkeypatch.setattr(MaxwellBot, "_remember_tool_call", AsyncMock())
        names = [ending, action] if ending_first else [action, ending]
        _, results = await MaxwellBot._process_native_tool_calls(
            owner, message, "", [_call(n, i) for i, n in enumerate(names)]
        )
        assert executed == [action]
        assert any(f"Tool {ending}: Deferred" in r for r in results)
        assert _tool_results_need_followup(results)
        # Every emitted call, including a deferred one, retains a matching
        # tool message for native providers' next generation.
        history = owner._last_native_followup_messages
        ids = {m["tool_call_id"] for m in history if m["role"] == "tool"}
        assert ids == {"call_0", "call_1"}

    asyncio.run(run())


@pytest.mark.parametrize("tool_cls", [
    ReactTool, DeleteMessageTool, ChangePresenceTool, SetNicknameTool, ClearSleepTool,
])
def test_state_changes_return_confirmation_in_live_and_fallback_catalogs(tool_cls):
    tool = tool_cls(SimpleNamespace())
    assert tool.returns_result
    assert returns_result(tool.tool_name)
    folder = Path(__file__).resolve().parents[1] / "plugins"
    declared = [
        t for manifest in folder.glob("*/plugin.json")
        for t in json.loads(manifest.read_text()).get("tools", [])
        if t["name"] == tool.tool_name
    ]
    assert len(declared) == 1 and declared[0]["returns_result"] is True


@pytest.mark.parametrize("following", ["no_response", "wait", "typing", "send_message"])
def test_successful_sleep_ends_the_entire_batch(monkeypatch, following):
    async def run():
        executed = []
        owner = SimpleNamespace(_control={}, tools={})
        message = SimpleNamespace(id=7, channel=SimpleNamespace(id=99), guild=None)

        async def execute(self, message, name, *_args, **_kwargs):
            executed.append(name)
            return "Tool sleep: Sleeping for 5 minutes"

        monkeypatch.setattr(MaxwellBot, "_execute_tool_by_name", execute)
        monkeypatch.setattr(MaxwellBot, "_remember_tool_call", AsyncMock())
        _, results = await MaxwellBot._process_native_tool_calls(
            owner, message, "", [_call("sleep", 0), _call(following, 1)]
        )
        assert executed == ["sleep"]
        assert "turn already ended" in results[-1]

    asyncio.run(run())


@pytest.mark.parametrize("result", [
    "Tool typing: Error - invalid tool arguments",
    "Tool unknown_action: Error - unknown tool",
    "Tool typing: refused: permission denied",
])
def test_errors_from_silent_or_unknown_tools_get_a_followup(result):
    assert _tool_results_need_followup([result])


def test_a_search_title_containing_error_is_not_a_failure():
    assert not _tool_results_need_followup(["Tool typing: Error handling in Python"])


def test_prompt_has_one_freshness_policy_and_no_retired_instructions():
    assert TOOL_PROTOCOL.count("You decide whether web evidence is needed.") == 1
    assert "spawn_background" not in TOOL_PROTOCOL
    assert "reasoning only when the tool's declared schema accepts it" in TOOL_PROTOCOL
    assert "Core personality" in MAXWELL_BASE_KNOWLEDGE
    assert "never identity, permissions or tool rules" in MAXWELL_BASE_KNOWLEDGE


def test_saved_settings_cannot_restore_retired_memory_workers():
    stale = {
        "cross_context_extract_enabled": True,
        "cross_context_extract_timeout_seconds": 30,
        "cross_context_extract_threshold": 0,
        "entity_memory_from_extract": True,
        "aux_model": "old-worker",
        "aux_api_key": "secret",
        "bg_max_iters": 100,
        "rem_enabled": True,
        "autofix_enabled": True,
        "autofix_open_pr": True,
    }
    settings = _sanitize_control(stale)
    assert not stale.keys() & settings.keys()
    assert settings["store_memory"] is True


def test_removed_agent_methods_cannot_be_invoked():
    for name in (
        "_get_aux_provider", "_rem_scheduler_loop", "_run_rem_once_guarded",
        "_maybe_schedule_context_extraction", "_extract_shared_context_fact",
        "_run_rem", "_get_aux_model",
    ):
        assert not hasattr(MaxwellBot, name)


def test_removed_memory_agent_routes_are_not_published():
    from api.api_server import app
    paths = [str(route.resource) for route in app.router.routes()]
    assert not any(
        "/api/rem/" in p or "/api/context_cleanup/" in p or "/api/github/webhook" in p
        for p in paths
    )
