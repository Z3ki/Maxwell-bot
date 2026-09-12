"""Owner DM reports: explicit tool + rate limit."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot_tools import ReportTool, notify_owner


class FakeDM:
    def __init__(self):
        self.sent = []

    async def send(self, content):
        self.sent.append(content)


def _bot(*, uid="99", name="Zed"):
    dm = FakeDM()
    user = SimpleNamespace(id=int(uid), dm_channel=dm, create_dm=AsyncMock(return_value=dm))
    bot = SimpleNamespace(
        config=SimpleNamespace(
            CREATOR_ID=uid,
            CREATOR_NAME=name,
            MAXWELL_OWNER_IDS=(uid,),
            BOT_NAME="Maxwell",
            MAXWELL_USER_ID="",
            BOT_BIRTHDAY="2026-05-21",
            BOT_INVITE_URL="",
            OFFICIAL_INVITE="",
            COMMAND_PREFIX=",",
        ),
        get_user=lambda value: user if int(value) == int(uid) else None,
        fetch_user=AsyncMock(side_effect=AssertionError("must not fetch")),
        user=SimpleNamespace(id=1),
        _owner_notify_times=[],
        _owner_notify_fingerprints={},
        _owner_notify_sending=False,
    )
    bot._dm = dm
    bot._user = user
    return bot


def test_notify_owner_dms_configured_creator():
    bot = _bot()
    msg = SimpleNamespace(
        author=SimpleNamespace(id=5, display_name="Ada", name="ada"),
        channel=SimpleNamespace(id=21, name="general"),
        guild=SimpleNamespace(id=10, name="Villa"),
        id=777,
        content="kick the spammer",
    )
    result = asyncio.run(
        notify_owner(
            bot,
            kind="error",
            title="kick failed",
            details="missing permission",
            message=msg,
        )
    )
    assert "Reported to Zed (99)" in result
    assert bot._dm.sent
    body = "\n".join(bot._dm.sent)
    assert "Maxwell error" in body
    assert "kick failed" in body
    assert "Ada (5)" in body
    assert "#general" in body
    assert "Villa" in body
    assert "missing permission" in body
    assert "kick the spammer" in body


def test_notify_owner_rate_limits_duplicates():
    bot = _bot()
    first = asyncio.run(notify_owner(bot, kind="error", title="same crash"))
    second = asyncio.run(notify_owner(bot, kind="error", title="same crash"))
    assert "Reported" in first
    assert "already got this report" in second
    assert len(bot._dm.sent) == 1


def test_notify_owner_requires_owner_id(monkeypatch):
    monkeypatch.delenv("MAXWELL_OWNER_IDS", raising=False)
    monkeypatch.delenv("CREATOR_ID", raising=False)
    bot = _bot()
    bot.config.CREATOR_ID = ""
    bot.config.MAXWELL_OWNER_IDS = ()
    result = asyncio.run(notify_owner(bot, title="hello"))
    assert result.startswith("Error:")
    assert "owner Discord id" in result
    assert bot._dm.sent == []


def test_report_tool_sends_to_owner():
    bot = _bot()
    msg = SimpleNamespace(
        author=SimpleNamespace(id=8, display_name="Bob", name="bob"),
        channel=SimpleNamespace(id=3, name="help"),
        guild=None,
        id=1,
        content="this is broken",
    )
    result = asyncio.run(
        ReportTool(bot).execute(
            msg, what="site 500s", details="affinity-nexus is down", kind="report"
        )
    )
    assert "Reported to Zed" in result
    body = "\n".join(bot._dm.sent)
    assert "site 500s" in body
    assert "affinity-nexus is down" in body


def test_notify_owner_redacts_secrets(monkeypatch):
    bot = _bot()
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "super-secret-token-value")
    result = asyncio.run(
        notify_owner(
            bot,
            title="token leaked super-secret-token-value",
            details="token=super-secret-token-value",
        )
    )
    assert "Reported" in result
    body = "\n".join(bot._dm.sent)
    assert "super-secret-token-value" not in body
    assert "[redacted]" in body
