"""Usage reads the caller's local message quota, never provider accounts."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from message_quota import MessageQuota
from plugins.diagnostics import setup
from plugins.diagnostics.impl import UsageTool
from usage_commands import usage_text_for


def make_bot(tmp_path, *, enabled=True):
    return SimpleNamespace(
        _control={
            "message_quota_enabled": enabled,
            "message_quota_limit": 100,
            "message_quota_window_seconds": 18000,
        },
        _message_quota=MessageQuota(tmp_path / "quota.sqlite3", clock=lambda: 1000),
    )


def test_usage_is_public_read_only_and_returns_a_followup_result(tmp_path):
    tool = next(t for t in setup(make_bot(tmp_path)) if t.get_name() == "usage")
    assert tool.requires_admin is False
    assert tool.side_effects is False
    assert tool.returns_result is True
    assert tool.ends_turn is False
    assert tool.get_parameters()["properties"] == {}
    manifest = json.loads(
        (Path(__file__).resolve().parents[1] / "plugins/diagnostics/plugin.json").read_text()
    )
    declaration = next(t for t in manifest["tools"] if t["name"] == "usage")
    assert declaration["requires_admin"] is False


def test_usage_reads_only_caller_and_matches_slash_command(tmp_path):
    bot = make_bot(tmp_path)
    bot._message_quota.configure("caller", limit=4)
    bot._message_quota.charge("caller", 100, 18000)
    for _ in range(3):
        bot._message_quota.charge("other", 100, 18000)
    message = SimpleNamespace(author=SimpleNamespace(id="caller"))
    result = asyncio.run(UsageTool(bot).execute(message, user_id="other"))
    assert "25%" in result
    assert "75% remaining" in result
    assert "5 hours" in result
    assert result == usage_text_for(bot, "caller")
    assert bot._message_quota.status("caller", 100, 18000)["used"] == 1


@pytest.mark.parametrize("enabled,exempt", [(False, False), (True, True)])
def test_unlimited_usage_has_no_misleading_percentage(tmp_path, enabled, exempt):
    bot = make_bot(tmp_path, enabled=enabled)
    bot._message_quota.configure("caller", exempt=exempt)
    result = asyncio.run(
        UsageTool(bot).execute(SimpleNamespace(author=SimpleNamespace(id="caller")))
    )
    assert "Unlimited messages" in result
    assert "%" not in result


def test_usage_handles_missing_identity_and_ledger(tmp_path):
    bot = make_bot(tmp_path)
    assert "unknown" in asyncio.run(UsageTool(bot).execute(SimpleNamespace()))
    bot._message_quota = None
    assert "unavailable" in asyncio.run(
        UsageTool(bot).execute(SimpleNamespace(user=SimpleNamespace(id="caller")))
    )


def test_lowered_limit_never_reports_negative_remaining(tmp_path):
    bot = make_bot(tmp_path)
    for _ in range(2):
        bot._message_quota.charge("caller", 100, 18000)
    bot._message_quota.configure("caller", limit=1)
    result = asyncio.run(
        UsageTool(bot).execute(SimpleNamespace(author=SimpleNamespace(id="caller")))
    )
    assert "200%" in result
    assert "0% remaining" in result
