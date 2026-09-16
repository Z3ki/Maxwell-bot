from __future__ import annotations

from types import SimpleNamespace

from plugins.maxwell_extras.style_freedom import (
    STYLE_FREEDOM_ADDENDUM,
    append_style_freedom,
    install_style_freedom,
)


def test_append_style_freedom_adds_expected_language_once():
    out = append_style_freedom("you're Maxwell. be direct.")
    assert "conversational freedom" in out
    assert "you can swear" in out
    assert "profanity, slang, sarcasm" in out
    assert out.count("style & freedom:") == 1
    assert append_style_freedom(out) == out


def test_install_style_freedom_wraps_persisted_personality():
    class Bot:
        def _get_personality(self):
            return "persisted custom personality"

    bot = Bot()
    assert install_style_freedom(bot)
    first = bot._get_personality()
    assert first.startswith("persisted custom personality")
    assert STYLE_FREEDOM_ADDENDUM in first

    # Re-installing (as can happen during plugin reloads) must not stack copies.
    assert install_style_freedom(bot)
    assert bot._get_personality().count("style & freedom:") == 1


def test_install_style_freedom_requires_personality_provider():
    bot = SimpleNamespace()
    assert not install_style_freedom(bot)
