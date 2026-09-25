import asyncio
from types import SimpleNamespace

import user_install as ui
from control_defaults import DEFAULT_CONTROL
from daily_tokens import DailyTokens
from premium_discovery import DiscoveryStore, handle_interaction, help_text, install, premium_text, state


class Response:
    def __init__(self):
        self.messages = []
    def is_done(self):
        return bool(self.messages)
    async def send_message(self, **kwargs):
        self.messages.append(kwargs)


def interaction(name, options=None):
    return SimpleNamespace(data={"name": name, "options": options or []}, user=SimpleNamespace(id=123), response=Response())


def bot(tmp_path, mode="off"):
    return SimpleNamespace(_control={"premium_discovery_state": mode, "daily_user_token_limit": DEFAULT_CONTROL["daily_user_token_limit"]}, config=SimpleNamespace(DATA_DIR=str(tmp_path)), _daily_tokens=DailyTokens(tmp_path / "usage.sqlite3"))


def test_disabled_by_default_and_registration_reconciles(tmp_path, monkeypatch):
    assert DEFAULT_CONTROL["premium_discovery_state"] == "off"
    b = bot(tmp_path)
    monkeypatch.setattr(ui, "USER_INSTALL_COMMANDS", [])
    monkeypatch.setattr(ui, "USER_INSTALL_NAMES", frozenset())
    install(b)
    assert {c["name"] for c in ui.USER_INSTALL_COMMANDS} == {"help", "usage"}
    assert "premium" not in help_text(state(b))
    b._control["premium_discovery_state"] = "preview"
    install(b)
    assert {c["name"] for c in ui.USER_INSTALL_COMMANDS} == {"help", "usage", "premium"}
    b._control["premium_discovery_state"] = "off"
    install(b)
    assert "premium" not in {c["name"] for c in ui.USER_INSTALL_COMMANDS}
    ui.unregister_interaction_handler("premium_discovery")


def test_interactions_are_ephemeral_and_never_call_ai(tmp_path):
    b = bot(tmp_path, "preview")
    premium = interaction("premium")
    assert asyncio.run(handle_interaction(b, premium))
    text = premium.response.messages[0]["content"]
    assert premium.response.messages[0]["ephemeral"] is True
    assert "not yet available" in text and "proposed $3" in text
    assert "checkout" not in text.lower()
    help_cmd = interaction("help")
    asyncio.run(handle_interaction(b, help_cmd))
    assert "informational" in help_cmd.response.messages[0]["content"]
    assert asyncio.run(handle_interaction(b, interaction("maxwell"))) is False
    assert "subscriptions are unavailable" in premium_text("launched")


def test_usage_preferences_persist_and_quota_unchanged(tmp_path):
    b = bot(tmp_path, "launched")
    before = b._daily_tokens.status("123", 3_000_000)
    off = interaction("usage", [{"name": "notices", "value": "off"}])
    asyncio.run(handle_interaction(b, off))
    assert "Optional notices in /usage: off" in off.response.messages[0]["content"]
    assert "Plus" not in off.response.messages[0]["content"]
    assert not DiscoveryStore(tmp_path / "premium_discovery.sqlite3").notices(123)
    again = interaction("usage")
    asyncio.run(handle_interaction(b, again))
    assert "off" in again.response.messages[0]["content"]
    assert b._daily_tokens.status("123", 3_000_000) == before


def test_announcement_claim_survives_restart_and_requires_opt_in(tmp_path):
    path = tmp_path / "prefs.sqlite3"
    store = DiscoveryStore(path)
    assert not store.claim_announcement("launch-1", 5, 7)
    store.opt_in_channel(5, 7)
    assert not store.claim_announcement("launch-1", 5, 8)
    assert store.claim_announcement("launch-1", 5, 7)
    assert not DiscoveryStore(path).claim_announcement("launch-1", 5, 7)


def test_registered_interaction_path_is_handled_without_ai(tmp_path):
    b = bot(tmp_path, "preview")
    cmd = interaction("premium")
    ui.register_interaction_handler(handle_interaction, priority=11, name="premium_discovery")
    try:
        assert asyncio.run(ui.handle_user_install_interaction(b, cmd))
        assert len(cmd.response.messages) == 1
        assert cmd.response.messages[0]["ephemeral"]
    finally:
        ui.unregister_interaction_handler("premium_discovery")
