"""Rich-message permission failures must stay local and preserve the reply."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import discord
import pytest

from plugins.maxwell_extras.tools import SendRichMessageTool
from user_install import UserInstallChannelAdapter, UserInstallSession


def _http_error(cls=discord.Forbidden, *, status=403, code=50013):
    return cls(
        SimpleNamespace(status=status, reason="Rejected"),
        {"code": code, "message": "test-only failure"},
    )


def _message(*, permissions=None, channel_type=discord.ChannelType.text, fail=None):
    calls = []
    member = SimpleNamespace(id=123)
    guild = SimpleNamespace(me=member)

    async def send(**kwargs):
        calls.append(kwargs)
        if fail is not None:
            exc = fail(kwargs, len(calls))
            if exc is not None:
                raise exc
        return SimpleNamespace(id=len(calls))

    channel = SimpleNamespace(
        type=channel_type,
        guild=guild,
        permissions_for=lambda who: permissions,
        send=send,
    )
    return SimpleNamespace(channel=channel, guild=guild), calls


def _permissions(**overrides):
    return SimpleNamespace(
        **{
            "view_channel": True,
            "send_messages": True,
            "send_messages_in_threads": True,
            "embed_links": True,
            **overrides,
        }
    )


@pytest.mark.parametrize("mode", ["embed", "auto", "components_v2"])
def test_missing_embed_permission_preserves_text_fields_media_and_buttons(mode):
    message, calls = _message(permissions=_permissions(embed_links=False))
    result = asyncio.run(
        SendRichMessageTool(SimpleNamespace()).execute(
            message,
            mode=mode,
            title="Status",
            description="hello @everyone",
            fields=json.dumps([{"name": "State", "value": "Ready"}]),
            footer="Footer",
            image_url="https://example.org/image.png",
            thumbnail_url="https://example.org/thumb.png",
            buttons=json.dumps([{"label": "Open", "url": "https://example.org"}]),
        )
    )
    assert result.startswith("Sent plain-text fallback")
    assert len(calls) == 1
    payload = calls[0]
    assert payload["content"] == (
        "**Status**\n\nhello @everyone\n\n**State**\nReady\n\nFooter"
        "\n\nhttps://example.org/image.png\n\nhttps://example.org/thumb.png"
    )
    assert "embed" not in payload
    assert payload["view"].children[0].url == "https://example.org"
    assert payload["allowed_mentions"].to_dict() == {"parse": []}


@pytest.mark.parametrize(
    ("permission", "channel_type"),
    [
        ("view_channel", discord.ChannelType.text),
        ("send_messages", discord.ChannelType.text),
        ("send_messages_in_threads", discord.ChannelType.public_thread),
        ("send_messages_in_threads", discord.ChannelType.private_thread),
        ("send_messages_in_threads", discord.ChannelType.news_thread),
    ],
)
def test_missing_send_permissions_returns_actionable_error_without_sending(
    permission, channel_type
):
    message, calls = _message(
        permissions=_permissions(**{permission: False}), channel_type=channel_type
    )
    result = asyncio.run(SendRichMessageTool(None).execute(message, description="sowwy"))
    assert result.startswith("Error: missing permissions")
    assert permission in result
    assert calls == []


def test_thread_uses_thread_send_permission_instead_of_parent_send_permission():
    message, calls = _message(
        permissions=_permissions(send_messages=False),
        channel_type=discord.ChannelType.public_thread,
    )
    result = asyncio.run(
        SendRichMessageTool(None).execute(message, mode="embed", description="sowwy")
    )
    assert result == "Sent rich embed message."
    assert len(calls) == 1


@pytest.mark.parametrize("mode", ["embed", "components_v2"])
def test_live_403_falls_back_to_text_without_retrying_another_rich_format(mode):
    message, calls = _message(
        permissions=_permissions(),
        fail=lambda payload, _: _http_error() if "content" not in payload else None,
    )
    result = asyncio.run(
        SendRichMessageTool(None).execute(message, mode=mode, description="sowwy")
    )
    assert result.startswith("Sent plain-text fallback")
    assert len(calls) == 2
    assert calls[1]["content"] == "sowwy"
    assert "embed" not in calls[1]
    assert calls[1]["view"] is None


@pytest.mark.parametrize("mode", ["embed", "components_v2"])
def test_live_send_denial_returns_error_instead_of_crashing(mode):
    message, calls = _message(fail=lambda *_args: _http_error())
    result = asyncio.run(
        SendRichMessageTool(None).execute(message, mode=mode, description="sowwy")
    )
    assert result.startswith("Error: missing permissions")
    assert "Send Messages" in result
    assert len(calls) == 2
    assert "test-only failure" not in result


@pytest.mark.parametrize("mode", ["embed", "components_v2"])
def test_deleted_channel_is_not_retried(mode):
    message, calls = _message(
        fail=lambda *_args: _http_error(discord.NotFound, status=404, code=10003)
    )
    result = asyncio.run(
        SendRichMessageTool(None).execute(message, mode=mode, description="sowwy")
    )
    assert result.startswith("Error:")
    assert "unavailable" in result
    assert len(calls) == 1


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("uncertain delivery"),
        _http_error(discord.HTTPException, status=503, code=0),
        _http_error(discord.HTTPException, status=400, code=40060),
    ],
)
def test_uncertain_v2_failure_does_not_duplicate_the_send(error):
    message, calls = _message(fail=lambda *_args: error)
    with pytest.raises(type(error)):
        asyncio.run(
            SendRichMessageTool(None).execute(
                message, mode="components_v2", description="sowwy"
            )
        )
    assert len(calls) == 1


def test_explicit_v2_payload_rejection_can_fall_back_to_embed():
    message, calls = _message(
        fail=lambda _payload, count: (
            _http_error(discord.HTTPException, status=400, code=50035)
            if count == 1 else None
        )
    )
    result = asyncio.run(
        SendRichMessageTool(None).execute(
            message, mode="components_v2", description="sowwy"
        )
    )
    assert result == "Sent rich embed message."
    assert len(calls) == 2
    assert calls[1]["embed"].description == "sowwy"


@pytest.mark.parametrize("mode", ["embed", "components_v2"])
def test_dm_send_still_works_without_guild_permissions(mode):
    message, calls = _message()
    message.guild = message.channel.guild = None
    result = asyncio.run(
        SendRichMessageTool(None).execute(message, mode=mode, description="sowwy")
    )
    assert result.startswith("Sent")
    assert len(calls) == 1
    assert calls[0]["allowed_mentions"].to_dict() == {"parse": []}


def test_plain_fallback_chunks_without_losing_content():
    message, calls = _message(permissions=_permissions(embed_links=False))
    description = "hello " * 1500 + "the end"
    result = asyncio.run(
        SendRichMessageTool(None).execute(message, description=description)
    )
    assert result.startswith("Sent plain-text fallback")
    assert "".join(payload["content"] for payload in calls) == description
    assert all(len(payload["content"]) <= 2000 for payload in calls)


def test_aggregate_embed_limit_uses_text_instead_of_sending_invalid_embed():
    message, calls = _message(permissions=_permissions())
    fields = [{"name": f"Field {i}", "value": "x" * 1000} for i in range(7)]
    result = asyncio.run(
        SendRichMessageTool(None).execute(
            message, mode="embed", description="Status", fields=fields
        )
    )
    assert result.startswith("Sent plain-text fallback")
    assert all("embed" not in payload for payload in calls)
    assert "Field 6" in "".join(payload["content"] for payload in calls)


def test_partial_plain_delivery_is_reported_without_replaying_sent_chunks():
    message, calls = _message(
        permissions=_permissions(embed_links=False),
        fail=lambda _payload, count: _http_error() if count == 2 else None,
    )
    result = asyncio.run(
        SendRichMessageTool(None).execute(message, description="x" * 5000)
    )
    assert result.startswith("Sent 1 plain-text fallback messages")
    assert "Do not resend" in result
    assert len(calls) == 2


def test_older_discord_without_v2_support_uses_embed(monkeypatch):
    monkeypatch.delattr(discord.ui, "LayoutView")
    message, calls = _message(permissions=_permissions())
    result = asyncio.run(SendRichMessageTool(None).execute(message, description="sowwy"))
    assert result == "Sent rich embed message."
    assert len(calls) == 1
    assert calls[0]["embed"].description == "sowwy"


def test_user_install_replies_use_webhook_permissions_even_in_restricted_channel():
    original, channel_calls = _message(
        permissions=_permissions(view_channel=False, send_messages=False)
    )
    webhook_calls = []

    async def webhook_send(**kwargs):
        webhook_calls.append(kwargs)
        return SimpleNamespace(id=1)

    interaction = SimpleNamespace(
        guild=original.guild,
        channel=original.channel,
        channel_id=55,
        response=SimpleNamespace(is_done=lambda: True),
        followup=SimpleNamespace(send=webhook_send),
    )
    session = UserInstallSession(interaction)
    original.channel = UserInstallChannelAdapter(session)
    result = asyncio.run(
        SendRichMessageTool(None).execute(original, mode="embed", description="sowwy")
    )
    assert result == "Sent rich embed message."
    assert channel_calls == []
    assert len(webhook_calls) == 1
    assert webhook_calls[0]["embed"].description == "sowwy"
    assert webhook_calls[0]["ephemeral"] is True
