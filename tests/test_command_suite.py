from __future__ import annotations

import asyncio
from types import SimpleNamespace

from plugins.maxwell_extras import command_suite
from plugins.maxwell_extras.user_preferences import UserPreferenceStore
from bot import MaxwellBot


def test_command_suite_registers_purpose_commands_and_no_owner_command():
    definitions = command_suite.command_definitions()
    names = {row["name"] for row in definitions}
    names.update({"diagnostics", "maintenance"})
    assert {
        "config",
        "personality",
        "image",
        "chess",
        "checkers",
        "moderation",
        "memory",
        "reminder",
        "diagnostics",
        "maintenance",
    } <= names
    assert "owner" not in names
    image = next(row for row in definitions if row["name"] == "image")
    assert any(option["name"] == "image" and option["type"] == 11 for option in image["options"])


def test_prompt_edit_slash_commands_and_config_fields_are_removed():
    definitions = command_suite.command_definitions()
    names = {row["name"] for row in definitions}
    assert "server-prompt" not in names
    assert "clear-server-prompt" not in names
    assert set(command_suite._SERVER_SETTINGS) == {"progress", "ticket"}


def test_prefix_commands_are_retired_but_unknown_prefixed_text_is_not_a_command():
    bot = SimpleNamespace(command_prefix=",")
    retired = SimpleNamespace(content=",help")
    unknown = SimpleNamespace(content=",hello Maxwell")
    assert MaxwellBot._retired_prefix_command_name(bot, retired) == "help"
    assert MaxwellBot._retired_prefix_command_name(bot, unknown) == ""


def test_user_preferences_are_isolated_and_resettable(tmp_path):
    store = UserPreferenceStore(tmp_path / "user_preferences.json")
    store.set_default("100", "mode", "research")
    store.set_default("100", "context", 10)
    store.set_personality("100", "Keep replies concise.")
    store.set_default("200", "mode", "code")

    first = store.get("100")
    second = store.get("200")
    assert first["defaults"]["mode"] == "research"
    assert first["defaults"]["context"] == 10
    assert first["personality"] == "Keep replies concise."
    assert second["defaults"]["mode"] == "code"
    assert second["personality"] == ""

    store.reset_default("100", "mode")
    store.set_personality("100", "")
    reset = store.get("100")
    assert reset["defaults"]["mode"] == "ask"
    assert reset["personality"] == ""


def test_personality_has_a_bounded_size(tmp_path):
    store = UserPreferenceStore(tmp_path / "user_preferences.json")
    try:
        store.set_personality("100", "x" * 801)
    except ValueError as exc:
        assert "800" in str(exc)
    else:
        raise AssertionError("oversized personal style should be rejected")


def test_server_config_requires_server_privileges():
    bot = SimpleNamespace(_is_admin=lambda _uid: False)
    interaction = SimpleNamespace(
        guild=SimpleNamespace(id=10, owner_id=1),
        guild_id=10,
        user=SimpleNamespace(id=2),
        member=SimpleNamespace(
            guild_permissions=SimpleNamespace(manage_guild=False, administrator=False)
        ),
        permissions=SimpleNamespace(manage_guild=False, administrator=False),
    )
    assert not command_suite._can_manage_server(bot, interaction)
    interaction.member.guild_permissions.manage_guild = True
    assert command_suite._can_manage_server(bot, interaction)


def test_config_opens_a_private_settings_menu(tmp_path):
    store = UserPreferenceStore(tmp_path / "user_preferences.json")
    responses = []

    class Response:
        def is_done(self):
            return False

        async def send_message(self, text, *, view=None, ephemeral=False):
            responses.append((text, view, ephemeral))

    bot = SimpleNamespace(_user_preferences=store)
    interaction = SimpleNamespace(
        data={"name": "config"},
        user=SimpleNamespace(id=100),
        response=Response(),
    )

    assert asyncio.run(command_suite._handle_config(bot, interaction))
    assert responses and responses[0][2] is True
    assert "Your personal settings" in responses[0][0]
    assert isinstance(responses[0][1], command_suite._ConfigPanel)
    assert not responses[0][1].can_choose_scope
    assert store.get("100")["defaults"]["mode"] == "ask"


