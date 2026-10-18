"""Every refusal branch of MaxwellBot._authorize_tool_execution.

This is the last gate before a tool runs (native, recovered-text, alias and
internal dispatch all pass through it), so each branch is pinned here.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import tooling.helpers as helpers
from bot import MaxwellBot, _current_byok_tool_budget, _current_request_provider
from bot_common import OPERATOR_MAILBOX_TOOLS, PUBLIC_RUNTIME_BLOCKED_TOOLS

ADMIN_ID = "1001"
USER_ID = "2002"
GUILD_ID = "3003"


def _message(*, user_id=USER_ID, guild=True, visibility="public", **extra):
    msg = SimpleNamespace(
        id="m-1",
        author=SimpleNamespace(id=user_id),
        guild=SimpleNamespace(id=GUILD_ID, name="g") if guild else None,
        channel=SimpleNamespace(id="c-1"),
        response_visibility=visibility,
    )
    for key, value in extra.items():
        setattr(msg, key, value)
    return msg


class _Tool:
    def __init__(self, name="read_data", schema=None, **attrs):
        self.tool_name = name
        self._schema = schema or {"type": "object", "properties": {}}
        for key, value in attrs.items():
            setattr(self, key, value)

    def get_parameters(self):
        return self._schema


def _bot(control=None, *, admins=(ADMIN_ID,), manager=None, **config):
    bot = object.__new__(MaxwellBot)
    bot._control = {"tools_enabled": True, "disabled_tools": [], **(control or {})}
    bot.plugin_manager = manager
    bot._is_admin = lambda uid: str(uid) in set(admins)
    bot.config = SimpleNamespace(**config)
    return bot


def _auth(bot, message, name="read_data", tool=None, args=None):
    tool = tool if tool is not None else _Tool(name)
    return MaxwellBot._authorize_tool_execution(bot, message, name, tool, args)


def test_plain_tool_is_allowed():
    assert _auth(_bot(), _message()) is None


def test_missing_tool_identity_is_refused():
    tool = SimpleNamespace(tool_name="", name="")
    assert (
        MaxwellBot._authorize_tool_execution(_bot(), _message(), "  ", tool, {})
        == "refused: tool identity is missing"
    )


def test_canonical_name_comes_from_handler_not_alias():
    # An alias must not dodge a disable that names the real tool.
    bot = _bot({"disabled_tools": ["real_tool"]})
    out = MaxwellBot._authorize_tool_execution(
        bot, _message(), "alias", _Tool("real_tool"), {}
    )
    assert out == "refused: this tool is disabled"


def test_disabled_by_identifier_is_also_refused():
    bot = _bot({"disabled_tools": ["alias"]})
    out = MaxwellBot._authorize_tool_execution(
        bot, _message(), "alias", _Tool("real_tool"), {}
    )
    assert out == "refused: this tool is disabled"


def test_global_tools_switch_off():
    assert _auth(_bot({"tools_enabled": False}), _message()) == (
        "refused: tools are disabled"
    )


def test_request_older_than_privacy_deletion_is_refused():
    bot = _bot()
    bot._user_preferences = SimpleNamespace(request_allowed=lambda uid, ts: False)
    msg = _message(created_at=SimpleNamespace(timestamp=lambda: 1.0))
    assert "predates your privacy deletion" in _auth(bot, msg)


def test_request_allowed_by_preferences_passes():
    calls = []
    bot = _bot()
    bot._user_preferences = SimpleNamespace(
        request_allowed=lambda uid, ts: calls.append((uid, ts)) or True
    )
    msg = _message(created_at=SimpleNamespace(timestamp=lambda: 42.0))
    assert _auth(bot, msg) is None
    assert calls == [(USER_ID, 42.0)]


@pytest.mark.parametrize(
    "budget, refused", [(None, True), ({"calls": 24}, True), ({"calls": 23}, False)]
)
def test_personal_provider_tool_budget(budget, refused):
    p_token = _current_request_provider.set(object())
    b_token = _current_byok_tool_budget.set(budget)
    try:
        out = _auth(_bot(), _message())
    finally:
        _current_byok_tool_budget.reset(b_token)
        _current_request_provider.reset(p_token)
    assert (out == "refused: personal provider tool-call limit reached") is refused
    if not refused:
        assert out is None


def test_public_runtime_blocks_retired_tools_only_when_restricted():
    name = sorted(PUBLIC_RUNTIME_BLOCKED_TOOLS)[0]
    restricted = _bot(MAXWELL_RESTRICT_PUBLIC_RUNTIME=True)
    assert _auth(restricted, _message(user_id=ADMIN_ID), name) == (
        "refused: this tool is retired from the public runtime"
    )
    open_bot = _bot(MAXWELL_RESTRICT_PUBLIC_RUNTIME=False)
    assert _auth(open_bot, _message(user_id=ADMIN_ID), name) is None


@pytest.mark.parametrize("name", sorted(OPERATOR_MAILBOX_TOOLS))
def test_operator_mailbox_requires_operator(name):
    assert "operator mailbox" in _auth(_bot(), _message(), name)
    assert _auth(_bot(), _message(user_id=ADMIN_ID), name) is None


@pytest.mark.parametrize(
    "attrs, name",
    [
        ({"requires_admin": True}, "admin_tool"),
        ({"required_discord_permissions": ("manage_messages",)}, "perm_tool"),
        ({}, "shell"),
    ],
)
@pytest.mark.parametrize("bad_id", ["", "not-a-number", None])
def test_privileged_tools_need_a_numeric_requester(attrs, name, bad_id):
    msg = _message(user_id=bad_id)
    out = _auth(_bot(), msg, name, _Tool(name, **attrs))
    assert out == "refused: authenticated requester identity is unavailable"


def test_requires_admin_refuses_non_operator_and_allows_operator():
    tool = _Tool("admin_tool", requires_admin=True)
    assert _auth(_bot(), _message(), "admin_tool", tool) == (
        "refused: this tool is restricted to Maxwell operators"
    )
    assert _auth(_bot(), _message(user_id=ADMIN_ID), "admin_tool", tool) is None


def test_requires_admin_without_checker_fails_closed():
    bot = _bot()
    bot._is_admin = None
    tool = _Tool("admin_tool", requires_admin=True)
    assert "restricted to Maxwell operators" in _auth(
        bot, _message(user_id=ADMIN_ID), "admin_tool", tool
    )


def test_discord_permission_tool_needs_a_guild():
    tool = _Tool("perm_tool", required_discord_permissions=("manage_messages",))
    assert _auth(_bot(), _message(guild=False), "perm_tool", tool) == (
        "refused: this tool requires a server permission context"
    )


def test_discord_permission_denial_is_returned_verbatim(monkeypatch):
    seen = []

    def fake_missing(guild, permission, message, channel=None):
        seen.append(permission)
        return "Error: you lack it" if permission == "ban_members" else ""

    monkeypatch.setattr(helpers, "_missing_cap", fake_missing)
    tool = _Tool(
        "perm_tool",
        required_discord_permissions=(" manage_messages ", "", "ban_members"),
    )
    assert _auth(_bot(), _message(), "perm_tool", tool) == "Error: you lack it"
    # Blank entries are dropped and names are trimmed before checking.
    assert seen == ["manage_messages", "ban_members"]


def test_discord_permission_granted(monkeypatch):
    monkeypatch.setattr(helpers, "_missing_cap", lambda *a, **k: "")
    tool = _Tool("perm_tool", required_discord_permissions=("manage_messages",))
    assert _auth(_bot(), _message(), "perm_tool", tool) is None


class _Manager:
    def __init__(self, allowed=(), *, error=None):
        self.all_plugin_tools = {"plugin_tool": {"plugin": "demo"}}
        self._allowed = set(allowed)
        self._error = error
        self.calls = []

    def get_available_tools(self, *, user_id, platform, guild_id):
        self.calls.append((user_id, platform, guild_id))
        if self._error:
            raise self._error
        return self._allowed


def test_plugin_tool_unavailable_to_user_is_refused():
    manager = _Manager()
    out = _auth(_bot(manager=manager), _message(), "plugin_tool")
    assert out == "refused: this tool is unavailable to this user"
    assert manager.calls == [(USER_ID, "discord", GUILD_ID)]


def test_plugin_tool_available_passes_and_uses_message_platform():
    manager = _Manager({"plugin_tool"})
    msg = _message(tool_platform="user_install")
    assert _auth(_bot(manager=manager), msg, "plugin_tool") is None
    assert manager.calls[0][1] == "user_install"


def test_plugin_availability_error_fails_closed():
    manager = _Manager(error=RuntimeError("db down"))
    assert _auth(_bot(manager=manager), _message(), "plugin_tool") == (
        "refused: tool availability could not be verified"
    )


def test_non_plugin_tools_skip_the_plugin_check():
    manager = _Manager(error=RuntimeError("must not be called"))
    assert _auth(_bot(manager=manager), _message(), "read_data") is None
    assert manager.calls == []


def test_server_disabled_capability_is_refused_in_that_server_only():
    control = {"guild_disabled_capabilities": {GUILD_ID: ["web"]}}
    assert _auth(_bot(control), _message(), "web_search") == (
        "refused: this server has disabled the web capability"
    )
    assert _auth(_bot(control), _message(guild=False), "web_search") is None
    other = {"guild_disabled_capabilities": {"9999": ["web"]}}
    assert _auth(_bot(other), _message(), "web_search") is None


def test_server_disabled_plugins_capability_covers_plugin_tools():
    control = {"guild_disabled_capabilities": {GUILD_ID: ["plugins"]}}
    manager = _Manager({"plugin_tool"})
    out = _auth(_bot(control, manager=manager), _message(), "plugin_tool")
    assert out == "refused: this server has disabled the plugins capability"


def test_guild_id_falls_back_to_message_guild_id():
    control = {"guild_disabled_capabilities": {GUILD_ID: ["web"]}}
    msg = _message(guild=False, guild_id=GUILD_ID)
    assert "disabled the web capability" in _auth(_bot(control), msg, "web_search")


@pytest.mark.parametrize(
    "name", ["react", "delete_message", "ban_member", "reminder", "create_site"]
)
def test_private_visibility_blocks_public_side_effects(name):
    out = _auth(_bot(), _message(visibility="private"), name)
    assert (
        out
        == "refused: choose Public visibility before requesting a server-wide side effect"
    )


@pytest.mark.parametrize(
    "name, args",
    [
        ("edit_site", {"action": "read"}),
        ("edit_site", {}),  # default action is list
        ("site_server", {"action": "logs"}),
        ("site_server", {"action": "request", "method": "get"}),
        ("site_server", {"action": "request"}),
    ],
)
def test_private_visibility_allows_site_reads(name, args):
    assert _auth(_bot(), _message(visibility="private"), name, args=args) is None


@pytest.mark.parametrize(
    "name, args",
    [
        ("edit_site", {"action": "write"}),
        ("site_server", {"action": "request", "method": "POST"}),
        ("site_server", {"action": "restart"}),
    ],
)
def test_private_visibility_blocks_site_writes(name, args):
    assert "choose Public visibility" in _auth(
        _bot(), _message(visibility="private"), name, args=args
    )


def test_unserializable_arguments_fail_closed():
    class Loop:
        pass

    args = {"x": []}
    args["x"].append(args)  # circular reference
    assert _auth(_bot(), _message(), args=args) == (
        "refused: tool arguments are not valid JSON data"
    )
    # Non-JSON objects are stringified rather than rejected.
    assert _auth(_bot(), _message(), args={"o": Loop()}) is None


def test_oversized_arguments_are_refused():
    big = {"blob": "x" * (256 * 1024 + 1)}
    assert _auth(_bot(), _message(), args=big) == (
        "refused: tool arguments exceed the request size limit"
    )
    wide = {f"k{i}": i for i in range(129)}
    assert _auth(_bot(), _message(), args=wide) == (
        "refused: tool arguments exceed the request size limit"
    )


def test_schema_violations_are_refused_with_the_error_type_only():
    schema = {
        "type": "object",
        "properties": {"count": {"type": "integer"}},
        "required": ["count"],
        "additionalProperties": False,
    }
    tool = _Tool("typed", schema=schema)
    assert _auth(_bot(), _message(), "typed", tool, {"count": 3}) is None
    out = _auth(_bot(), _message(), "typed", tool, {"count": "three"})
    assert out == "refused: invalid arguments (ValidationError)"
    assert _auth(_bot(), _message(), "typed", tool, {}) == (
        "refused: invalid arguments (ValidationError)"
    )
    assert "ValidationError" in _auth(
        _bot(), _message(), "typed", tool, {"count": 1, "extra": 2}
    )


def test_broken_schema_getter_fails_closed():
    class Broken(_Tool):
        def get_parameters(self):
            raise RuntimeError("boom")

    assert _auth(_bot(), _message(), "b", Broken("b")) == (
        "refused: invalid arguments (RuntimeError)"
    )


def test_non_object_schema_is_not_enforced():
    tool = _Tool("loose", schema={"type": "string"})
    assert _auth(_bot(), _message(), "loose", tool, {"anything": 1}) is None


def test_worker_cannot_change_github_accounts():
    from plugins.maxwell_extras.task_workers import (
        GITHUB_ACCOUNT_ACTIONS,
        _inside_worker,
    )

    action = sorted(GITHUB_ACCOUNT_ACTIONS)[0]
    tool = _Tool("github_repo", schema={"type": "object"})
    token = _inside_worker.set(True)
    try:
        refused = _auth(_bot(), _message(), "github_repo", tool, {"action": action})
        with_token = _auth(_bot(), _message(), "github_repo", tool, {"token": "x"})
        allowed = _auth(_bot(), _message(), "github_repo", tool, {"action": "status"})
    finally:
        _inside_worker.reset(token)
    assert (
        refused == "refused: workers cannot change accounts or repository permissions"
    )
    assert with_token == refused
    assert allowed is None
    # Outside a worker the same call is not blocked by this rule.
    assert _auth(_bot(), _message(), "github_repo", tool, {"action": action}) is None


@pytest.mark.parametrize("name", ["send_message", "send_rich_message", "react", "no_response"])
def test_spent_personal_budget_still_lets_the_turn_reply(name):
    p_token = _current_request_provider.set(object())
    b_token = _current_byok_tool_budget.set({"calls": 24})
    try:
        out = _auth(_bot(), _message(), name=name)
    finally:
        _current_byok_tool_budget.reset(b_token)
        _current_request_provider.reset(p_token)
    assert out is None
