"""Requester permissions gate historical reply text and visual context."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from bot import MaxwellBot


def _channel(*, channel_id=22, view=True, history=True):
    guild = SimpleNamespace(id=33)
    channel = SimpleNamespace(id=channel_id, guild=guild, name="chat")
    channel.permissions_for = Mock(return_value=SimpleNamespace(
        view_channel=view, read_message_history=history,
    ))
    channel.fetch_message = AsyncMock()
    return channel


def _message(channel, message_id=100, *, content="question"):
    return SimpleNamespace(
        id=message_id, channel=channel, guild=channel.guild,
        author=SimpleNamespace(id=11, display_name="Alice", bot=False),
        content=content, reference=None, mentions=[], attachments=[],
        stickers=[], embeds=[], components=[], poll=None,
    )


def _bot():
    bot = object.__new__(MaxwellBot)
    bot._connection = SimpleNamespace(user=SimpleNamespace(id=1))
    bot._control = {}
    bot._recent_users = {}
    bot._message_snapshots = {}
    bot._inflight_context = {}
    bot._media_context = {}
    return bot


@pytest.mark.parametrize("view,history", [(False, True), (True, False)])
def test_unresolved_reply_fetch_requires_requester_history_permission(view, history):
    bot = _bot()
    channel = _channel(view=view, history=history)
    message = _message(channel)
    message.reference = SimpleNamespace(message_id=99, resolved=None)
    asyncio.run(bot._ensure_reply_chain_resolved(message))
    channel.fetch_message.assert_not_awaited()
    channel.permissions_for.assert_called_with(message.author)


@pytest.mark.parametrize("view,history", [(False, True), (True, False)])
def test_cached_reply_text_metadata_and_pixels_require_requester_permission(view, history):
    bot = _bot()
    channel = _channel(view=view, history=history)
    message = _message(channel, content="describe the previous image")
    parent = _message(channel, 99, content="private historical caption")
    parent.attachments = [SimpleNamespace(filename="private.png")]
    message.reference = SimpleNamespace(message_id=99, resolved=parent)
    bot._message_snapshots["99"] = parent
    assert list(bot._iter_resolved_reply_chain(message)) == []
    assert bot._render_reply_parent(message, parent) == ""
    assert bot._reply_parent_context_lines(message) == []
    assert bot._reply_meta_from_message(message) == {}
    assert bot._reply_media_message_id(message, bot._message_snapshots) is None
    assert not bot._should_use_cached_media_context(message, message.content)


def test_reference_permissions_are_checked_in_the_target_channel():
    bot = _bot()
    source = _channel()
    target = _channel(channel_id=44, history=False)
    message = _message(source)
    parent = _message(target, 99, content="other channel secret")
    parent.attachments = [SimpleNamespace(filename="secret.png")]
    message.reference = SimpleNamespace(message_id=99, resolved=parent)
    assert bot._render_reply_parent(message, parent) == ""
    assert bot._reply_meta_from_message(message) == {}
    assert list(bot._iter_resolved_reply_chain(message)) == []
    assert bot._reply_media_message_id(message) is None
    asyncio.run(bot._ensure_reply_chain_resolved(message))
    target.fetch_message.assert_not_awaited()
    target.permissions_for.assert_called_with(message.author)


def test_authorized_reply_context_remains_available():
    bot = _bot()
    channel = _channel()
    message = _message(channel)
    parent = _message(channel, 99, content="authorized caption")
    parent.attachments = [SimpleNamespace(filename="photo.png")]
    message.reference = SimpleNamespace(message_id=99, resolved=parent)
    assert list(bot._iter_resolved_reply_chain(message)) == [parent]
    assert "authorized caption" in bot._render_reply_parent(message, parent)
    assert bot._reply_meta_from_message(message)["reply_to_message_id"] == "99"
    assert bot._reply_media_message_id(message) == 99


def test_inflight_refresh_excludes_parent_and_cached_pixels_when_history_denied():
    bot = _bot()
    channel = _channel(history=False)
    message = _message(channel, content="describe the previous image")
    root_image = {
        "message_id": 100, "filename": "current.png", "is_image": True,
        "mime_type": "image/png", "b64": "Y3VycmVudA==",
    }
    parent_image = {
        "message_id": 99, "filename": "private.png", "is_image": True,
        "mime_type": "image/png", "b64": "cHJpdmF0ZQ==",
    }
    state = bot._begin_inflight_context(message, message.content)
    state.update(version=1, latest_message=message,
                 latest_media=[root_image, parent_image])
    bot._get_media_context = Mock(side_effect=AssertionError("history cache read"))
    bot._build_messages = AsyncMock(return_value=[])
    result = asyncio.run(bot._apply_inflight_refresh(
        state, message, message.content, [], [], "", []
    ))
    assert result[2] == [root_image]
    assert result[3] == [root_image]
    assert "current.png" in result[4]
    assert "private.png" not in result[4]
    bot._get_media_context.assert_not_called()
