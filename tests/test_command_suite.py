from __future__ import annotations

import asyncio
from types import SimpleNamespace

from plugins.maxwell_extras import command_suite
from plugins.maxwell_extras.user_preferences import UserPreferenceStore
from bot import MaxwellBot


def test_command_suite_keeps_only_config_and_cancellation_as_standalone_commands():
    definitions = command_suite.command_definitions()
    names = {row["name"] for row in definitions}
    assert names == {"config", "cancel"}
    assert not names.intersection({"personality", "image", "chess", "checkers", "moderation", "memory", "reminder", "diagnostics", "maintenance"})


def test_prompt_edit_slash_commands_and_config_fields_are_removed():
    definitions = command_suite.command_definitions()
    names = {row["name"] for row in definitions}
    assert "server-prompt" not in names
    assert "clear-server-prompt" not in names
    assert set(command_suite._SERVER_SETTINGS) == {
        "channels", "plugins", "capabilities", "moderation", "progress", "ticket"
    }
    assert set(command_suite._OWNER_SETTINGS) == {
        "diagnostics", "tools_enabled", "autonomy_enabled",
        "message_quota_enabled", "user_quota", "reload",
    }


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
    bot = SimpleNamespace(_is_admin=lambda _uid: True)
    interaction = SimpleNamespace(
        guild=SimpleNamespace(id=10, owner_id=1),
        guild_id=10,
        user=SimpleNamespace(id=2),
        member=SimpleNamespace(
            guild_permissions=SimpleNamespace(manage_guild=False, administrator=False)
        ),
        permissions=SimpleNamespace(manage_guild=False, administrator=False),
    )
    # A Maxwell operator role cannot substitute for current target-guild permissions.
    assert not command_suite._can_manage_server(bot, interaction)
    interaction.member.guild_permissions.manage_guild = True
    assert command_suite._can_manage_server(bot, interaction)


def test_config_opens_a_private_settings_menu(tmp_path):
    store = UserPreferenceStore(tmp_path / "user_preferences.json")
    responses = []

    class Response:
        def is_done(self):
            return False

        async def send_message(self, text, *, view=None, ephemeral=False, **kwargs):
            responses.append((kwargs["embed"].title + "\n" + kwargs["embed"].description, view, ephemeral))

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
    panel.selected_key = "mode"

    class ComponentResponse:
        async def edit_message(self, content, *, view, **kwargs):
            responses.append((kwargs["embed"].description, view))

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

        async def send_message(self, text, *, ephemeral=False, **kwargs):
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
    assert panel.scope == "server"
    assert denied and denied[0][1] is True


def test_server_plugin_settings_are_scoped_and_require_manage_permission(tmp_path):
    store = UserPreferenceStore(tmp_path / "user_preferences.json")
    manager = SimpleNamespace(
        loaded_plugins={"web": {}, "shell": {}},
        is_protected=lambda name: name == "core",
        is_plugin_enabled_for_user=lambda name, _uid: name == "web",
    )
    owner = SimpleNamespace(
        guild=SimpleNamespace(id=10, owner_id=2, name="Test server"),
        guild_id=10,
        user=SimpleNamespace(id=2),
        member=SimpleNamespace(
            guild_permissions=SimpleNamespace(manage_guild=False, administrator=False)
        ),
    )
    bot = SimpleNamespace(
        _user_preferences=store,
        _control={},
        plugin_manager=manager,
        config=SimpleNamespace(DATA_DIR=str(tmp_path)),
    )
    panel = command_suite._ConfigPanel(bot, store, owner)
    panel.scope = "server"
    panel.selected_key = "plugins"
    panel._build()
    selector = next(
        item for item in panel.children
        if isinstance(item, command_suite._GuildPluginSelect)
    )
    assert {option.value for option in selector.options} == {"web", "shell"}
    assert {option.value for option in selector.options if option.default} == {"web"}

    edited = []

    class Response:
        async def edit_message(self, content, *, view, **kwargs):
            edited.append(kwargs["embed"].description)

    selector._values = ["shell"]
    click = SimpleNamespace(
        guild=owner.guild,
        guild_id=10,
        user=owner.user,
        member=owner.member,
        response=Response(),
    )
    asyncio.run(selector.callback(click))
    assert bot._control["guild_plugin_overrides"]["10"] == {
        "shell": True,
        "web": False,
    }
    assert edited and "shell" in edited[0]

    # A menu is not reusable by someone else, even if they have server access.
    denied = []

    class DeniedResponse:
        def is_done(self):
            return False

        async def send_message(self, text, *, ephemeral=False, **kwargs):
            denied.append((text, ephemeral))

    click.user = SimpleNamespace(id=3)
    click.response = DeniedResponse()
    assert asyncio.run(panel.interaction_check(click)) is False
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

        async def send_message(self, text, *, ephemeral=False, **kwargs):
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


