"""Unrestricted installs and optional integrations follow actual setup."""

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import legal_notice
from bot import MaxwellBot
from control_defaults import DEFAULT_CONTROL
from maxwell_core.plugins.bundled import build_tools
from maxwell_core.plugins.manifest import ManifestError, validate_manifest
from plugin_manager import PluginManager
from usage_commands import install_usage_commands, usage_text_for


def test_self_hosted_defaults_have_no_hosted_allowance():
    assert DEFAULT_CONTROL["message_quota_enabled"] is False
    assert DEFAULT_CONTROL["premium_discovery_enabled"] is False
    assert DEFAULT_CONTROL["site_ttl_hours"] == 0
    ledger = SimpleNamespace(status=lambda *a: pytest.fail("disabled quota was queried"))
    assert "Unlimited" in usage_text_for(SimpleNamespace(_control={}, _message_quota=ledger), "1")


def test_no_legal_dm_without_operator_opt_in(monkeypatch, tmp_path):
    monkeypatch.delenv("MAXWELL_LEGAL_NOTICE", raising=False)
    bot = SimpleNamespace(config=SimpleNamespace(DATA_DIR=str(tmp_path)))
    user = SimpleNamespace(id=1, bot=False, send=AsyncMock())
    legal_notice.install(bot)
    asyncio.run(legal_notice.notify_user(bot, user))
    user.send.assert_not_awaited()
    assert list(tmp_path.iterdir()) == []


def test_premium_command_only_registered_when_operator_enables_it(monkeypatch):
    registered, removed = [], []
    monkeypatch.setattr("user_install.register_command", lambda c: registered.append(c["name"]))
    monkeypatch.setattr("user_install.unregister_command", removed.append)
    monkeypatch.setattr("user_install.register_interaction_handler", lambda *a, **kw: None)
    install_usage_commands(SimpleNamespace(_control={}))
    assert registered == ["help", "usage"]
    assert removed == ["premium"]
    registered.clear()
    install_usage_commands(SimpleNamespace(_control={"premium_discovery_enabled": True}))
    assert registered == ["help", "usage", "premium"]


