"""
Unit tests for Maxwell's Plugin Architecture.
"""
import json
import os
import shutil
import tempfile
import pytest

from plugin_manager import PluginManager


class DummyBot:
    def __init__(self):
        self.tools = {}

    def _is_admin(self, user_id):
        return str(user_id) == "123456789"


@pytest.fixture
def temp_plugin_env(tmp_path):
    plugins_dir = tmp_path / "plugins"
    data_dir = tmp_path / "data"
    plugins_dir.mkdir()
    data_dir.mkdir()

    # Create a dummy plugin
    dummy_plug = plugins_dir / "test_plugin"
    dummy_plug.mkdir()
    (dummy_plug / "plugin.json").write_text(
        json.dumps({
            "name": "test_plugin",
            "version": "1.0.0",
            "description": "A test plugin",
            "enabled_globally": False,
            "allowed_users": ["111"],
            "denied_users": ["222"],
        })
    )
    (dummy_plug / "__init__.py").write_text(
        """
class DummyTool:
    def get_name(self):
        return "test_tool"

def setup(bot):
    return [DummyTool()]
"""
    )
    return plugins_dir, data_dir


def test_plugin_loading_and_scoping(temp_plugin_env):
    plugins_dir, data_dir = temp_plugin_env
    bot = DummyBot()
    state_file = data_dir / "plugins.json"

    pm = PluginManager(bot, plugins_dir=str(plugins_dir), state_file=str(state_file))
    loaded = pm.load_plugins()

    assert "test_plugin" in loaded
    assert "test_tool" in loaded["test_plugin"]["tools"]

    # User 111 is explicitly allowed
    user_tools = pm.get_available_tools(user_id="111")
    assert "test_tool" in user_tools

    # User 999 is not allowed (since enabled_globally is False)
    user_tools_999 = pm.get_available_tools(user_id="999")
    assert "test_tool" not in user_tools_999

    # Enable globally
    pm.enable_plugin("test_plugin", is_global=True)
    user_tools_999_after = pm.get_available_tools(user_id="999")
    assert "test_tool" in user_tools_999_after

    # Denied user 222 should still not have access when global
    pm.disable_plugin("test_plugin", user_id="222")
    user_tools_222 = pm.get_available_tools(user_id="222")
    assert "test_tool" not in user_tools_222


def test_user_self_enable_disable(temp_plugin_env):
    plugins_dir, data_dir = temp_plugin_env
    bot = DummyBot()
    state_file = data_dir / "plugins.json"

    pm = PluginManager(bot, plugins_dir=str(plugins_dir), state_file=str(state_file))
    pm.load_plugins()

    # User 333 enables for self
    pm.enable_plugin("test_plugin", user_id="333")
    assert "test_tool" in pm.get_available_tools(user_id="333")

    # User 333 disables for self
    pm.disable_plugin("test_plugin", user_id="333")
    assert "test_tool" not in pm.get_available_tools(user_id="333")