def test_guild_owner_does_not_receive_application_owner_scope(tmp_path):
    store = UserPreferenceStore(tmp_path / "user_preferences.json")
    bot = SimpleNamespace(
        config=SimpleNamespace(MAXWELL_OWNER_IDS={"99"}),
        _user_preferences=store,
    )
    interaction = SimpleNamespace(
        guild=SimpleNamespace(id=10, owner_id=1, name="Test server"),
        guild_id=10,
        user=SimpleNamespace(id=1),
        member=SimpleNamespace(guild_permissions=SimpleNamespace(manage_guild=True)),
    )
    panel = command_suite._ConfigPanel(bot, store, interaction)
    assert panel.can_manage_server
    assert not panel.is_owner
    assert "Application owner" not in {option.label for option in panel.children[0].options}


def test_config_owner_scope_is_bound_to_configured_application_owner(tmp_path):
    store = UserPreferenceStore(tmp_path / "user_preferences.json")
    bot = SimpleNamespace(
        config=SimpleNamespace(MAXWELL_OWNER_IDS={"99"}),
        _user_preferences=store,
    )
    owner = SimpleNamespace(
        guild=None, guild_id=None, user=SimpleNamespace(id=99)
    )
    panel = command_suite._ConfigPanel(bot, store, owner)
    assert panel.is_owner
    assert isinstance(panel.children[0], command_suite._ConfigScopeSelect)
    panel.scope = "owner"
    panel.selected_key = "diagnostics"
    assert "Application owner settings" in panel.render()


def test_owner_diagnostics_sends_embed_and_export(tmp_path):
    store = UserPreferenceStore(tmp_path / "user_preferences.json")
    bot = SimpleNamespace(
        config=SimpleNamespace(MAXWELL_OWNER_IDS={"99"}),
        _control={"tools_enabled": True},
        _user_preferences=store,
        guilds=[],
        tools={},
        latency=0.01,
        user=SimpleNamespace(id=1, name="Maxwell", display_name="Maxwell"),
        provider=SimpleNamespace(model="test"),
        plugin_manager=None,
    )
    interaction = SimpleNamespace(user=SimpleNamespace(id=99), guild=None, guild_id=None)
    panel = command_suite._ConfigPanel(bot, store, interaction)
    panel.scope = "owner"
    panel.selected_key = "diagnostics"
    panel._build()
    select = next(item for item in panel.children if isinstance(item, command_suite._OwnerDiagnosticsSelect))
    sent = []

    class Response:
        def is_done(self):
            return False

        async def send_message(self, **payload):
            sent.append(payload)

    interaction.response = Response()

    async def choose(section: str) -> None:
        select._values = [section]
        await select.callback(interaction)

    asyncio.run(choose("runtime"))
    assert sent[-1]["embed"].title == "Maxwell Diagnostics"
    assert "file" not in sent[-1]

    asyncio.run(choose("controls"))
    assert sent[-1]["embed"].title == "Maxwell Diagnostics"
    assert sent[-1]["file"].filename == "maxwell-controls.json"

    asyncio.run(choose("data"))
    assert sent[-1]["content"] == "Redacted Maxwell diagnostics export."
    assert sent[-1]["file"].filename == "maxwell-diagnostics.json"


