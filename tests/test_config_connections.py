"""Exercise the rebuilt settings journeys with real encrypted storage."""

from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace

import discord
import pytest

from maxwell_core.providers.errors import (
    ProviderAuthenticationError,
    ProviderInvalidRequestError,
    ProviderRateLimitError,
    ProviderUsageExhaustedError,
)
from plugins.maxwell_extras import command_suite as settings
from plugins.maxwell_extras import config_connections as connections
from plugins.maxwell_extras.byok import CredentialVault, parse_model_support
from plugins.maxwell_extras.user_preferences import UserPreferenceStore

SECRET = "sk-private-test-key-abcdef123456"


class Interaction:
    def __init__(self, user_id=100, guild=None):
        self.user = SimpleNamespace(id=user_id)
        self.guild = guild
        self.guild_id = getattr(guild, "id", None)
        self.events = []
        self.modals = []
        self.edits = []
        self.notices = []
        self.done = False
        self.response = SimpleNamespace(
            is_done=lambda: self.done,
            defer=self.defer,
            send_modal=self.send_modal,
            edit_message=self.edit_original_response,
            send_message=self.send,
        )
        self.followup = SimpleNamespace(send=self.send)

    async def defer(self, **kwargs):
        self.done = True
        self.events.append("ack")

    async def send_modal(self, modal):
        self.modals.append(modal)

    async def send(self, text, **kwargs):
        assert kwargs.get("ephemeral") is True
        self.notices.append(text)

    async def edit_original_response(self, **payload):
        self.edits.append(payload)


@pytest.fixture
def setup(tmp_path):
    store = UserPreferenceStore(tmp_path / "prefs.json")
    vault = CredentialVault(tmp_path / "byok.sqlite3", "11" * 32)
    bot = SimpleNamespace(
        _user_preferences=store,
        _byok_vault=vault,
        config=SimpleNamespace(DATA_DIR=str(tmp_path), MAXWELL_OWNER_IDS={"100"}),
        _control={},
        plugin_manager=None,
    )
    interaction = Interaction()
    panel = settings._ConfigPanel(bot, store, interaction)
    return panel, bot, vault, interaction


def open_page(panel, key, *, scope="personal"):
    panel.scope = scope
    panel.selected_key = key
    panel._build()


def component(panel, cls):
    return next(item for item in panel.children if isinstance(item, cls))


def seed(panel, vault, provider="openai", *, custom=False):
    vault.save(
        "100",
        provider,
        "example-model",
        SECRET,
        base_url="https://example.com/v1" if custom or provider == "custom" else "",
        settings=parse_model_support(
            "text, vision, tools",
            "reasoning=on max_tokens=8192 temperature=0.7 effort=high context=128000",
        ),
    )
    asyncio.run(panel._refresh_connection())
    panel.selected_provider = provider


def test_home_navigation_and_reply_changes_keep_other_preferences(setup):
    panel, bot, _, interaction = setup
    bot._user_preferences.set_personality("100", "Keep it short")
    bot._user_preferences.set_default("100", "language", "Spanish")
    nav = next(
        item
        for item in panel.children
        if isinstance(item, settings._ConfigNavButton) and item.key == "replies"
    )
    asyncio.run(nav.callback(interaction))
    assert panel.selected_key == "replies"
    assert not any(
        isinstance(item, discord.ui.Select) for item in panel.children if item.row == 0
    )
    choice = next(
        item
        for item in panel.children
        if isinstance(item, settings._ConfigValueSelect) and item.key == "visibility"
    )
    choice._values = ["private"]
    asyncio.run(choice.callback(Interaction()))
    row = bot._user_preferences.get("100")
    assert row["defaults"]["visibility"] == "private"
    assert row["defaults"]["language"] == "Spanish"
    assert row["personality"] == "Keep it short"
    assert panel.selected_key == "replies"
    assert "Private" in panel.render()
    assert bot._user_preferences.get("200")["defaults"]["visibility"] == "public"


def test_language_preset_and_automatic_are_saved(setup):
    panel, bot, _, _ = setup
    open_page(panel, "language")
    asyncio.run(panel.set_choice(Interaction(), "Spanish"))
    assert bot._user_preferences.get("100")["defaults"]["language"] == "Spanish"
    asyncio.run(panel.set_choice(Interaction(), "auto"))
    assert not bot._user_preferences.get("100")["defaults"]["language"]
    assert panel.choice_value() == "auto"


