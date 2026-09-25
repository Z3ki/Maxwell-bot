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
    options = {row["name"]: row for row in slash["options"]}
    assert {"prompt", "mode", "web", "detail", "language", "context", "image", "file"} <= set(options)
    assert {c["value"] for c in options["mode"]["choices"]} >= {"research", "translate", "code"}
    assert {c["value"] for c in options["context"]["choices"]} == {0, 10, 25, 50}


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
    assert "Use web_search" in text
    assert "thorough" in text
    assert "Respond in Spanish" in text
    assert text.endswith("compare the new releases")


def test_slash_prompt_stays_clean_with_defaults():
    assert mod._slash_prompt("hello", {}) == "hello"


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
    assert "mode=code" in turn["note"]


def test_fact_check_context_action_targets_selected_message(monkeypatch):
    target = SimpleNamespace(id=88, mentions=[SimpleNamespace(id=9)])
    monkeypatch.setattr(mod.ui, "parse_target_message", lambda _interaction: target)
    interaction = _interaction(name=mod.MESSAGE_FACT_CHECK, cmd_type=3)

    turn = mod._enhanced_build_turn(interaction, lambda _interaction: None)
    assert turn is not None
    assert "Fact-check" in turn["prompt"]
    assert "web search" in turn["prompt"].lower()
    assert turn["reference"].resolved is target


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
