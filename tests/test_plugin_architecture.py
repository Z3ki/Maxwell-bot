"""Architectural contracts for the plugin host."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from maxwell_core.plugins.catalog import discover_plugin_manifests, discover_tool_names
from maxwell_core.plugins.manifest import ManifestError, validate_manifest
from maxwell_core.prompts.component import PromptComponent, PromptRequest
from maxwell_core.prompts.manager import PromptManager
from maxwell_core.tools.registry import ToolRegistry
from plugin_manager import PluginManager
from tools import Tool

ROOT = Path(__file__).resolve().parents[1]


def test_core_does_not_import_feature_plugins():
    tree = ast.parse((ROOT / "maxwell_core").joinpath("plugins/manager.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert not node.module.startswith("plugins.chess")
            assert not node.module.startswith("plugins.web")
            assert not node.module.startswith("plugins.sites")


def test_bot_setup_tools_does_not_hardcode_tool_names():
    text = (ROOT / "bot.py").read_text()
    assert 'self.tools["web_search"]' not in text
    assert 'self.tools["chess_start"]' not in text
    assert "plugin_manager.load_plugins()" in text


def test_manifests_declare_unique_tool_names():
    names = discover_tool_names()
    assert "web_search" in names
    assert "send_message" in names
    assert "manage_plugin" in names
    assert len(names) == len(set(names))


def test_invalid_manifest_is_rejected():
    with pytest.raises(ManifestError, match="API"):
        validate_manifest(
            {"id": "nope", "api_version": 99},
            directory_name="nope",
        )


def test_duplicate_tools_are_rejected(tmp_path):
    class A(Tool):
        tool_name = "shared"
        returns_result = True

        def get_description(self):
            return "a"

        async def execute(self, message, **kwargs):
            return "a"

    class B(A):
        def get_description(self):
            return "b"

    bot = SimpleNamespace(tools={})
    pm = PluginManager(
        bot,
        plugins_dir=str(tmp_path / "plugins"),
        data_dir=str(tmp_path / "data"),
        state_file=str(tmp_path / "data" / "plugins.json"),
    )
    pm.tool_registry.register_tool(A(bot), name="shared", plugin="one")
    with pytest.raises(ValueError, match="already registered"):
        pm.tool_registry.register_tool(B(bot), name="shared", plugin="two")


def test_prompt_components_are_scoped_to_enabled_tools():
    manager = PromptManager()
    manager.register(
        PromptComponent(
            id="chess.prompt",
            plugin="chess",
            text="chess: you play your own moves.",
            position="tools",
            requires_tools=("chess_move",),
        )
    )
    manager.register(
        PromptComponent(
            id="core.protocol",
            plugin="core",
            text="Use send_message.",
            position="protocol",
        )
    )
    without = manager.assemble(
        PromptRequest(tool_names=("web_search",), plugin_ids=("web",)),
        enabled_plugins=("core", "web"),
    )
    assert "you play your own moves" not in without
    with_chess = manager.assemble(
        PromptRequest(tool_names=("chess_move",), plugin_ids=("chess",)),
        enabled_plugins=("core", "chess"),
    )
    assert "you play your own moves" in with_chess


def test_disabling_a_plugin_hides_tools_and_keeps_data(tmp_path):
    plugins = tmp_path / "plugins" / "echo"
    plugins.mkdir(parents=True)
    (plugins / "plugin.json").write_text(
        json.dumps(
            {
                "id": "echo",
                "version": "1.0.0",
                "api_version": 1,
                "enabled_globally": True,
                "tools": [{"name": "echo_tool", "returns_result": True}],
            }
        )
    )
    (plugins / "__init__.py").write_text(
        """
from tools import Tool

class Echo(Tool):
    tool_name = "echo_tool"
    returns_result = True
    def get_description(self):
        return "echo"
    def get_parameters(self):
        return {"type": "object", "properties": {"text": {"type": "string"}}}
    async def execute(self, message, text="", **kwargs):
        return text

def setup(bot, ctx):
    ctx.store_path("keep.json").write_text("alive", encoding="utf-8")
    return [Echo(bot)]
"""
    )
    data = tmp_path / "data"
    bot = SimpleNamespace(tools={})
    pm = PluginManager(
        bot,
        plugins_dir=str(tmp_path / "plugins"),
        data_dir=str(data),
        state_file=str(data / "plugins.json"),
    )
    pm.load_plugins()
    assert "echo_tool" in pm.get_available_tools(user_id="1")
    keep = data / "plugins" / "echo" / "keep.json"
    assert keep.read_text(encoding="utf-8") == "alive"
    msg = pm.disable_plugin("echo", is_global=True)
    assert "disabled GLOBALLY" in msg
    assert "echo_tool" not in pm.get_available_tools(user_id="1")
    assert keep.exists()


def test_sample_plugin_installs_without_core_edits(tmp_path):
    src = ROOT / "examples" / "sample_plugin"
    dest = tmp_path / "plugins" / "sample_echo"
    dest.mkdir(parents=True)
    dest.joinpath("plugin.json").write_text(
        (src / "plugin.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    dest.joinpath("__init__.py").write_text(
        (src / "__init__.py").read_text(encoding="utf-8"), encoding="utf-8"
    )
    bot = SimpleNamespace(tools={})
    pm = PluginManager(
        bot,
        plugins_dir=str(tmp_path / "plugins"),
        data_dir=str(tmp_path / "data"),
        state_file=str(tmp_path / "data" / "plugins.json"),
    )
    loaded = pm.load_plugins()
    assert "sample_echo" in loaded
    assert "sample_echo" in loaded["sample_echo"]["tools"]
    spec = pm.tool_registry.get("sample_echo")
    assert spec is not None
    assert spec.returns_result is True
    assert spec.schema()["properties"]["text"]["type"] == "string"


def test_every_bundled_manifest_is_valid():
    errors = []
    for path, manifest, error in discover_plugin_manifests():
        if error:
            errors.append(f"{path.name}: {error}")
            continue
        assert manifest is not None
        assert manifest.api_version == 1
    assert errors == []


def test_tool_registry_contracts_are_exclusive():
    registry = ToolRegistry()

    class ResultTool(Tool):
        returns_result = True
        ends_turn = True

        def get_description(self):
            return "x"

        async def execute(self, message, **kwargs):
            return "x"

    registry.register_tool(ResultTool(None), name="bad", plugin="demo")
    problems = registry.validate_contracts()
    assert any("cannot both" in p for p in problems)
