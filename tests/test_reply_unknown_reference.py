"""Replying to a message that was deleted mid-turn must not kill the reply.

Discord does NOT answer that with a 404. It answers the send with a 400
"Invalid Form Body / In message_reference: Unknown message" (error code
50035), which discord.py raises as a bare ``discord.HTTPException`` — so the
``except discord.NotFound`` guards never fired and the whole turn blew up
with an unhandled exception (pm2 maxwell-bot-error.log 2026-07-21 12:47 and
2026-07-23 09:24, both "ERROR - Error handling message").
"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import discord
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot import MaxwellBot, _is_unknown_reference_error  # noqa: E402


def _http_exc(status, code, message, errors_key=None):
    data = {"code": code, "message": message}
    if errors_key:
        data["errors"] = {
            errors_key: {"_errors": [{"code": "X", "message": "Unknown message"}]}
        }
    return discord.HTTPException(
        SimpleNamespace(status=status, reason="Bad Request"), data
    )


class _FakeChannel:
    id = 999
    slowmode_delay = 0

    def __init__(self):
        self.sent = []

    async def send(self, content=None, file=None, **kwargs):
        msg = SimpleNamespace(id=len(self.sent) + 1, content=content)
        self.sent.append(msg)
        return msg


class _FakeParent:
    """A message whose reply() fails the way Discord fails a dead parent."""

    def __init__(self, exc):
        self._exc = exc
        self.reply_calls = 0

    async def reply(self, content=None, file=None, **kwargs):
        self.reply_calls += 1
        raise self._exc


class _StubBot:
    """Just enough of MaxwellBot to exercise _send_with_slowmode."""

    def __init__(self):
        self.marked = []

    async def _respect_slowmode(self, channel):
        return None

    def _mark_bot_sent(self, channel):
        self.marked.append(channel)

    _send_with_slowmode = MaxwellBot._send_with_slowmode


def test_unknown_reference_detects_the_400_flavour():
    exc = _http_exc(400, 50035, "Invalid Form Body", errors_key="message_reference")
    assert _is_unknown_reference_error(exc) is True


def test_unknown_reference_detects_the_404_flavour():
    exc = discord.NotFound(
        SimpleNamespace(status=404, reason="Not Found"),
        {"code": 10008, "message": "Unknown Message"},
    )
    assert _is_unknown_reference_error(exc) is True


def test_other_invalid_form_body_errors_are_not_swallowed():
    """50035 also covers over-length content and bad embeds. Those are real
    bugs in our payload and must keep propagating, not be retried blindly."""
    exc = _http_exc(400, 50035, "Invalid Form Body", errors_key="content")
    assert _is_unknown_reference_error(exc) is False


def test_reply_to_deleted_parent_falls_back_to_channel_send():
    bot = _StubBot()
    channel = _FakeChannel()
    parent = _FakeParent(
        _http_exc(400, 50035, "Invalid Form Body", errors_key="message_reference")
    )

    sent = asyncio.run(
        bot._send_with_slowmode(channel, content="the answer", reply_to=parent)
    )

    assert parent.reply_calls == 1
    assert sent is not None
    assert [m.content for m in channel.sent] == ["the answer"]


def test_unrelated_http_error_on_reply_still_propagates():
    bot = _StubBot()
    channel = _FakeChannel()
    parent = _FakeParent(_http_exc(500, 0, "Internal Server Error"))

    with pytest.raises(discord.HTTPException):
        asyncio.run(
            bot._send_with_slowmode(channel, content="the answer", reply_to=parent)
        )

    assert channel.sent == []
