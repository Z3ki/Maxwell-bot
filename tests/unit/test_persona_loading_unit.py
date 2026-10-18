"""Persona loading: _get_personality() and the style-freedom addendum."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from bot import MaxwellBot
from control_defaults import DEFAULT_CONTROL
from maxwell_core.prompts.component import PromptComponent
from maxwell_core.prompts.manager import PromptManager
from plugins.maxwell_extras import style_freedom
from plugins.maxwell_extras.style_freedom import (
    STYLE_FREEDOM_ADDENDUM,
    append_style_freedom,
    install_style_freedom,
)

MARKER = "style & freedom:"
REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _no_operator_env(monkeypatch):
    for name in (
        "BOT_BIRTHDAY",
        "MAXWELL_PUBLIC_BASE_URL",
        "MAXWELL_TERMS_URL",
        "MAXWELL_PRIVACY_URL",
        "MAXWELL_CONTACT_URL",
        "MAXWELL_OWNER_IDS",
    ):
        monkeypatch.delenv(name, raising=False)


def _bot(**config):
    bot = object.__new__(MaxwellBot)
    defaults = {
        "BOT_NAME": "Maxwell",
        "MAXWELL_PUBLIC_BASE_URL": "",
        "MAXWELL_TERMS_URL": "",
        "MAXWELL_PRIVACY_URL": "",
        "MAXWELL_CONTACT_URL": "",
    }
    defaults.update(config)
    bot.config = SimpleNamespace(**defaults)
    bot._identity = None
    bot.prompts = None
    return bot


# --------------------------------------------------------------------------
# append_style_freedom
# --------------------------------------------------------------------------


@pytest.mark.parametrize("empty", [None, "", "   ", "\n\n"])
def test_append_to_empty_personality_is_just_the_addendum(empty):
    assert append_style_freedom(empty) == STYLE_FREEDOM_ADDENDUM


def test_append_separates_with_one_blank_line_and_strips_trailing_space():
    out = append_style_freedom("be direct.   \n\n")
    assert out == f"be direct.\n\n{STYLE_FREEDOM_ADDENDUM}"


def test_append_is_idempotent_over_many_rounds():
    out = "base"
    for _ in range(5):
        out = append_style_freedom(out)
    assert out.count(MARKER) == 1


def test_append_detects_marker_case_insensitively():
    text = "base\n\nSTYLE & FREEDOM:\n- already here"
    assert append_style_freedom(text) == text


def test_append_coerces_non_string_input():
    out = append_style_freedom(12345)
    assert out.startswith("12345\n\n")
    assert out.count(MARKER) == 1


def test_addendum_starts_with_its_own_marker():
    # The idempotence check relies on the addendum carrying the marker.
    assert STYLE_FREEDOM_ADDENDUM.lower().startswith(style_freedom._MARKER)


# --------------------------------------------------------------------------
# install_style_freedom
# --------------------------------------------------------------------------


def test_install_wraps_an_instance_and_passes_arguments_through():
    seen = []

    class Bot:
        def _get_personality(self, *args, **kwargs):
            seen.append((args, kwargs))
            return "persona"

    bot = Bot()
    assert install_style_freedom(bot) is True
    out = bot._get_personality(1, k="v")
    assert seen == [((1,), {"k": "v"})]
    assert out == f"persona\n\n{STYLE_FREEDOM_ADDENDUM}"
    assert bot._maxwell_style_freedom_installed is True


def test_install_only_patches_that_instance():
    class Bot:
        def _get_personality(self):
            return "persona"

    patched, untouched = Bot(), Bot()
    install_style_freedom(patched)
    assert MARKER in patched._get_personality()
    assert MARKER not in untouched._get_personality()


def test_install_does_not_double_append_when_base_already_has_it():
    class Bot:
        def _get_personality(self):
            return append_style_freedom("persona")

    bot = Bot()
    install_style_freedom(bot)
    assert bot._get_personality().count(MARKER) == 1


def test_install_rejects_non_callable_personality():
    assert install_style_freedom(SimpleNamespace(_get_personality="text")) is False


# --------------------------------------------------------------------------
# MaxwellBot._get_personality
# --------------------------------------------------------------------------


def test_personality_starts_with_code_owned_base():
    out = MaxwellBot._get_personality(_bot())
    assert out.startswith(DEFAULT_CONTROL["base_personality"].strip())


def test_personality_ignores_a_persisted_control_override():
    bot = _bot()
    bot._control = {"base_personality": "you are a pirate"}
    out = MaxwellBot._get_personality(bot)
    assert "pirate" not in out


def test_personality_without_policy_urls_tells_model_to_ask_operator():
    out = MaxwellBot._get_personality(_bot())
    assert "## This deployment's policies" in out
    assert "has not configured policy URLs" in out


def test_personality_lists_configured_https_policy_urls_only():
    bot = _bot(
        MAXWELL_TERMS_URL="https://example.test/terms/",
        MAXWELL_PRIVACY_URL="http://insecure.example.test/privacy/",
        MAXWELL_CONTACT_URL="https://user:pw@example.test/contact/",
    )
    out = MaxwellBot._get_personality(bot)
    assert "Terms: https://example.test/terms/" in out
    assert "insecure.example.test" not in out
    assert "user:pw" not in out


def test_personality_derives_policy_urls_from_public_base():
    out = MaxwellBot._get_personality(
        _bot(MAXWELL_PUBLIC_BASE_URL="https://bot.example.test/")
    )
    assert "Privacy: https://bot.example.test/privacy/" in out
    assert "Contact: https://bot.example.test/contact/" in out


def test_personality_has_no_unfilled_identity_tokens():
    from identity import IDENTITY_TOKENS

    out = MaxwellBot._get_personality(_bot(BOT_NAME="Testwell"))
    for token in IDENTITY_TOKENS:
        assert "{" + token + "}" not in out


def test_personality_birthday_line_only_when_env_is_set(monkeypatch):
    bot = _bot(BOT_BIRTHDAY="2026-01-01")
    assert "days old" not in MaxwellBot._get_personality(bot)

    monkeypatch.setenv("BOT_BIRTHDAY", "2026-01-01")
    out = MaxwellBot._get_personality(bot)
    age = (datetime.now(timezone.utc) - datetime(2026, 1, 1, tzinfo=timezone.utc)).days
    assert f"You are currently {age} days old." in out
    assert "January 1, 2026" in out
    assert out.count("You are currently") == 1


def test_personality_includes_core_style_components_but_not_plugin_ones():
    bot = _bot()
    bot.prompts = PromptManager()
    bot.prompts.register(
        PromptComponent(
            id="core.style", plugin="core", text="CORE STYLE", position="style"
        )
    )
    bot.prompts.register(
        PromptComponent(
            id="music.style", plugin="music", text="PLUGIN STYLE", position="style"
        )
    )
    out = MaxwellBot._get_personality(bot)
    assert "CORE STYLE" in out
    assert "PLUGIN STYLE" not in out
    assert out.index(DEFAULT_CONTROL["base_personality"].strip()) < out.index(
        "CORE STYLE"
    )


def test_personality_never_carries_the_style_addendum_twice():
    bot = _bot()
    install_style_freedom(bot)
    install_style_freedom(bot)
    assert bot._get_personality().count(MARKER) == 1


def test_installed_style_addendum_lands_after_the_policy_block():
    bot = _bot()
    install_style_freedom(bot)
    out = bot._get_personality()
    assert out.index("## This deployment's policies") < out.index(MARKER)
    assert out.endswith(STYLE_FREEDOM_ADDENDUM)


def test_persona_doc_exists_and_is_not_the_runtime_base():
    doc = (REPO / "docs" / "MAXWELL_PERSONA.md").read_text(encoding="utf-8")
    assert doc.startswith("# Maxwell Discord persona")
    # The doc complements the code-owned base; it must not be pasted into it.
    assert "## System prompt" not in DEFAULT_CONTROL["base_personality"]