def test_config_value_menu_updates_a_personal_default(tmp_path):
    store = UserPreferenceStore(tmp_path / "user_preferences.json")
    interaction = SimpleNamespace(user=SimpleNamespace(id=100))
    bot = SimpleNamespace(_user_preferences=store)
    panel = command_suite._ConfigPanel(bot, store, interaction)
    responses = []

    class ComponentResponse:
        async def edit_message(self, content, *, view):
            responses.append((content, view))

    interaction.response = ComponentResponse()
    asyncio.run(panel.set_choice(interaction, "research"))
    assert store.get("100")["defaults"]["mode"] == "research"
    assert responses and "Research" in responses[0][0]


def test_config_server_controls_are_hidden_without_manage_permissions(tmp_path):
    store = UserPreferenceStore(tmp_path / "user_preferences.json")
    bot = SimpleNamespace(_user_preferences=store, _is_admin=lambda _uid: False)
    interaction = SimpleNamespace(
        guild=SimpleNamespace(id=10, owner_id=1, name="Test server"),
        guild_id=10,
        user=SimpleNamespace(id=2),
        member=SimpleNamespace(
            guild_permissions=SimpleNamespace(manage_guild=False, administrator=False)
        ),
    )
    panel = command_suite._ConfigPanel(bot, store, interaction)
    assert not panel.can_choose_scope
    assert not any(
        isinstance(item, command_suite._ConfigScopeSelect)
        for item in panel.children
    )


def test_config_rechecks_server_permission_when_a_menu_is_used(tmp_path):
    store = UserPreferenceStore(tmp_path / "user_preferences.json")
    permissions = SimpleNamespace(manage_guild=True, administrator=False)
    bot = SimpleNamespace(_user_preferences=store, _is_admin=lambda _uid: False)
    opened_by = SimpleNamespace(
        guild=SimpleNamespace(id=10, owner_id=1, name="Test server"),
        guild_id=10,
        user=SimpleNamespace(id=2),
        member=SimpleNamespace(guild_permissions=permissions),
    )
    panel = command_suite._ConfigPanel(bot, store, opened_by)
    assert panel.can_choose_scope
    panel.scope = "server"
    panel.selected_key = "progress"

    denied = []

    class Response:
        def is_done(self):
            return False

        async def send_message(self, text, *, ephemeral=False):
            denied.append((text, ephemeral))

    permissions.manage_guild = False
    click = SimpleNamespace(
        guild=opened_by.guild,
        guild_id=10,
        user=opened_by.user,
        member=opened_by.member,
        response=Response(),
    )
    assert asyncio.run(panel.interaction_check(click)) is False
    assert panel.scope == "personal"
    assert denied and denied[0][1] is True


def test_config_does_not_open_server_settings_after_permission_is_revoked(tmp_path):
    store = UserPreferenceStore(tmp_path / "user_preferences.json")
    permissions = SimpleNamespace(manage_guild=True, administrator=False)
    bot = SimpleNamespace(_user_preferences=store, _is_admin=lambda _uid: False)
    opened_by = SimpleNamespace(
        guild=SimpleNamespace(id=10, owner_id=1, name="Test server"),
        guild_id=10,
        user=SimpleNamespace(id=2),
        member=SimpleNamespace(guild_permissions=permissions),
    )
    panel = command_suite._ConfigPanel(bot, store, opened_by)
    scope_select = next(
        item for item in panel.children
        if isinstance(item, command_suite._ConfigScopeSelect)
    )
    scope_select._values = ["server"]
    denied = []

    class Response:
        def is_done(self):
            return False

        async def send_message(self, text, *, ephemeral=False):
            denied.append((text, ephemeral))

    permissions.manage_guild = False
    click = SimpleNamespace(
        guild=opened_by.guild,
        guild_id=10,
        user=opened_by.user,
        member=opened_by.member,
        response=Response(),
    )
    asyncio.run(scope_select.callback(click))
    assert panel.scope == "personal"
    assert denied and "permission" in denied[0][0].lower()


def test_config_text_settings_open_a_bounded_modal(tmp_path):
    store = UserPreferenceStore(tmp_path / "user_preferences.json")
    interaction = SimpleNamespace(user=SimpleNamespace(id=100))
    panel = command_suite._ConfigPanel(SimpleNamespace(_user_preferences=store), store, interaction)
    panel.selected_key = "language"
    panel._build()
    modal = panel.edit_modal()
    assert modal is not None
    assert modal.title == "Edit response language"
    assert modal.children[0].max_length == 80
