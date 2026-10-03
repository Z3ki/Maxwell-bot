from __future__ import annotations

import asyncio
from types import SimpleNamespace

from plugins.maxwell_extras import user_install_features as mod


def _interaction(*, options=None, name="maxwell", cmd_type=1, resolved=None):
    return SimpleNamespace(
        id=123,
        type=2,
        channel_id=55,
        channel=SimpleNamespace(name="general", id=55),
        guild=None,
        user=SimpleNamespace(id=1, name="admin", display_name="Admin", bot=False),
        data={
            "name": name,
            "type": cmd_type,
            "options": list(options or []),
            "resolved": dict(resolved or {}),
        },
    )


def test_modern_command_surface_has_modes_context_and_message_actions():
    commands = mod.modern_user_install_commands()
    kinds = {(row["name"], row["type"]) for row in commands}
    assert ("maxwell", 1) in kinds
    assert ("Ask Maxwell", 3) in kinds
    assert ("Summarize", 3) in kinds
    assert (mod.MESSAGE_EXPLAIN, 3) in kinds
    assert (mod.MESSAGE_FACT_CHECK, 3) in kinds
    assert ("Ask Maxwell", 2) in kinds

    slash = next(row for row in commands if row["name"] == "maxwell" and row["type"] == 1)
    assert slash["integration_types"] == [0, 1]
    assert slash["contexts"] == [0, 1, 2]
    options = {row["name"]: row for row in slash["options"]}
    assert {"prompt", "mode", "web", "detail", "language", "context", "visibility", "image", "file"} <= set(options)
    assert {c["value"] for c in options["mode"]["choices"]} >= {"research", "translate", "code"}
    assert {c["value"] for c in options["context"]["choices"]} == {0, 10, 25, 50, 100, 250, 500, 1000}
    assert {c["value"] for c in options["visibility"]["choices"]} == {"private", "public"}


def test_slash_prompt_applies_research_web_detail_and_language():
    text = mod._slash_prompt(
        "compare the new releases",
        {
            "mode": "research",
            "web": "search",
            "detail": "deep",
            "language": "Spanish",
        },
    )
    assert "Research this request" in text
    assert "Search the web before answering" in text
    assert "thorough" in text
    assert "Respond in Spanish" in text
    assert text.endswith("compare the new releases")


def test_slash_prompt_auto_mode_does_not_force_search():
    text = mod._slash_prompt("hello", {})
    assert "Do not search unless" in text
    assert "current/latest" not in text
    assert "web_search" not in text
    assert text.endswith("User request:\nhello")


def test_context_limit_is_user_selectable_and_sanitized():
    good = _interaction(options=[{"name": "context", "value": 50}])
    assert mod._context_limit(good) == 50
    bad = _interaction(options=[{"name": "context", "value": 999}])
    assert mod._context_limit(bad) == 25


def test_enhanced_slash_turn_keeps_core_attachments():
    interaction = _interaction(
        options=[
            {"name": "prompt", "value": "debug it"},
            {"name": "mode", "value": "code"},
            {"name": "detail", "value": "quick"},
            {"name": "context", "value": 10},
        ]
    )

    def original(_interaction):
        return {
            "prompt": "debug it",
            "attachments": [SimpleNamespace(filename="log.txt")],
            "mentions": [],
            "reference": None,
            "note": "base",
            "command": "maxwell",
        }

    turn = mod._enhanced_build_turn(interaction, original)
    assert turn is not None
    assert "coding/technical task" in turn["prompt"]
    assert "concise" in turn["prompt"]
    assert turn["attachments"][0].filename == "log.txt"
    assert turn["history_limit"] == 10
    assert turn["search_query"] == "debug it"
    assert turn["web"] == "auto"
    assert turn["mode"] == "code"
    assert turn["visibility"] == "public"
    assert "mode=code" in turn["note"]


def test_slash_visibility_override_is_validated():
    interaction = _interaction(options=[
        {"name": "prompt", "value": "hello"},
        {"name": "visibility", "value": "public"},
    ])
    turn = mod._enhanced_build_turn(
        interaction,
        lambda _interaction: {"prompt": "hello", "attachments": [], "note": ""},
    )
    assert turn["visibility"] == "public"

    interaction.data["options"][1]["value"] = "channel-is-secret"
    turn = mod._enhanced_build_turn(
        interaction,
        lambda _interaction: {"prompt": "hello", "attachments": [], "note": ""},
    )
    assert turn["visibility"] == "private"


def test_private_context_keys_separate_users_guilds_and_channels():
    one = _interaction()
    one.guild = SimpleNamespace(id=10)
    other_channel = _interaction()
    other_channel.guild = SimpleNamespace(id=10)
    other_channel.channel_id = 56
    other_channel.channel.id = 56
    other_guild = _interaction()
    other_guild.guild = SimpleNamespace(id=11)
    assert mod.ui.private_channel_key(one) != mod.ui.private_channel_key(other_channel)
    assert mod.ui.private_channel_key(one) != mod.ui.private_channel_key(other_guild)


