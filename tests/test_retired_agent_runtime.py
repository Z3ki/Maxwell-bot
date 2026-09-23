"""Detached LLM workers cannot be revived by old persisted plugin settings."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot import MaxwellBot, ToolCircuitBreaker
from plugin_manager import PluginManager


def test_retired_agent_plugins_never_initialize_even_when_persisted_enabled(tmp_path):
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    for name in ("agent_life", "background_jobs"):
        folder = plugins / name
        folder.mkdir()
        (folder / "plugin.json").write_text(
            json.dumps({"id": name, "name": name, "version": "1.0.0",
                        "api_version": 1, "enabled_by_default": True}),
            encoding="utf-8",
        )
        (folder / "__init__.py").write_text(
            "raise AssertionError('retired worker started')", encoding="utf-8"
        )
    state = tmp_path / "plugins.json"
    state.write_text(json.dumps({"plugins": {
        name: {"enabled_globally": True}
        for name in ("agent_life", "background_jobs")
    }}), encoding="utf-8")
    bot = SimpleNamespace(tools={})
    manager = PluginManager(bot, plugins_dir=str(plugins), data_dir=str(tmp_path),
                            state_file=str(state))
    manager.load_plugins()
    assert manager.loaded_plugins == {}
    assert manager.load_errors == {}
    assert manager.get_available_tools(user_id="1") == {}


def test_shell_dispatch_checks_owner_even_when_tool_is_offered():
    shell = SimpleNamespace(execute=AsyncMock(return_value="executed"), is_destructive=True)
    bot = SimpleNamespace(
        tools={"shell": shell}, plugin_manager=None,
        _tool_breaker=ToolCircuitBreaker(), tool_concurrency=None, hooks=None,
        config=SimpleNamespace(MAXWELL_OWNER_IDS={"1"}, DISABLE_TAINT_GATE=False),
        _record_llm_trace=AsyncMock(),
        _message_tool_platform=lambda _message: "discord",
        is_message_tainted=lambda _message: False,
    )

    async def call(author_id):
        msg = SimpleNamespace(id=7, author=SimpleNamespace(id=author_id),
                              channel=SimpleNamespace(id=2), guild=None)
        return await MaxwellBot._execute_tool_by_name(
            bot, msg, "shell", {"command": "true"}, disabled=set(), compatible={"shell"}
        )

    denied = asyncio.run(call(2))
    assert "restricted to the bot owner" in denied
    shell.execute.assert_not_awaited()
    assert "executed" in asyncio.run(call(1))
    shell.execute.assert_awaited_once()


def test_dispatch_does_not_return_sensitive_exception_text():
    tool = SimpleNamespace(execute=AsyncMock(side_effect=RuntimeError("token=private-secret")),
                           is_destructive=False)
    bot = SimpleNamespace(
        tools={"chess_start": tool}, plugin_manager=None,
        _tool_breaker=ToolCircuitBreaker(), tool_concurrency=None, hooks=None,
        config=SimpleNamespace(DISABLE_TAINT_GATE=False),
        _control={"autofix_enabled": False},
        _record_llm_trace=AsyncMock(),
        _message_tool_platform=lambda _message: "discord",
        is_message_tainted=lambda _message: False,
    )
    msg = SimpleNamespace(id=7, author=SimpleNamespace(id=2),
                          channel=SimpleNamespace(id=2), guild=None)
    result = asyncio.run(MaxwellBot._execute_tool_by_name(
        bot, msg, "chess_start", {}, disabled=set(), compatible={"chess_start"}
    ))
    assert result == "Tool chess_start: Error - internal tool failure (chess_start)"
    assert "private-secret" not in result
