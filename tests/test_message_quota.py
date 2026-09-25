"""Customer-facing message quotas and the not-for-sale Plus discovery copy."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from api.state import _sanitize_control
from bot import DISCORD_CHAT_PROTOCOL, MaxwellBot, _current_inbound
from message_quota import (
    FREE_MESSAGE_LIMIT,
    MessageQuota,
    MessageQuotaExceeded,
    enforced_message_limit,
)
from providers import ProviderResult
from usage_commands import (
    HELP_COMMAND,
    command_help_text,
    handle_discovery_interaction,
    premium_discovery_text,
    usage_status_text,
)


def test_rolling_window_drops_aged_messages(tmp_path):
    now = {"t": 1_000_000.0}
    quota = MessageQuota(tmp_path / "messages.sqlite3", clock=lambda: now["t"])
    assert quota.charge("12", 2, 100) is not None
    assert quota.charge("12", 2, 100) is not None
    with pytest.raises(MessageQuotaExceeded) as exc:
        quota.charge("12", 2, 100)
    assert "token" not in str(exc.value).lower()
    assert "premium" not in str(exc.value).lower()
    assert "current message allowance" in str(exc.value).lower()
    assert "2/2" not in str(exc.value)
    now["t"] += 101
    assert quota.charge("12", 2, 100) is not None
    assert quota.status("12", 2, 100)["used"] == 1


def test_concurrent_charges_cannot_oversubscribe(tmp_path):
    quota = MessageQuota(tmp_path / "messages.sqlite3")

    def charge(_):
        try:
            return quota.charge("12", 3, 3600)
        except MessageQuotaExceeded:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(charge, range(8)))
    assert len([item for item in results if item]) == 3
    assert quota.status("12", 3, 3600)["used"] == 3


def test_override_exemption_and_reset(tmp_path):
    path = tmp_path / "messages.sqlite3"
    quota = MessageQuota(path)
    quota.configure("12", limit=1)
    quota.charge("12", 100, 3600)
    with pytest.raises(MessageQuotaExceeded):
        quota.charge("12", 100, 3600)
    quota.configure("12", exempt=True)
    assert quota.charge("12", 100, 3600) is None
    quota.configure("12", reset=True, exempt=False)
    assert MessageQuota(path).status("12", 100, 3600)["used"] == 0
    quota.configure("12", clear=True)
    assert quota.status("12", 100, 3600)["limit"] == 100


def test_plus_allowances_are_not_enforced():
    control = {
        "message_quota_limit": 40,
        "message_quota_personal_plus_limit": 5000,
        "message_quota_server_plus_limit": 20000,
        "message_quota_server_fair_use_limit": 500,
        "premium_billing_enabled": True,
    }
    assert enforced_message_limit(control) == 40
    sanitized = _sanitize_control(control)
    assert sanitized["premium_billing_enabled"] is False
    assert sanitized["message_quota_personal_plus_limit"] == 5000
    assert enforced_message_limit(sanitized) == 40
    assert FREE_MESSAGE_LIMIT == 300


def test_retired_daily_token_controls_are_discarded():
    sanitized = _sanitize_control({
        "daily_user_token_limit_enabled": True,
        "daily_user_token_limit": 12345,
        "message_quota_limit": 25,
    })
    assert "daily_user_token_limit_enabled" not in sanitized
    assert "daily_user_token_limit" not in sanitized
    assert sanitized["message_quota_limit"] == 25


def test_premium_copy_states_prices_and_refuses_sale():
    text = premium_discovery_text()
    assert "$2.99/month per user" in text
    assert "$4.99/month per server" in text
    assert "higher individual allowance" in text
    assert "may change as needed" in text
    assert "percentage" in text
    assert "300 messages per rolling 5 hours" not in text
    assert "not for sale" in text
    assert "cannot be transferred" in text
    assert "Guild Subscription" in text
    assert "not decided" in text
    assert "http" not in text
    assert "buy now" not in text.lower()
    off = premium_discovery_text(discovery=False)
    assert off == "Plan details are turned off."
    assert "$2.99" not in off


def test_help_and_usage_mention_premium_only_as_discovery():
    shown = command_help_text(discovery=True)
    hidden = command_help_text(discovery=False)
    assert "/usage" in shown and "/premium" in shown
    assert "/premium" not in hidden
    assert "$2.99" not in shown
    usage = usage_status_text(
        {"used": 3, "limit": 100, "window_seconds": 18000, "resets_in": 0},
        discovery=True,
    )
    assert "3%" in usage
    assert "3/100" not in usage
    assert "Plan details: /premium" in usage
    assert "$4.99" not in usage
    quiet = usage_status_text(
        {"used": 3, "limit": 100, "window_seconds": 18000, "resets_in": 0},
        discovery=False,
    )
    assert "/premium" not in quiet


def test_help_browses_topics_and_returns_only_the_selected_section():
    topic_option = HELP_COMMAND["options"][0]
    assert topic_option["name"] == "topic"
    assert {choice["value"] for choice in topic_option["choices"]} >= {
        "start",
        "personal",
        "server",
        "memory",
    }
    assert "/config" in command_help_text(discovery=False, topic="personal")
    assert "/personality" in command_help_text(discovery=False, topic="personal")
    assert "/image" not in command_help_text(discovery=False, topic="personal")


def test_help_interaction_uses_selected_topic():
    sent = []

    class Response:
        def is_done(self):
            return False

        async def send_message(self, text, *, ephemeral=False):
            sent.append((text, ephemeral))

    interaction = SimpleNamespace(
        data={
            "name": "help",
            "options": [{"name": "topic", "value": "personal"}],
        },
        response=Response(),
    )
    assert asyncio.run(
        handle_discovery_interaction(SimpleNamespace(_control={}), interaction)
    )
    assert sent and sent[0][1] is True
    assert "/config" in sent[0][0]
    assert "/personality" in sent[0][0]
    assert "/image" not in sent[0][0]


def test_protocol_does_not_pitch_premium():
    assert "Do not advertise Premium" in DISCORD_CHAT_PROTOCOL
    assert "promotional DMs" in DISCORD_CHAT_PROTOCOL


def test_live_turn_charges_one_message_not_followups(tmp_path):
    ledger = MessageQuota(tmp_path / "messages.sqlite3")
    calls = []

    async def generate(_messages, **kwargs):
        calls.append(kwargs)
        return ProviderResult("ok", usage={"total_tokens": 10, "cost_usd": 0.01})

    bot = object.__new__(MaxwellBot)
    bot._control = {
        "message_quota_enabled": True,
        "message_quota_limit": 100,
        "message_quota_window_seconds": 18000,
    }
    bot._message_quota = ledger
    bot.ai_provider = SimpleNamespace(generate_response=generate, model="test")
    bot._night_fallback_kwargs = dict
    message = SimpleNamespace(
        id=1,
        author=SimpleNamespace(id=42, bot=False),
        channel=SimpleNamespace(id=2),
        guild=SimpleNamespace(id=7),
    )
    token = _current_inbound.set(message)
    try:
        async def run():
            await MaxwellBot._generate_response(
                bot, [{"role": "user", "content": "hi"}], charge_message=True, max_tokens=20
            )
            await MaxwellBot._generate_response(
                bot, [{"role": "user", "content": "follow up"}], max_tokens=20
            )

        asyncio.run(run())
    finally:
        _current_inbound.reset(token)
    assert len(calls) == 2
    assert ledger.status("42", 100, 18000)["used"] == 1


def test_premium_command_replies_in_place_and_does_not_dm():
    sent = []

    async def send_message(text, ephemeral=False):
        sent.append((text, ephemeral))

    interaction = SimpleNamespace(
        data={"name": "premium"},
        user=SimpleNamespace(id=9),
        response=SimpleNamespace(is_done=lambda: False, send_message=send_message),
        followup=None,
    )
    bot = SimpleNamespace(
        _control={
            "premium_discovery_enabled": True,
            "message_quota_limit": 100,
            "message_quota_window_seconds": 18000,
        },
        _message_quota=None,
    )
    assert asyncio.run(handle_discovery_interaction(bot, interaction)) is True
    assert sent and sent[0][1] is True
    assert "$2.99/month per user" in sent[0][0]
    assert "not for sale" in sent[0][0]
    assert not hasattr(interaction.user, "send") or interaction.user.send is None
