"""Inbound DMs are admin-only; reply_dms is the master off switch."""

from types import SimpleNamespace

import discord

from bot import MaxwellBot


class FakeDM(discord.DMChannel):
    def __init__(self, cid=99):
        self.id = cid


def _bot(*, reply_dms=True, admins=()):
    bot = SimpleNamespace(
        _control={
            "bot_enabled": True,
            "reply_dms": reply_dms,
            "reply_groups": True,
            "reply_mentions": True,
            "blocked_channels": [],
            "allowed_channels": [],
            "ignore_users": [],
        },
        _blacklist=set(),
        _admins={str(a) for a in admins},
    )
    bot._is_admin = lambda uid: str(uid) in bot._admins
    bot._solo_blocks = lambda _message: False
    bot._dm_replies_allowed = MaxwellBot._dm_replies_allowed.__get__(bot)
    bot._queued_request_policy_reason = MaxwellBot._queued_request_policy_reason.__get__(
        bot
    )
    return bot


def _dm_message(author_id=11):
    return SimpleNamespace(
        channel=FakeDM(99),
        author=SimpleNamespace(id=author_id),
        guild=None,
    )


def test_non_admin_dm_is_refused_even_when_reply_dms_is_on():
    bot = _bot(reply_dms=True, admins=("42",))
    message = _dm_message(author_id=1003210843984498748)
    assert bot._dm_replies_allowed(message) is False
    assert bot._queued_request_policy_reason(message) == "dm_replies_disabled"


def test_admin_dm_is_allowed_when_reply_dms_is_on():
    bot = _bot(reply_dms=True, admins=("42",))
    message = _dm_message(author_id=42)
    assert bot._dm_replies_allowed(message) is True
    assert bot._queued_request_policy_reason(message) == ""


def test_reply_dms_off_blocks_admins_too():
    bot = _bot(reply_dms=False, admins=("42",))
    message = _dm_message(author_id=42)
    assert bot._dm_replies_allowed(message) is False
    assert bot._queued_request_policy_reason(message) == "dm_replies_disabled"


def test_guild_messages_are_not_dm_gated():
    bot = _bot(reply_dms=False, admins=())
    message = SimpleNamespace(
        channel=SimpleNamespace(id=22),
        author=SimpleNamespace(id=11),
        guild=SimpleNamespace(id=33),
    )
    assert bot._dm_replies_allowed(message) is True
    assert bot._queued_request_policy_reason(message) == ""
