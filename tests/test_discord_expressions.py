"""Request-local Discord delivery denials also apply to webhook transports."""

from types import SimpleNamespace

import pytest

from discord_expressions import cannot_send, mark_send_unavailable


@pytest.mark.parametrize("transport", ["user_install", "webhook"])
def test_confirmed_send_failure_blocks_webhook_request_only(transport):
    bot = SimpleNamespace()
    channel = SimpleNamespace()
    message = SimpleNamespace(id=100, channel=channel, guild=None)
    following = SimpleNamespace(id=101, channel=channel, guild=None)
    if transport == "user_install":
        message.user_install = following.user_install = True
    else:
        channel._session = object()

    assert not cannot_send(message, bot)
    mark_send_unavailable(bot, message)
    assert cannot_send(message, bot)
    assert not cannot_send(following, bot)
