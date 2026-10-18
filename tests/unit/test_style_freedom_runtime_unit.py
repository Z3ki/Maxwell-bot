"""Regression: the style-freedom addendum reaches the runtime persona once."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from bot import MaxwellBot
from control_defaults import DEFAULT_CONTROL
from plugins.maxwell_extras.style_freedom import (
    STYLE_FREEDOM_ADDENDUM,
    install_style_freedom,
)

MARKER = "style & freedom:"


@pytest.fixture
def bot(monkeypatch):
    for name in ("BOT_BIRTHDAY", "MAXWELL_PUBLIC_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    instance = object.__new__(MaxwellBot)
    instance.config = SimpleNamespace(BOT_NAME="Maxwell")
    instance._identity = None
    instance.prompts = None
    return instance


def test_runtime_personality_carries_the_addendum_exactly_once(bot):
    out = MaxwellBot._get_personality(bot)
    assert out.count(MARKER) == 1
    assert out.endswith(STYLE_FREEDOM_ADDENDUM)
    assert out.startswith(DEFAULT_CONTROL["base_personality"].strip())


def test_addendum_follows_the_policy_block(bot):
    out = MaxwellBot._get_personality(bot)
    assert out.index("## This deployment's policies") < out.index(MARKER)


def test_legacy_wrapper_does_not_duplicate_the_addendum(bot):
    install_style_freedom(bot)
    assert bot._get_personality().count(MARKER) == 1


def test_system_prompt_personality_line_has_the_addendum_once(bot):
    # conversation.py embeds _get_personality() verbatim into the system
    # prompt; repeated calls must not accumulate copies.
    first = MaxwellBot._get_personality(bot)
    second = MaxwellBot._get_personality(bot)
    assert first == second
    assert f"Core personality (style only): {first}".count(MARKER) == 1