def test_custom_language_is_displayed_as_the_saved_selection(setup):
    panel, bot, _, _ = setup
    bot._user_preferences.set_default("100", "language", "Portuguese")
    asyncio.run(panel._refresh_personal())
    open_page(panel, "language")
    selector = component(panel, settings._ConfigValueSelect)
    assert [option.value for option in selector.options if option.default] == [
        "Portuguese"
    ]


def test_channel_picker_explains_missing_bot_permissions_without_saving(setup):
    panel, bot, _, _ = setup
    channel = SimpleNamespace(
        id=1000,
        permissions_for=lambda _: SimpleNamespace(
            view_channel=True, send_messages=False
        ),
    )
    guild = SimpleNamespace(id=55, owner_id=100, text_channels=[channel], me=object())
    click = Interaction(guild=guild)
    panel.guild_id = "55"
    panel.command_interaction = click
    saved = []

    async def save(*args, **kwargs):
        saved.append(args)

    bot._save_solo = save
    open_page(panel, "channels", scope="server")
    selector = component(panel, settings._GuildChannelSelect)
    selector._values = [channel]
    asyncio.run(selector.callback(click))
    assert not saved
    assert "channel permissions" in click.notices[-1]


@pytest.mark.parametrize(
    "provider,count", [("openai", 2), ("openrouter", 2), ("groq", 2), ("custom", 3)]
)
def test_connect_form_is_short_and_cancel_does_not_save(setup, provider, count):
    panel, _, vault, _ = setup
    panel.selected_provider = provider
    open_page(panel, "byok")
    click = Interaction()
    asyncio.run(component(panel, connections.ConnectButton).callback(click))
    modal = click.modals[0]
    assert len(modal.children) == count
    assert modal.api_key.default is None
    assert not vault.has_credential("100")
    assert panel.selected_key == "byok"
    for child in modal.children:
        assert len(child.label) <= 45
        assert len(child.placeholder or "") <= 100


def test_connect_save_and_edit_preserve_key_endpoint_and_advanced_settings(setup):
    panel, _, vault, interaction = setup
    seed(panel, vault, custom=True)
    previous = vault.get("100")
    open_page(panel, "byok")
    modal = connections.ConnectModal(panel)
    modal.api_key._value = ""
    modal.model._value = "new-model"
    asyncio.run(modal.on_submit(Interaction()))
    current = vault.get("100")
    assert current["api_key"] == previous["api_key"]
    assert current["settings"] == previous["settings"]
    assert current["base_url"] == previous["base_url"]
    assert current["model"] == "new-model"
    assert "new-model" in interaction.edits[-1]["embed"].description
    assert SECRET not in json.dumps(panel.to_components()) + panel.render()


def test_switching_provider_requires_its_key_and_never_reuses_another_key(setup):
    panel, _, vault, _ = setup
    seed(panel, vault)
    open_page(panel, "byok")
    selector = component(panel, connections.ProviderSelect)
    selector._values = ["groq"]
    asyncio.run(selector.callback(Interaction()))
    assert vault.get("100")["provider"] == "openai"
    assert "OpenAI" in panel.render() and "replaces" in panel.render()
    modal = connections.ConnectModal(panel)
    modal.model._value = "groq-model"
    modal.api_key._value = ""
    click = Interaction()
    asyncio.run(modal.on_submit(click))
    assert "Paste an API key" in click.notices[-1]
    assert vault.get("100")["provider"] == "openai"
    modal.api_key._value = "new-provider-key-987654"
    asyncio.run(modal.on_submit(Interaction()))
    assert vault.get("100")["provider"] == "groq"
    assert vault.get("100")["base_url"] == ""
    assert vault.get("100")["settings"]["vision"] is False


@pytest.mark.parametrize(
    "kind", ["other_user", "other_guild", "other_page", "other_provider", "closed"]
)
def test_late_or_unauthorized_connect_form_cannot_write(setup, kind):
    panel, _, vault, _ = setup
    open_page(panel, "byok")
    modal = connections.ConnectModal(panel)
    modal.api_key._value = SECRET
    modal.model._value = "example-model"
    click = Interaction(user_id=200 if kind == "other_user" else 100)
    if kind == "other_guild":
        click.guild_id = 55
    if kind == "other_page":
        open_page(panel, "style")
    if kind == "other_provider":
        panel.selected_provider = "groq"
    if kind == "closed":
        panel.stop()
    asyncio.run(modal.on_submit(click))
    assert not vault.has_credential("100")
    assert click.notices


