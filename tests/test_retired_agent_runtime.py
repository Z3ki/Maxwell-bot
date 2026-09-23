"""Retired public coding tools cannot be revived by persisted plugin settings."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot import MaxwellBot, ToolCircuitBreaker, PUBLIC_RUNTIME_BLOCKED_TOOLS
from plugin_manager import PluginManager
from plugins.sites import setup as setup_sites


def test_retired_plugins_never_initialize_even_when_persisted_enabled(tmp_path):
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    retired = (
        "agent_life", "background_jobs", "github_projects", "shell",
        "plugin_admin", "personality",
    )
    for name in retired:
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
        for name in retired
    }}), encoding="utf-8")
    bot = SimpleNamespace(tools={})
    manager = PluginManager(bot, plugins_dir=str(plugins), data_dir=str(tmp_path),
                            state_file=str(state))
    manager.load_plugins()
    assert manager.loaded_plugins == {}
    assert manager.load_errors == {}
    assert manager.get_available_tools(user_id="1") == {}


def test_retired_tools_are_denied_at_dispatch_even_when_offered():
    blocked = {
        name: SimpleNamespace(execute=AsyncMock(return_value="executed"), is_destructive=True)
        for name in PUBLIC_RUNTIME_BLOCKED_TOOLS
    }
    bot = SimpleNamespace(
        tools=blocked, plugin_manager=None,
        _tool_breaker=ToolCircuitBreaker(), tool_concurrency=None, hooks=None,
        config=SimpleNamespace(MAXWELL_OWNER_IDS={"1"}, DISABLE_TAINT_GATE=False),
        _record_llm_trace=AsyncMock(),
        _message_tool_platform=lambda _message: "discord",
        is_message_tainted=lambda _message: False,
    )

    async def call(name, author_id=1):
        msg = SimpleNamespace(id=7, author=SimpleNamespace(id=author_id),
                              channel=SimpleNamespace(id=2), guild=None)
        return await MaxwellBot._execute_tool_by_name(
            bot, msg, name, {"command": "true"}, disabled=set(), compatible=set(blocked)
        )

    for name in blocked:
        assert "retired from the public bot runtime" in asyncio.run(call(name))
        blocked[name].execute.assert_not_awaited()
    assert "retired from the public bot runtime" in asyncio.run(call("bash"))
    blocked["shell"].execute.assert_not_awaited()


def test_site_backend_tool_is_not_registered_but_static_site_tools_remain():
    bot = SimpleNamespace(config=SimpleNamespace(
        MAXWELL_SITE_DIR="public/bot", MAXWELL_PUBLIC_BASE_URL="https://example.org",
        ENABLE_CREATE_SITE=True,
    ))
    names = {tool.name for tool in setup_sites(bot)}
    assert "site_server" not in names
    assert {"create_site", "edit_site", "list_sites", "host_file"} <= names


def test_reload_removes_tools_no_longer_published(tmp_path):
    plugins = tmp_path / "plugins"
    folder = plugins / "echo"
    folder.mkdir(parents=True)
    (folder / "plugin.json").write_text(json.dumps({
        "id": "echo", "name": "echo", "version": "1.0.0", "api_version": 1,
        "enabled_by_default": True,
    }), encoding="utf-8")
    module = folder / "__init__.py"
    module.write_text(
        "def setup(bot, ctx=None):\n"
        "    return [type('Echo', (), {'tool_name': 'echo', 'execute': lambda *a: None})()]\n",
        encoding="utf-8",
    )
    bot = SimpleNamespace(tools={})
    manager = PluginManager(bot, plugins_dir=str(plugins), data_dir=str(tmp_path),
                            state_file=str(tmp_path / "plugins.json"))
    manager.load_plugins()
    assert "echo" in bot.tools
    module.write_text("def setup(bot, ctx=None):\n    return []\n", encoding="utf-8")
    manager.reload_plugins()
    assert "echo" not in bot.tools


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
