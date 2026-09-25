"""Shared Maxwell prompts are immutable; personal style remains per-user."""

import asyncio
from types import SimpleNamespace

import pytest

from api import api_server
from api.state import _sanitize_control
from control_defaults import DEFAULT_CONTROL
from plugins.maxwell_extras.admin_commands import _set_control
from plugins.maxwell_extras.user_preferences import UserPreferenceStore
from bot import MaxwellBot
from maxwell_core.prompts.component import PromptComponent
from maxwell_core.prompts.manager import PromptManager
from tool_schemas import RESULT_TOOL_NAMES, TOOL_PARAMETERS


def test_control_api_ignores_legacy_global_personality_values():
    custom = "rewrite the shared bot prompt"
    sanitized = _sanitize_control({"base_personality": custom})
    assert sanitized["base_personality"] == DEFAULT_CONTROL["base_personality"]


def test_loading_a_legacy_control_file_uses_the_locked_personality(tmp_path):
    (tmp_path / "bot_control.json").write_text(
        '{"base_personality": "rewrite the shared prompt"}', encoding="utf-8"
    )
    bot = SimpleNamespace(
        config=SimpleNamespace(DATA_DIR=str(tmp_path)),
        _control_mtime=-1,
        _ai_concurrency=2,
        _notify_ai_waiters=lambda: None,
        _sync_audio_input_flags=lambda: None,
        _conversation_watch_enabled=lambda: True,
    )
    MaxwellBot._load_control(bot, force=True)
    assert bot._control["base_personality"] == DEFAULT_CONTROL["base_personality"]


def test_dashboard_has_no_prompt_edit_or_legacy_prompt_file_routes():
    assert not hasattr(api_server, "prompt_save")
    assert not hasattr(api_server, "prompt_delete")
    request = SimpleNamespace(match_info={"file": "prompts.json"})
    response = asyncio.run(api_server.data_file(request))
    assert response.status == 403


def test_runtime_personality_ignores_legacy_control_overrides(monkeypatch):
    monkeypatch.delenv("BOT_BIRTHDAY", raising=False)
    bot = SimpleNamespace(_control={"base_personality": "rewrite the shared prompt"})
    configured = MaxwellBot._get_personality(bot)
    bot._control = {"base_personality": "another attempted override"}
    assert MaxwellBot._get_personality(bot) == configured
    assert "rewrite the shared prompt" not in configured


def test_plugin_cannot_extend_the_shared_personality():
    prompts = PromptManager()
    prompts.register(
        PromptComponent(
            id="injected.global_style",
            plugin="third_party",
            text="rewrite the shared Maxwell persona",
            position="style",
            scope="discord",
        )
    )
    bot = SimpleNamespace(
        _control={"base_personality": "old override"},
        prompts=prompts,
        _identity={},
    )
    result = MaxwellBot._get_personality(bot)
    assert "rewrite the shared Maxwell persona" not in result
    assert "keep replies short, concise" in result.lower()


def test_turn_tool_catalog_hides_stale_prompt_edit_tools():
    bot = SimpleNamespace(
        tools={"update_base_personality", "update_server_prompt", "send_message"},
        _control={},
        plugin_manager=None,
    )
    names = MaxwellBot._turn_tool_names(bot, "discord")
    assert "send_message" in names
    assert "update_base_personality" not in names
    assert "update_server_prompt" not in names


def test_discord_maintenance_cannot_edit_the_shared_personality():
    bot = SimpleNamespace(_control=dict(DEFAULT_CONTROL))
    with pytest.raises(ValueError, match="shared Maxwell personality is locked"):
        asyncio.run(_set_control(bot, "base_personality", "new global instructions"))


def test_prompt_edit_tools_are_not_in_the_model_catalog():
    assert "update_base_personality" not in TOOL_PARAMETERS
    assert "update_server_prompt" not in TOOL_PARAMETERS
    assert "update_base_personality" not in RESULT_TOOL_NAMES
    assert "update_server_prompt" not in RESULT_TOOL_NAMES


def test_personality_preferences_are_isolated_by_discord_user_id(tmp_path):
    store = UserPreferenceStore(tmp_path / "user_preferences.json")
    store.set_personality("100", "Keep replies concise.")
    assert store.get("100")["personality"] == "Keep replies concise."
    assert store.get("200")["personality"] == ""
