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


def test_config_personal_defaults_are_available_to_every_user(tmp_path):
    store = UserPreferenceStore(tmp_path / "user_preferences.json")
    responses = []

    class Response:
        def is_done(self):
            return False

        async def send_message(self, text, *, ephemeral=False):
            responses.append((text, ephemeral))

    bot = SimpleNamespace(_user_preferences=store)
    interaction = SimpleNamespace(
        data={"name": "config", "options": [{"name": "action", "value": "set"}, {"name": "scope", "value": "personal"}, {"name": "key", "value": "mode"}, {"name": "value", "value": "research"}]},
        user=SimpleNamespace(id=100),
        response=Response(),
    )

    assert asyncio.run(command_suite._handle_config(bot, interaction))
    assert store.get("100")["defaults"]["mode"] == "research"
    assert responses and responses[0][1] is True