def test_fact_check_context_action_targets_selected_message(monkeypatch):
    target = SimpleNamespace(
        id=88, mentions=[SimpleNamespace(id=9)], content="current claim"
    )
    monkeypatch.setattr(mod.ui, "parse_target_message", lambda _interaction: target)
    interaction = _interaction(name=mod.MESSAGE_FACT_CHECK, cmd_type=3)

    turn = mod._enhanced_build_turn(interaction, lambda _interaction: None)
    assert turn is not None
    assert "Fact-check" in turn["prompt"]
    assert "web search" in turn["prompt"].lower()
    assert turn["reference"].resolved is target
    assert turn["search_query"] == "current claim"
    assert turn["mode"] == "research"
    assert turn["web"] == "search"


def test_modern_session_send_preserves_embed_view_and_multiple_files():
    sent_payloads = []

    class Followup:
        async def send(self, **payload):
            sent_payloads.append(payload)
            return SimpleNamespace(id=1, content=payload.get("content"), edit=lambda **_kwargs: None)

    class Session:
        def __init__(self):
            self.interaction = SimpleNamespace(followup=Followup())
            self._sent = 0
            self._last = None

        async def ensure_deferred(self):
            return None

        async def _edit_last(self, text, file=None):
            raise AssertionError("cap path should not run")

    async def run():
        session = Session()
        result = await mod._modern_session_send(
            session,
            "hello",
            files=["a", "b"],
            embed="embed",
            view="view",
            silent=True,
        )
        assert result.id == 1

    asyncio.run(run())
    payload = sent_payloads[0]
    assert payload["content"] == "hello"
    assert payload["files"] == ["a", "b"]
    assert payload["embed"] == "embed"
    assert payload["view"] == "view"
    assert payload["silent"] is True
    assert payload["allowed_mentions"].everyone is False
    assert payload["allowed_mentions"].users is False
    assert payload["allowed_mentions"].roles is False


def test_saved_visibility_and_context_apply_but_explicit_options_win(tmp_path, monkeypatch):
    from plugins.maxwell_extras.user_preferences import UserPreferenceStore
    store = UserPreferenceStore(tmp_path / "prefs.json")
    monkeypatch.setattr(mod, "_USER_PREFERENCE_STORE", store)
    store.set_default("1", "visibility", "private")
    store.set_default("1", "context", 0)
    interaction = _interaction(options=[{"name": "prompt", "value": "hello"}])
    original = lambda _: {"prompt": "hello", "attachments": [], "note": ""}
    turn = mod._enhanced_build_turn(interaction, original)
    assert turn["visibility"] == "private"
    assert turn["history_limit"] == 0
    assert mod._context_limit(interaction) == 0
    # Another user gets public defaults, never the first user's saved choices.
    interaction.user.id = 2
    assert mod._enhanced_build_turn(interaction, original)["visibility"] == "public"
    interaction.user.id = 1
    interaction.data["options"] += [
        {"name": "visibility", "value": "public"},
        {"name": "context", "value": 10},
    ]
    turn = mod._enhanced_build_turn(interaction, original)
    assert turn["visibility"] == "public"
    assert turn["history_limit"] == 10
    assert mod._context_limit(interaction) == 10


def test_saved_zero_context_skips_live_channel_read(tmp_path, monkeypatch):
    from plugins.maxwell_extras.user_preferences import UserPreferenceStore
    store = UserPreferenceStore(tmp_path / "prefs.json")
    store.set_default("1", "context", 0)
    monkeypatch.setattr(mod, "_USER_PREFERENCE_STORE", store)
    interaction = _interaction()

    def history(**kwargs):
        raise AssertionError("Channel history must not be fetched with context disabled")

    interaction.channel.history = history
    assert asyncio.run(mod._snapshot_channel_history(None, interaction)) == []


def test_large_saved_context_fetches_all_selected_messages(tmp_path, monkeypatch):
    from plugins.maxwell_extras.user_preferences import UserPreferenceStore

    store = UserPreferenceStore(tmp_path / "prefs.json")
    store.set_default(1, "context", 1000)
    monkeypatch.setattr(mod, "_USER_PREFERENCE_STORE", store)
    interaction = _interaction()
    requested = []

    async def history(*, limit):
        requested.append(limit)
        for index in range(limit, 0, -1):
            yield SimpleNamespace(id=index, content=f"message {index}",
                                  author=SimpleNamespace(id=7, display_name="Alice", bot=False),
                                  created_at=None)

    interaction.channel.history = history
    rows = asyncio.run(mod._snapshot_channel_history(None, interaction))
    assert requested == [1000]
    assert len(rows) == 1000
    assert rows[0]["content"] == "message 1"
    assert rows[-1]["content"] == "message 1000"
    # A one-request override still takes priority over the large saved default.
    interaction.data["options"] = [{"name": "context", "value": 100}]
    assert len(asyncio.run(mod._snapshot_channel_history(None, interaction))) == 100
    assert requested[-1] == 100
