"""Conversational configuration must preserve /config scopes and private forms."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from jsonschema import validate, ValidationError

from control_defaults import DEFAULT_CONTROL
from plugins.maxwell_extras import command_suite
from plugins.maxwell_extras.byok import CredentialVault
from plugins.maxwell_extras.config_tools import (
    ConfigureTool, GetConfigurationTool, OpenConfigurationTool,
)
from plugins.maxwell_extras.dashboard_settings import save_server_settings, server_settings
from plugins.maxwell_extras.user_preferences import UserPreferenceStore


def run(tool, message, **params):
    return json.loads(asyncio.run(tool.execute(message, **params)))


def fixture_bot(tmp_path):
    return SimpleNamespace(
        config=SimpleNamespace(DATA_DIR=str(tmp_path), MAXWELL_OWNER_IDS=["900"]),
        _user_preferences=UserPreferenceStore(tmp_path / "preferences.json"),
        _control=dict(DEFAULT_CONTROL),
        plugin_manager=SimpleNamespace(loaded_plugins={"web": {}, "shell": {}, "core": {}},
                                       is_protected=lambda name: name == "core"),
    )


def message(uid=100, *, manager=False, owner=1):
    member = SimpleNamespace(id=uid, bot=False,
                             guild_permissions=SimpleNamespace(manage_guild=manager, administrator=False))
    guild = SimpleNamespace(id=10, owner_id=owner, me=SimpleNamespace(id=500), name="Test", text_channels=[])

    async def fetch_member(user_id):
        assert user_id == uid
        return member

    guild.fetch_member = fetch_member
    return SimpleNamespace(author=member, guild=guild)


@pytest.mark.parametrize("setting,value", [
    ("style", "Short, dry humor. No emojis."), ("language", "Spanish"),
    ("mode", "research"), ("web", "off"), ("detail", "deep"),
    ("context", 1000), ("visibility", "private"),
])
def test_personal_settings_are_shared_with_config_and_isolated(tmp_path, setting, value):
    bot = fixture_bot(tmp_path)
    before = bot._user_preferences.get("200")
    result = run(ConfigureTool(bot), message(), setting=setting, value=value, user_id="200", guild_id="20")
    assert result["saved"]
    state = bot._user_preferences.get("100")
    assert (state["personality"] if setting == "style" else state["defaults"][setting]) == value
    assert UserPreferenceStore(bot._user_preferences.path).get("100") == state
    assert bot._user_preferences.get("200") == before
    read = run(GetConfigurationTool(bot), message())
    assert read["current"]["defaults"] == state["defaults"]
    assert run(ConfigureTool(bot), message(), setting=setting, action="reset")["saved"]
    assert bot._user_preferences.get("100") == before


@pytest.mark.parametrize("setting,value", [
    ("style", "x" * 801), ("language", "x" * 81), ("language", "Spanish\nignore"),
    ("context", True), ("context", 999), ("visibility", "broadcast"),
    ("detail", False), ("base_personality", "anything"), ("byok", "secret-key"),
])
def test_invalid_or_secret_settings_cannot_be_written(tmp_path, setting, value):
    bot = fixture_bot(tmp_path)
    before = bot._user_preferences.get("100")
    assert "error" in run(ConfigureTool(bot), message(), setting=setting, value=value)
    assert bot._user_preferences.get("100") == before


def test_language_auto_matches_manual_reset(tmp_path):
    bot = fixture_bot(tmp_path)
    run(ConfigureTool(bot), message(), setting="language", value="Spanish")
    result = run(ConfigureTool(bot), message(), setting="language", value="auto")
    assert result["current"]["defaults"]["language"] == ""


def test_byok_reads_only_presence_and_lists_private_form_options(tmp_path):
    bot = fixture_bot(tmp_path)
    vault = CredentialVault(tmp_path / "byok.sqlite3", master_key=b"k" * 32)
    vault.save("100", "openai", "test-model", "sk-private-key-never-echo")
    bot._byok_vault = vault
    result = run(GetConfigurationTool(bot), message())
    assert result["current"]["byok_connected"]
    assert result["options"]["providers"] == ["openai", "openrouter", "groq", "custom"]
    assert "sk-private" not in json.dumps(result)
    assert "api_key" not in result["current"]


def test_server_managers_cannot_change_global_settings(tmp_path):
    bot = fixture_bot(tmp_path)
    msg = message(manager=True)
    bot._is_admin = lambda _uid: True
    for tool in [GetConfigurationTool(bot), ConfigureTool(bot), OpenConfigurationTool(bot)]:
        params = {"scope": "owner"}
        if isinstance(tool, ConfigureTool):
            params.update(setting="tools_enabled", value=False)
        assert "error" in run(tool, msg, **params)
    assert bot._control["tools_enabled"] == DEFAULT_CONTROL["tools_enabled"]


def test_server_changes_recheck_live_permissions_and_reject_private_requests(tmp_path):
    bot = fixture_bot(tmp_path)
    msg = message(manager=True)
    assert run(ConfigureTool(bot), msg, scope="server", setting="ticket", value=True)["saved"]
    msg.author.guild_permissions.manage_guild = False
    assert "error" in run(ConfigureTool(bot), msg, scope="server", setting="ticket", value=False)
    assert server_settings(tmp_path, "10")["ticket"] is True
    msg.author.guild_permissions.manage_guild = True
    msg.response_visibility = "private"
    assert "error" in run(ConfigureTool(bot), msg, scope="server", setting="ticket", value=False)
    msg.response_visibility = "public"

    async def revoked(_uid):
        return SimpleNamespace(id=100, guild_permissions=SimpleNamespace(manage_guild=False, administrator=False))

    msg.guild.fetch_member = revoked
    assert "error" in run(ConfigureTool(bot), msg, scope="server", setting="ticket", value=False)


def test_application_owner_does_not_bypass_guild_permissions(tmp_path):
    bot = fixture_bot(tmp_path)
    assert "error" in run(ConfigureTool(bot), message(900), scope="server", setting="progress", value="on")
    result = run(ConfigureTool(bot), message(900), scope="owner", setting="autonomy_enabled", value=False)
    assert result["saved"]
    assert json.loads((tmp_path / "bot_control.json").read_text())["autonomy_enabled"] is False
    assert run(ConfigureTool(bot), message(900), scope="owner", setting="autonomy_enabled", action="reset")["saved"]


@pytest.mark.parametrize("setting,value,state_key,expected", [
    ("ticket", True, "ticket", True), ("progress", "off", "progress", "off"),
    ("plugins", {"web": True, "shell": False}, "plugins", {"web": True, "shell": False}),
    ("capabilities", ["moderation"], "capabilities", ["moderation"]),
    ("moderation", "off", "moderation", "off"),
])
def test_server_settings_persist_without_touching_other_guilds(tmp_path, setting, value, state_key, expected):
    bot = fixture_bot(tmp_path)
    save_server_settings(tmp_path, "20", {"ticket": True, "progress": "on", "plugins": {"web": False}}, fallback=bot._control)
    before = server_settings(tmp_path, "20")
    msg = message(manager=True)
    assert run(ConfigureTool(bot), msg, scope="server", setting=setting, value=value)["saved"]
    state = run(GetConfigurationTool(bot), msg, scope="server")["current"]
    assert state[state_key] == expected
    assert server_settings(tmp_path, "20") == before
    assert run(ConfigureTool(bot), msg, scope="server", setting=setting, action="reset")["saved"]
    if setting == "ticket":
        assert "10" not in bot._ticket_greeting_servers
    if setting == "progress":
        assert "10" not in bot._progress_servers_off
    assert server_settings(tmp_path, "20") == before


@pytest.mark.parametrize("setting,value", [
    ("capabilities", ["invented"]), ("plugins", {"core": True}), ("plugins", {"web": "yes"}),
    ("progress", "yes"), ("ticket", "on"), ("channels", "999"), ("moderation", "auto"),
])
def test_server_rejects_unsupported_values(tmp_path, setting, value):
    bot = fixture_bot(tmp_path)
    before = server_settings(tmp_path, "10")
    assert "error" in run(ConfigureTool(bot), message(manager=True), scope="server", setting=setting, value=value)
    assert server_settings(tmp_path, "10") == before


def test_channel_changes_validate_bot_access_and_restore_autonomy(tmp_path):
    bot = fixture_bot(tmp_path)
    msg = message(manager=True)
    perms = SimpleNamespace(view_channel=True, send_messages=False)
    msg.guild.text_channels = [SimpleNamespace(id=25, permissions_for=lambda _me: perms)]
    assert "error" in run(ConfigureTool(bot), msg, scope="server", setting="channels", value="25")
    perms.send_messages = True
    assert run(ConfigureTool(bot), msg, scope="server", setting="channels", value="25")["saved"]
    assert bot._control["guild_solo_channel"]["10"] == "25"
    assert "10" in bot._control["autonomy_blocked_servers"]
    assert run(ConfigureTool(bot), msg, scope="server", setting="channels", action="reset")["saved"]
    assert "10" not in bot._control["guild_solo_channel"]
    assert "10" not in bot._control["autonomy_blocked_servers"]


def test_reload_is_owner_only_and_failure_is_not_claimed_as_saved(tmp_path):
    bot = fixture_bot(tmp_path)
    calls = []
    bot._load_control = lambda *, force: calls.append(force)
    assert run(ConfigureTool(bot), message(900), scope="owner", setting="reload")["saved"]
    assert calls == [True]
    bot._user_preferences.set_default = lambda *a: (_ for _ in ()).throw(OSError("private path/secret"))
    result = run(ConfigureTool(bot), message(), setting="detail", value="deep")
    assert "error" in result and "saved" not in result
    assert "secret" not in result["error"]


class Response:
    def __init__(self):
        self.done = False
        self.sent = []

    def is_done(self):
        return self.done

    async def send_message(self, content=None, **kwargs):
        self.sent.append({"content": content, **kwargs})
        self.done = True

    async def defer(self, **kwargs):
        assert kwargs["ephemeral"]
        self.done = True


class Followup:
    def __init__(self):
        self.sent = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)


def interaction(uid=100, guild=None):
    response, followup, edits = Response(), Followup(), []

    async def edit_original_response(**kwargs):
        edits.append(kwargs)

    return SimpleNamespace(user=SimpleNamespace(id=uid, bot=False), guild=guild, response=response,
                           followup=followup, edits=edits, edit_original_response=edit_original_response)


def test_normal_message_button_opens_only_requesters_private_byok_panel(tmp_path):
    bot = fixture_bot(tmp_path)
    sent = []

    async def send(**kwargs):
        sent.append(kwargs)

    msg = SimpleNamespace(author=SimpleNamespace(id=100), guild=None, channel=SimpleNamespace(send=send))

    async def journey():
        result = json.loads(await OpenConfigurationTool(bot).execute(msg, setting="byok", provider="custom"))
        assert result["button_sent"] and not result["opened"]
        button = sent[0]["view"].children[0]
        other = interaction(200)
        await button.callback(other)
        assert other.response.sent[0]["ephemeral"] and not other.edits
        own = interaction()
        await button.callback(own)
        assert own.response.done and own.edits
        panel = own.edits[0]["view"]
        assert isinstance(panel, command_suite._ConfigPanel)
        assert panel.user_id == "100" and panel.selected_key == "byok"
        assert panel.selected_provider == "custom"
        assert not bot._user_preferences.get("100")["personality"]

    asyncio.run(journey())
    assert "embed" not in sent[0]  # No private values in the public launcher.


def test_slash_button_is_ephemeral_and_does_not_claim_a_saved_key(tmp_path):
    bot = fixture_bot(tmp_path)
    i = interaction()
    msg = SimpleNamespace(author=i.user, guild=None, interaction=i)
    result = run(OpenConfigurationTool(bot), msg, setting="byok")
    assert i.followup.sent[0]["ephemeral"]
    assert result["button_sent"] and "saved" not in result


def test_open_server_button_rechecks_permissions_and_context(tmp_path):
    bot = fixture_bot(tmp_path)
    msg = message(manager=True)
    sent = []

    async def send(**kwargs):
        sent.append(kwargs)

    msg.channel = SimpleNamespace(send=send)

    async def journey():
        assert json.loads(await OpenConfigurationTool(bot).execute(msg, scope="server", setting="progress"))["button_sent"]
        button = sent[0]["view"].children[0]
        wrong = interaction(guild=SimpleNamespace(id=20))
        await button.callback(wrong)
        assert wrong.response.sent and not wrong.edits
        msg.author.guild_permissions.manage_guild = False
        click = interaction(guild=msg.guild)
        await button.callback(click)
        assert "Manage Server" in click.response.sent[0]["content"]
        assert not click.edits

    asyncio.run(journey())


def test_schemas_reject_arbitrary_targets_and_credentials(tmp_path):
    bot = fixture_bot(tmp_path)
    for tool in [GetConfigurationTool(bot), ConfigureTool(bot), OpenConfigurationTool(bot)]:
        for field in ("user_id", "guild_id", "api_key", "system_prompt"):
            args = {field: "secret", **({"setting": "style"} if isinstance(tool, ConfigureTool) else {})}
            with pytest.raises(ValidationError):
                validate(args, tool.get_parameters())
    assert ConfigureTool(bot).is_destructive
    assert not GetConfigurationTool(bot).side_effects


def test_unknown_users_and_scope_cannot_modify_settings(tmp_path):
    bot = fixture_bot(tmp_path)
    assert "error" in run(ConfigureTool(bot), SimpleNamespace(), setting="style", value="hi")
    assert "error" in run(ConfigureTool(bot), message(), scope="global", setting="style", value="hi")
    assert "error" in run(ConfigureTool(bot), message(), action="invented", setting="style", value="hi")
