import asyncio
import json
from types import SimpleNamespace

import pytest

from plugins.maxwell_extras.admin_commands import (
    DIAGNOSTICS_COMMAND,
    MAINTENANCE_COMMAND,
    _coerce_value,
    _is_owner,
    _redact,
    _set_control,
)


def test_admin_surface_splits_diagnostics_from_maintenance_and_removes_owner_command():
    assert DIAGNOSTICS_COMMAND["name"] == "diagnostics"
    assert MAINTENANCE_COMMAND["name"] == "maintenance"
    assert DIAGNOSTICS_COMMAND["integration_types"] == [0, 1]
    actions = {
        choice["value"]
        for option in MAINTENANCE_COMMAND["options"]
        if option["name"] == "action"
        for choice in option["choices"]
    }
    assert {"set", "enable", "disable", "reload_control", "quota_set"} <= actions
    assert "spend" not in actions
    assert {"overview", "controls", "data"} <= {
        choice["value"]
        for option in DIAGNOSTICS_COMMAND["options"]
        for choice in option["choices"]
    }


def test_owner_gate_uses_configured_owner_ids():
    config = SimpleNamespace(
        MAXWELL_OWNER_IDS=("123", "456"),
        CREATOR_ID="",
        BOT_NAME="Maxwell",
        MAXWELL_USER_ID="",
        COMMAND_PREFIX=",",
        BOT_BIRTHDAY="2026-05-21",
        BOT_INVITE_URL="",
        OFFICIAL_INVITE="",
    )
    bot = SimpleNamespace(config=config)
    assert _is_owner(bot, 123)
    assert _is_owner(bot, "456")
    assert not _is_owner(bot, 789)


def test_redaction_hides_nested_secrets():
    value = {
        "autonomy_api_key": "secret-key",
        "normal": True,
        "nested": {"token": "abc", "model": "test"},
    }
    assert _redact(value) == {
        "autonomy_api_key": "<redacted>",
        "normal": True,
        "nested": {"token": "<redacted>", "model": "test"},
    }


def test_control_value_coercion_is_strict():
    assert _coerce_value("tools_enabled", "off") is False
    assert _coerce_value("ai_concurrency", "3") == 3
    assert _coerce_value("disabled_tools", '["web_search"]') == ["web_search"]
    with pytest.raises(ValueError):
        _coerce_value("tools_enabled", "maybe")
    with pytest.raises(ValueError):
        _coerce_value("disabled_tools", "web_search")


def test_set_control_persists_and_updates_live_state(tmp_path):
    calls = []

    def load_control(force=False):
        calls.append(force)

    bot = SimpleNamespace(
        config=SimpleNamespace(DATA_DIR=str(tmp_path)),
        _control={"ai_concurrency": 2},
        _load_control=load_control,
    )
    before, after = asyncio.run(_set_control(bot, "ai_concurrency", "99"))
    assert before == 2
    assert after == 10
    assert bot._control["ai_concurrency"] == 10
    assert calls == [True]
    stored = json.loads((tmp_path / "bot_control.json").read_text(encoding="utf-8"))
    assert stored["ai_concurrency"] == 10


def test_sensitive_control_cannot_be_changed_through_discord(tmp_path):
    bot = SimpleNamespace(
        config=SimpleNamespace(DATA_DIR=str(tmp_path)),
        _control={},
        _load_control=lambda force=False: None,
    )
    with pytest.raises(ValueError, match="sensitive"):
        asyncio.run(_set_control(bot, "autonomy_api_key", "do-not-store"))
    assert not (tmp_path / "bot_control.json").exists()


def test_old_owner_command_is_not_in_either_command_definition():
    assert DIAGNOSTICS_COMMAND["name"] != "owner"
    assert MAINTENANCE_COMMAND["name"] != "owner"