def test_fresh_bot_startup_preserves_extension_commands_and_hides_optional_tools(tmp_path):
    root = Path(__file__).resolve().parents[1]
    env_file = tmp_path / ".env"
    env_file.write_text((root / ".env.simple.example").read_text().replace(
        "DISCORD_BOT_TOKEN=", "DISCORD_BOT_TOKEN=offline-test-token"
    ) + f"\nDATA_DIR={tmp_path}/data\n")
    code = """
import asyncio
import user_install as ui
from bot import MaxwellBot
ui.register_command({'name': 'extension-command', 'type': 1})
bot = MaxwellBot()
absent = {'create_site','edit_site','host_file','hd_image','shell','email_send','spawn_background','agent_life','github_repo'}
assert not (set(bot.tools) & absent), set(bot.tools) & absent
assert 'manage_plugin' in bot.tools
assert not bot._control['message_quota_enabled']
names = {c['name'] for c in ui.USER_INSTALL_COMMANDS}
assert 'premium' not in names
assert 'extension-command' in names
assert bot.plugin_manager.load_errors == {}, bot.plugin_manager.load_errors
assert str(bot.plugin_manager.data_dir) == bot.config.DATA_DIR
asyncio.run(bot.plugin_manager.teardown())
"""
    result = subprocess.run([sys.executable, "-c", code], cwd=root,
                            env={**os.environ, "MAXWELL_ENV_FILE": str(env_file)},
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_self_hosted_dispatch_allows_plugin_management():
    bot = SimpleNamespace(_control={}, config=SimpleNamespace(), plugin_manager=None)
    message = SimpleNamespace(author=SimpleNamespace(id=1), guild=None)
    handler = SimpleNamespace(tool_name="manage_plugin", requires_admin=False)
    assert MaxwellBot._authorize_tool_execution(bot, message, "manage_plugin", handler) is None
    bot.config.MAXWELL_RESTRICT_PUBLIC_RUNTIME = True
    assert "retired" in MaxwellBot._authorize_tool_execution(bot, message, "manage_plugin", handler)


def test_fully_disabled_tools_never_import_implementation(monkeypatch):
    monkeypatch.setattr("maxwell_core.plugins.bundled.import_module",
                        lambda *a: pytest.fail("disabled implementation imported"))
    bot = SimpleNamespace(config=SimpleNamespace(ENABLE_CREATE_SITE=False))
    assert build_tools(bot, "plugins.sites", [("create_site", "CreateSiteTool", "ENABLE_CREATE_SITE")]) == []
    assert build_tools(SimpleNamespace(), "plugins.sites", [("create_site", "CreateSiteTool", "ENABLE_CREATE_SITE")]) == []


def test_multiple_feature_gates_and_media_catalog(monkeypatch):
    implementation = SimpleNamespace()
    for name in ("ImageGeneratorTool", "HDImageGeneratorTool", "SeeImageTool", "SeeVideoTool", "SendMemeTool", "SendMediaTool"):
        setattr(implementation, name, lambda bot: SimpleNamespace(bot=bot))
    monkeypatch.setattr("maxwell_core.plugins.bundled.import_module", lambda *a: implementation)
    from plugins.images import setup as images
    from plugins.media import setup as media

    cfg = SimpleNamespace(ENABLE_IMAGE_GEN=True, ENABLE_HD_IMAGE=False,
                          ENABLE_IMAGE_INPUT=False, ENABLE_VIDEO_INPUT=False)
    bot = SimpleNamespace(config=cfg)
    assert [t.tool_name for t in images(bot)] == ["image_generator"]
    assert {t.tool_name for t in media(bot)} == {"send_meme", "send_media"}
    cfg.ENABLE_HD_IMAGE = True
    assert {t.tool_name for t in images(bot)} == {"image_generator", "hd_image"}
    cfg.ENABLE_IMAGE_GEN = False
    assert images(bot) == []


def _manager(tmp_path, manifest, code, *, cfg=None):
    folder = tmp_path / "plugins" / "extension"
    folder.mkdir(parents=True)
    (folder / "plugin.json").write_text(json.dumps({"id": "extension", **manifest}))
    (folder / "__init__.py").write_text("raise AssertionError('wrong entry imported')")
    (folder / "entry.py").write_text(code)
    return PluginManager(SimpleNamespace(tools={}, config=cfg or SimpleNamespace()),
                         plugins_dir=str(folder.parent), data_dir=str(tmp_path / "data"))


def test_custom_entry_can_use_relative_imports(tmp_path):
    manager = _manager(tmp_path, {"entry": "entry.py"},
                       "from .helper import VALUE\ndef setup(bot):\n    bot.entry_value = VALUE\n    return []\n")
    (tmp_path / "plugins/extension/helper.py").write_text("VALUE = 42")
    manager.load_plugins()
    assert manager.load_errors == {}
    assert manager.bot.entry_value == 42


def test_missing_feature_omits_plugin_without_import(tmp_path):
    manager = _manager(tmp_path, {"entry": "entry.py", "required_features": ["ENABLE_SHELL"]},
                       "raise AssertionError('unconfigured plugin imported')")
    assert manager.load_plugins() == {}
    assert manager.load_errors == {}


def test_missing_package_prevents_plugin_code_from_running(tmp_path):
    manager = _manager(tmp_path, {"entry": "entry.py", "optional_dependencies": ["maxwell_missing_package_for_test"]},
                       "raise AssertionError('unconfigured plugin imported')")
    assert manager.load_plugins() == {}
    assert "missing optional Python packages" in manager.load_errors["extension"]


@pytest.mark.parametrize("entry", ["../escape.py", "/tmp/escape.py", "..\\escape.py", "entry.txt"])
def test_invalid_plugin_entry_rejected_before_import(entry):
    with pytest.raises(ManifestError, match="entry"):
        validate_manifest({"id": "extension", "entry": entry}, directory_name="extension")


@pytest.mark.parametrize("version", [2, "nope"])
def test_unsupported_manifest_version_rejected(version):
    with pytest.raises(ManifestError, match="manifest"):
        validate_manifest({"id": "extension", "manifest_version": version}, directory_name="extension")