def test_advanced_controls_change_support_and_limits_without_replacing_credentials(
    setup,
):
    panel, _, vault, _ = setup
    seed(panel, vault)
    open_page(panel, "byok_advanced")
    features = component(panel, connections.FeaturesSelect)
    features._values = ["audio", "tools"]
    asyncio.run(features.callback(Interaction()))
    assert vault.get("100")["settings"]["audio"] is True
    assert vault.get("100")["settings"]["vision"] is False
    modal = connections.AdvancedModal(panel)
    for key, value in {
        "max_tokens": "2048",
        "temperature": "0.2",
        "effort": "low",
        "context": "64000",
    }.items():
        modal.fields[key]._value = value
    asyncio.run(modal.on_submit(Interaction()))
    saved = vault.get("100")
    assert saved["settings"]["max_tokens"] == 2048
    assert saved["settings"]["context"] == 64000
    assert saved["settings"]["audio"] is True
    assert saved["api_key"] == SECRET


@pytest.mark.parametrize(
    "value",
    [
        "http://example.com/v1",
        "https://127.0.0.1/v1",
        "https://example.com/v1?key=secret",
    ],
)
def test_advanced_endpoint_validation_preserves_existing_connection(setup, value):
    panel, _, vault, _ = setup
    seed(panel, vault)
    open_page(panel, "byok_advanced")
    before = vault.get("100")
    modal = connections.AdvancedModal(panel, endpoint=True)
    modal.fields["base_url"]._value = value
    click = Interaction()
    asyncio.run(modal.on_submit(click))
    assert vault.get("100") == before
    assert click.notices


def test_removing_connection_requires_confirmation_and_cancel_keeps_key(setup):
    panel, _, vault, _ = setup
    seed(panel, vault)
    open_page(panel, "byok")
    button = next(
        item
        for item in panel.children
        if isinstance(item, settings._ConfigNavButton) and item.key == "byok_remove"
    )
    asyncio.run(button.callback(Interaction()))
    assert vault.has_credential("100")
    assert panel.selected_key == "byok_remove"
    cancel = next(
        item
        for item in panel.children
        if isinstance(item, settings._ConfigNavButton)
        and item.key == "byok"
        and item.row == 2
    )
    asyncio.run(cancel.callback(Interaction()))
    assert vault.has_credential("100")
    open_page(panel, "byok_remove")
    asyncio.run(component(panel, connections.DisconnectButton).callback(Interaction()))
    assert not vault.has_credential("100")
    assert panel.selected_key == "byok"
    assert "Maxwell's default AI" in panel.render()


def test_unreadable_connection_can_be_removed_without_reporting_default_active(setup):
    panel, bot, vault, _ = setup
    seed(panel, vault)
    bot._byok_vault = CredentialVault(vault.path, "22" * 32)
    asyncio.run(panel._refresh_connection())
    open_page(panel, "byok")
    assert panel.connection_present
    assert "saved AI connection is unavailable" in panel.render()
    assert component(panel, connections.ConnectButton).disabled is True
    # Existing unavailable credentials can still be deleted; no decrypt needed.
    open_page(panel, "byok_remove")
    asyncio.run(component(panel, connections.DisconnectButton).callback(Interaction()))
    assert not vault.has_credential("100")


@pytest.mark.parametrize(
    "failure,message",
    [
        (None, "Connection works"),
        (ProviderAuthenticationError("secret upstream body"), "Key rejected"),
        (ProviderUsageExhaustedError("secret upstream body"), "API credit"),
        (ProviderRateLimitError("secret upstream body"), "rate limited"),
        (ProviderInvalidRequestError("secret upstream body"), "model or its settings"),
        (TimeoutError(), "in time"),
        (RuntimeError(SECRET), "Could not reach"),
    ],
)
def test_connection_test_reports_actionable_safe_result_and_closes_client(
    setup, monkeypatch, failure, message
):
    panel, _, vault, _ = setup
    seed(panel, vault)
    open_page(panel, "byok")
    calls = []

    class Provider:
        async def generate_response(self, messages, **kwargs):
            assert messages == [{"role": "user", "content": "Reply with OK."}]
            assert "temperature" not in kwargs
            assert "max_tokens" not in kwargs
            calls.append("generate")
            if failure:
                raise failure
            return "OK"

        async def close(self):
            calls.append("close")

    monkeypatch.setattr(connections, "make_request_provider", lambda *_: Provider())
    click = Interaction()
    asyncio.run(component(panel, connections.TestButton).callback(click))
    assert calls == ["generate", "close"]
    assert message in click.notices[-1]
    assert SECRET not in click.notices[-1]
    assert "secret upstream body" not in panel.render()