def test_owner_quota_modal_labels_fit_discord_limits():
    modal = command_suite._OwnerQuotaModal(SimpleNamespace())
    for child in modal.children:
        assert len(child.label) <= 45
        placeholder = getattr(child, "placeholder", None)
        if placeholder:
            assert len(placeholder) <= 100


def test_owner_global_toggle_is_fixed_and_persisted(tmp_path):
    store = UserPreferenceStore(tmp_path / "user_preferences.json")
    bot = SimpleNamespace(
        config=SimpleNamespace(MAXWELL_OWNER_IDS={"99"}, DATA_DIR=str(tmp_path)),
        _control={},
        _user_preferences=store,
        _load_control=lambda force=False: None,
    )
    interaction = SimpleNamespace(user=SimpleNamespace(id=99))
    panel = command_suite._ConfigPanel(bot, store, interaction)
    panel.scope = "owner"
    panel.selected_key = "tools_enabled"
    results = []

    class Response:
        async def edit_message(self, content, *, view, **kwargs):
            results.append(kwargs["embed"].description)

    interaction.response = Response()
    asyncio.run(panel.set_choice(interaction, "off"))
    assert bot._control["tools_enabled"] is False
    assert "Off" in results[0]


def test_owner_scope_denies_a_guild_admin_and_revoked_owner(tmp_path):
    store = UserPreferenceStore(tmp_path / "user_preferences.json")
    config = SimpleNamespace(MAXWELL_OWNER_IDS={"99"})
    bot = SimpleNamespace(config=config, _user_preferences=store)
    guild_admin = SimpleNamespace(
        guild=SimpleNamespace(id=10, owner_id=2), guild_id=10,
        user=SimpleNamespace(id=2),
        member=SimpleNamespace(guild_permissions=SimpleNamespace(manage_guild=True)),
    )
    panel = command_suite._ConfigPanel(bot, store, guild_admin)
    assert not panel.is_owner
    panel.scope = "owner"
    assert asyncio.run(panel.authorized(guild_admin)) is False

    owner = SimpleNamespace(user=SimpleNamespace(id=99), guild=None, guild_id=None)
    owner_panel = command_suite._ConfigPanel(bot, store, owner)
    owner_panel.scope = "owner"
    config.MAXWELL_OWNER_IDS = set()
    assert asyncio.run(owner_panel.authorized(owner)) is False


def test_cancel_only_cancels_requester_task_in_matching_context():
    async def scenario():
        owner_task = asyncio.create_task(asyncio.sleep(60))
        other_task = asyncio.create_task(asyncio.sleep(60))
        responses = []

        class Response:
            def is_done(self):
                return False

            async def send_message(self, text, *, ephemeral=False, **kwargs):
                responses.append((text, ephemeral))

        bot = SimpleNamespace(
            _active_requests={"55": other_task},
            _active_request_user={"55": "2"},
        )
        interaction = SimpleNamespace(
            data={"name": "cancel"},
            user=SimpleNamespace(id=1),
            channel_id=55,
            channel=SimpleNamespace(id=55),
            guild=None,
            response=Response(),
        )
        assert await command_suite._handle_cancel(bot, interaction)
        assert not other_task.cancelled()
        assert "no running" in responses[0][0]

        private_key = command_suite.ui.private_channel_key(interaction)
        bot._active_requests[private_key] = owner_task
        bot._active_request_user[private_key] = "1"
        await command_suite._handle_cancel(bot, interaction)
        assert owner_task.cancelling()
        owner_task.cancel()
        other_task.cancel()

    asyncio.run(scenario())


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


def test_visibility_menu_marks_saved_choice_and_reset_restores_public(tmp_path):
    store = UserPreferenceStore(tmp_path / "prefs.json")
    interaction = SimpleNamespace(user=SimpleNamespace(id=100))
    panel = command_suite._ConfigPanel(SimpleNamespace(), store, interaction)
    assert panel.selected_key == "overview"
    panel.selected_key = "visibility"
    panel._build()
    edits = []

    class Response:
        async def edit_message(self, **payload):
            edits.append(payload)

    interaction.response = Response()
    asyncio.run(panel.set_choice(interaction, "private"))
    menu = next(item for item in panel.children if isinstance(item, command_suite._ConfigValueSelect))
    assert [option.value for option in menu.options if option.default] == ["private"]
    asyncio.run(panel.reset(interaction))
    assert store.get("100")["defaults"]["visibility"] == "public"


