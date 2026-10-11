"""Policy questions use this deployment's links without unsolicited DMs."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot import MaxwellBot
from maxwell_core.prompts import PromptManager
from test_dm_replies import _bot as dm_bot, _dm_message
from test_prompt_cache import FakeMemory, _bot as prompt_bot, _message


@pytest.mark.parametrize("base", ["https://operator.example.test", "https://operator.example.test///"])
def test_model_request_has_operator_policy_urls(base):
    bot = prompt_bot(FakeMemory())
    bot.config = SimpleNamespace(MAXWELL_PUBLIC_BASE_URL=base)
    bot.prompts = PromptManager()
    MaxwellBot._install_core_prompt_components(bot)
    bot._get_personality = MaxwellBot._get_personality.__get__(bot)
    messages = asyncio.run(MaxwellBot._build_messages(bot, _message(), "where are your policies?"))
    system = "\n".join(str(m["content"]) for m in messages if m["role"] == "system")
    assert "https://operator.example.test/terms/" in system
    assert "https://operator.example.test/privacy/" in system
    assert "https://maxwell.z3ki.dev/terms/" not in system


def test_unconfigured_operator_does_not_inherit_another_instances_policy_urls():
    configured = prompt_bot(FakeMemory())
    configured.config = SimpleNamespace(MAXWELL_PUBLIC_BASE_URL="https://another.example.test")
    configured.prompts = PromptManager()
    MaxwellBot._install_core_prompt_components(configured)
    bot = prompt_bot(FakeMemory())
    bot.config = SimpleNamespace(MAXWELL_PUBLIC_BASE_URL="")
    bot.prompts = PromptManager()
    MaxwellBot._install_core_prompt_components(bot)
    bot._get_personality = MaxwellBot._get_personality.__get__(bot)
    messages = asyncio.run(MaxwellBot._build_messages(bot, _message(), "privacy policy?"))
    system = "\n".join(str(m["content"]) for m in messages if m["role"] == "system")
    assert "https://another.example.test/" not in system
    assert "https://maxwell.z3ki.dev/privacy/" not in system


def test_first_dm_does_not_send_a_notice_even_with_legacy_opt_in(monkeypatch):
    monkeypatch.setenv("MAXWELL_LEGAL_NOTICE", "true")
    bot = dm_bot(reply_dms=False)
    bot._load_control = lambda: None
    bot.clear_message_taint = lambda message: None
    # Existing installations may still have loaded notice records/locks.
    bot._legal_notice_users = {}
    bot._legal_notice_locks = {}
    bot.config = SimpleNamespace(MAXWELL_PUBLIC_BASE_URL="https://operator.example.test")
    message = _dm_message()
    message.author.bot = False
    message.author.send = AsyncMock()
    result = asyncio.run(MaxwellBot._on_message_impl(bot, message))
    assert result == "dm_replies_disabled"
    message.author.send.assert_not_awaited()