def test_connection_opening_acks_before_slow_vault_and_never_blocks_loop(setup):
    panel, bot, vault, _ = setup
    started, release = threading.Event(), threading.Event()
    original = vault.has_credential
    click = Interaction()
    click.data = {"name": "config"}

    def slow(user_id):
        assert click.events == ["ack"]
        started.set()
        assert release.wait(5)
        return original(user_id)

    vault.has_credential = slow

    async def run():
        task = asyncio.create_task(settings._handle_config(bot, click))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            assert not task.done()
        finally:
            release.set()
            await task

    asyncio.run(run())
    assert click.edits


def test_every_screen_serializes_within_discord_component_limits(setup):
    panel, _, vault, interaction = setup
    seed(panel, vault)
    guild = SimpleNamespace(id=55, owner_id=100, name="Test server", text_channels=[])
    panel.command_interaction = Interaction(guild=guild)
    panel.guild_id = "55"
    panel.can_manage_server = True
    scopes = {
        "personal": [
            "overview",
            "replies",
            *settings._PERSONAL_SETTINGS,
            "byok_advanced",
            "byok_remove",
        ],
        "server": ["overview", *settings._SERVER_SETTINGS],
        "owner": ["overview", *settings._OWNER_SETTINGS],
    }
    for scope, keys in scopes.items():
        for key in keys:
            open_page(panel, key, scope=scope)
            rows = panel.to_components()
            assert len(rows) <= 5, (scope, key)
            ids = [
                child.custom_id
                for child in panel.children
                if not getattr(child, "url", None)
            ]
            assert len(set(ids)) == len(ids), (scope, key)
            for row in rows:
                assert len(row["components"]) <= 5
            assert len(panel.embed().description) <= 4096
            assert SECRET not in json.dumps(rows) + panel.render()


def test_server_tools_selection_uses_allowed_semantics_and_preserves_other_server(
    setup,
):
    panel, bot, _, _ = setup
    guild = SimpleNamespace(id=55, owner_id=100, name="Test", text_channels=[])
    interaction = Interaction(guild=guild)
    panel.command_interaction = interaction
    panel.guild_id = "55"
    panel.can_manage_server = True
    bot._control = {"guild_disabled_capabilities": {"66": ["moderation"]}}
    open_page(panel, "capabilities", scope="server")
    selector = component(panel, settings._GuildCapabilitySelect)
    assert all(option.default for option in selector.options)
    enabled = set(settings.GUILD_CAPABILITIES) - {"moderation"}
    selector._values = list(enabled)
    asyncio.run(selector.callback(interaction))
    assert bot._control["guild_disabled_capabilities"] == {
        "55": ["moderation"],
        "66": ["moderation"],
    }


def test_server_channel_picker_can_choose_channel_after_first_25(setup):
    panel, bot, _, _ = setup
    channels = [SimpleNamespace(id=1000 + i, name=f"channel-{i}") for i in range(60)]
    guild = SimpleNamespace(id=55, owner_id=100, name="Test", text_channels=channels)
    interaction = Interaction(guild=guild)
    panel.guild_id = "55"
    panel.command_interaction = interaction
    saved = []

    async def save(mapping, guild_id, **kwargs):
        saved.append((mapping, guild_id, kwargs))
        bot._control["guild_solo_channel"] = mapping

    bot._save_solo = save
    open_page(panel, "channels", scope="server")
    selector = component(panel, settings._GuildChannelSelect)
    selector._values = [channels[-1]]
    asyncio.run(selector.callback(interaction))
    assert saved[-1][0] == {"55": "1059"}
    asyncio.run(
        component(panel, settings._ConfigResetButton).callback(Interaction(guild=guild))
    )
    assert saved[-1][0] == {}


def test_bot_controls_do_not_advertise_retired_message_limits(setup):
    panel, _, _, _ = setup
    open_page(panel, "overview", scope="owner")
    labels = [
        item.label for item in panel.children if isinstance(item, discord.ui.Button)
    ]
    assert not any("allowance" in label.lower() for label in labels)
    assert not {"message_quota_enabled", "user_quota"} & settings._OWNER_SETTINGS.keys()
