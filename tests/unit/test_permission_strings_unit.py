"""A bare permission string is one requirement; bad declarations fail closed."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import tooling.helpers as helpers
from bot import MaxwellBot
from maxwell_core.tools.registry import ToolRegistry
from maxwell_core.tools.spec import normalize_permission_names, spec_from_tool

USER_ID = "2002"


class _Tool:
    def __init__(self, name="perm_tool", **attrs):
        self.tool_name = name
        for key, value in attrs.items():
            setattr(self, key, value)

    def get_parameters(self):
        return {"type": "object", "properties": {}}


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, ()),
        ((), ()),
        ([], ()),
        ("", ()),
        ("manage_messages", ("manage_messages",)),
        ("  ban_members ", ("ban_members",)),
        (["a", " a ", "", "b"], ("a", "b")),
        (("x", "y"), ("x", "y")),
        ({"shell", "network"}, ("network", "shell")),
        (frozenset({"files.read"}), ("files.read",)),
    ],
)
def test_normalize_permission_names(value, expected):
    assert normalize_permission_names(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        5,
        1.5,
        True,
        False,
        0,
        {"manage_messages": True},
        {},
        b"shell",
        ["ok", 3],
        [None],
    ],
)
def test_normalize_permission_names_rejects_invalid_types(value):
    with pytest.raises(TypeError):
        normalize_permission_names(value, field_name="required_capabilities")


def test_spec_keeps_a_string_permission_whole():
    spec = spec_from_tool(
        _Tool(
            required_capabilities="shell",
            required_discord_permissions="manage_messages",
        ),
        name="perm_tool",
        plugin="demo",
    )
    assert spec.required_capabilities == ("shell",)
    assert spec.required_discord_permissions == ("manage_messages",)
    assert spec.as_public_dict()["required_capabilities"] == ["shell"]


def test_spec_list_permissions_unchanged():
    spec = spec_from_tool(
        _Tool(required_capabilities=("network", "files.read")),
        name="perm_tool",
        plugin="demo",
    )
    assert spec.required_capabilities == ("network", "files.read")
    assert spec.required_discord_permissions == ()


@pytest.mark.parametrize(
    "attr", ["required_capabilities", "required_discord_permissions"]
)
@pytest.mark.parametrize("bad", [7, {"manage_messages": True}, ["ok", 1]])
def test_invalid_permission_declaration_is_never_registered(attr, bad):
    registry = ToolRegistry()
    with pytest.raises(TypeError):
        registry.register_tool(_Tool(**{attr: bad}), name="perm_tool", plugin="demo")
    assert registry.get("perm_tool") is None


def _bot():
    bot = object.__new__(MaxwellBot)
    bot._control = {"tools_enabled": True, "disabled_tools": []}
    bot.plugin_manager = None
    bot._is_admin = lambda uid: False
    bot.config = SimpleNamespace()
    return bot


def _message(guild=True):
    return SimpleNamespace(
        id="m-1",
        author=SimpleNamespace(id=USER_ID),
        guild=SimpleNamespace(id="3003", name="g") if guild else None,
        channel=SimpleNamespace(id="c-1"),
        response_visibility="public",
    )


def _auth(tool, message=None):
    return MaxwellBot._authorize_tool_execution(
        _bot(), message or _message(), tool.tool_name, tool, {}
    )


def test_gate_checks_a_string_permission_as_one_permission(monkeypatch):
    seen = []

    def fake_missing(guild, permission, message, channel=None):
        seen.append(permission)
        return "Error: you lack it"

    monkeypatch.setattr(helpers, "_missing_cap", fake_missing)
    out = _auth(_Tool(required_discord_permissions="manage_messages"))
    assert out == "Error: you lack it"
    assert seen == ["manage_messages"]


def test_gate_allows_string_permission_only_when_granted(monkeypatch):
    seen = []
    monkeypatch.setattr(
        helpers, "_missing_cap", lambda g, p, m, channel=None: seen.append(p) or ""
    )
    assert _auth(_Tool(required_discord_permissions="manage_messages")) is None
    assert seen == ["manage_messages"]


def test_gate_string_permission_still_needs_a_guild():
    assert _auth(
        _Tool(required_discord_permissions="manage_messages"), _message(guild=False)
    ) == ("refused: this tool requires a server permission context")


@pytest.mark.parametrize("bad", [7, {"manage_messages": True}, ["manage_messages", 1]])
def test_gate_refuses_invalid_permission_declarations(monkeypatch, bad):
    monkeypatch.setattr(helpers, "_missing_cap", lambda *a, **k: "")
    assert _auth(_Tool(required_discord_permissions=bad)) == (
        "refused: this tool declares invalid permission requirements"
    )