def test_config_close_removes_controls(tmp_path):
    store = UserPreferenceStore(tmp_path / "prefs.json")
    interaction = SimpleNamespace(user=SimpleNamespace(id=100))
    panel = command_suite._ConfigPanel(SimpleNamespace(), store, interaction)
    edits = []

    class Response:
        async def edit_message(self, **payload):
            edits.append(payload)

    interaction.response = Response()
    close = next(item for item in panel.children if isinstance(item, command_suite._ConfigCloseButton))
    asyncio.run(close.callback(interaction))
    assert edits[-1]["view"] is None


def test_config_home_offers_direct_controls_and_compact_embed(tmp_path):
    store = UserPreferenceStore(tmp_path / "prefs.json")
    store.set_personality("100", "Be brief and friendly.")
    store.set_default("100", "language", "Spanish")
    interaction = SimpleNamespace(user=SimpleNamespace(id=100))
    panel = command_suite._ConfigPanel(SimpleNamespace(), store, interaction)
    assert {child.label for child in panel.children if isinstance(child, command_suite._ConfigShortcutButton)} == {
        "Personality", "Language", "Reply visibility"
    }
    advanced = next(child for child in panel.children if isinstance(child, command_suite._ConfigSettingSelect))
    assert advanced.placeholder == "More options"
    assert {option.value for option in advanced.options} == {"mode", "web", "detail", "context", "byok"}
    assert panel.embed().title == "Your personal settings"
    assert "Be brief and friendly." in panel.embed().description
    assert "Spanish" in panel.embed().description
    assert len(panel.embed().description) < 700


def test_personality_shortcut_saves_and_refreshes_without_breaking_cancel(tmp_path):
    store = UserPreferenceStore(tmp_path / "prefs.json")
    sent, edits, modals = [], [], []

    class Response:
        async def send_modal(self, modal):
            modals.append(modal)

        async def send_message(self, text, **kwargs):
            sent.append((text, kwargs))

    async def edit_original_response(**kwargs):
        edits.append(kwargs)

    interaction = SimpleNamespace(
        user=SimpleNamespace(id=100), response=Response(),
        edit_original_response=edit_original_response,
    )
    panel = command_suite._ConfigPanel(SimpleNamespace(), store, interaction)
    shortcut = next(child for child in panel.children if isinstance(child, command_suite._ConfigShortcutButton) and child.key == "style")
    original_children = list(panel.children)
    asyncio.run(shortcut.callback(interaction))
    # Cancelling the modal leaves the displayed home buttons functional.
    assert panel.selected_key == "overview"
    assert panel.children == original_children
    modal = modals[0]
    modal.field._value = "Be cheerful and concise."
    asyncio.run(modal.on_submit(interaction))
    assert store.get("100")["personality"] == "Be cheerful and concise."
    assert panel.selected_key == "style"
    assert sent[-1][1]["ephemeral"]
    assert "Be cheerful and concise." in edits[-1]["embed"].description
    assert edits[-1]["content"] is None


def test_text_modal_rechecks_identity_and_context_before_saving(tmp_path):
    store = UserPreferenceStore(tmp_path / "prefs.json")
    opened = SimpleNamespace(user=SimpleNamespace(id=100), guild_id=10)
    panel = command_suite._ConfigPanel(SimpleNamespace(), store, opened)
    modal = panel.edit_modal(key="style")
    modal.field._value = "Changed by someone else"
    denied = []

    class Response:
        def is_done(self):
            return False

        async def send_message(self, text, **kwargs):
            denied.append(text)

    for user_id, guild_id in ((200, 10), (100, 20)):
        click = SimpleNamespace(user=SimpleNamespace(id=user_id), guild_id=guild_id, response=Response())
        asyncio.run(modal.on_submit(click))
        assert store.get("100")["personality"] == ""
    assert len(denied) == 2
