"""Signed URLs, message links and source-scoped CDN recovery."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import bot as bot_module
from bot import MaxwellBot, _current_inbound
from discord_media import (
    clean_media_url,
    discord_attachment_key,
    discord_message_link,
    refresh_discord_media_url,
    resolve_discord_message,
)

OLD = "https://cdn.discordapp.com/attachments/22/500/picture.png?ex=123&is=111&hm=old"
NEW = "https://cdn.discordapp.com/attachments/22/500/picture.png?ex=ffffffffff&is=222&hm=new"


def _message(channel, mid=100, url=OLD):
    return SimpleNamespace(
        id=mid,
        channel=channel,
        guild=SimpleNamespace(id=33),
        attachments=[SimpleNamespace(url=url, proxy_url=url)],
        content="",
        embeds=[],
        reference=None,
    )


@pytest.mark.parametrize(
    "wrapped", [f"`{OLD}`", f"<{OLD}>", f"**{OLD}**", OLD.replace("&", "&amp;")]
)
def test_signed_query_is_preserved_when_chat_wrappers_are_removed(wrapped):
    assert clean_media_url(wrapped) == OLD
    assert MaxwellBot._media_link_refs(wrapped) == [(OLD, ".png")]


def test_attachment_id_is_not_confused_with_message_id():
    channel = SimpleNamespace(id=22, fetch_message=AsyncMock())
    message = _message(channel)
    channel.fetch_message.return_value = _message(channel, url=NEW)
    assert (
        asyncio.run(refresh_discord_media_url(SimpleNamespace(), message, OLD)) == NEW
    )
    channel.fetch_message.assert_awaited_once_with(100)
    assert discord_attachment_key(OLD) == ("22", "500", "picture.png")


def test_refresh_and_message_links_do_not_use_other_private_channels():
    channel = SimpleNamespace(id=77, fetch_message=AsyncMock())
    message = _message(channel)
    bot = SimpleNamespace(_message_snapshots={100: _message(SimpleNamespace(id=22))})
    assert asyncio.run(refresh_discord_media_url(bot, message, OLD)) is None
    assert (
        asyncio.run(
            resolve_discord_message(message, "https://discord.com/channels/33/22/100")
        )
        is None
    )
    channel.fetch_message.assert_not_awaited()


def test_message_link_uses_the_real_channel_of_a_private_app_request():
    channel = SimpleNamespace(id=22, fetch_message=AsyncMock(return_value="resolved"))
    message = SimpleNamespace(
        channel=SimpleNamespace(id="private:11:33:22"),
        guild=None,
        interaction=SimpleNamespace(channel=channel, guild_id=33),
    )
    link = "https://discord.com/channels/33/22/100"
    assert discord_message_link(link) == ("33", "22", "100")
    assert asyncio.run(resolve_discord_message(message, link)) == "resolved"
    channel.fetch_message.assert_awaited_once_with(100)


def test_media_refresh_does_not_fetch_an_app_interaction_as_a_message():
    async def run():
        class Channel:
            id = 22
            fetch_message = AsyncMock()

            async def history(self, *, limit):
                assert limit == 25
                yield _message(self, url=NEW)

        channel = Channel()
        message = _message(channel, mid=900)
        message.user_install = True
        message.interaction = SimpleNamespace(channel=channel)
        message.channel = SimpleNamespace(id="private:11:33:22")
        assert await refresh_discord_media_url(SimpleNamespace(), message, OLD) == NEW
        channel.fetch_message.assert_not_awaited()

    asyncio.run(run())


def test_same_channel_message_link_passes_pixels_and_caption_to_context():
    async def run():
        channel = SimpleNamespace(id=22, fetch_message=AsyncMock())
        target = _message(channel, url=NEW)
        target.content = "Use this exact skin color"
        channel.fetch_message.return_value = target
        request = _message(channel, mid=200)
        request.content = "Inspect https://discord.com/channels/33/22/100"
        owner = SimpleNamespace(
            _control={"process_images": True, "process_audio": False},
            _media_link_refs=MaxwellBot._media_link_refs,
            _LINK_IMAGE_EXTS=MaxwellBot._LINK_IMAGE_EXTS,
            _LINK_AUDIO_EXTS=MaxwellBot._LINK_AUDIO_EXTS,
            _max_media_bytes=lambda: 1024,
            _media_item=MaxwellBot._media_item,
            _extract_media=AsyncMock(
                return_value=(
                    [],
                    [
                        {
                            "is_image": True,
                            "mime_type": "image/png",
                            "b64": "aW1hZ2U=",
                        }
                    ],
                )
            ),
            _extract_embeds=AsyncMock(return_value=[]),
        )
        items = await MaxwellBot._extract_linked_media(owner, request)
        assert any(item.get("b64") == "aW1hZ2U=" for item in items)
        assert any(
            "Use this exact skin color" in item.get("text", "") for item in items
        )
        owner._extract_media.assert_awaited_once_with(target)

    asyncio.run(run())


@pytest.mark.parametrize(
    "url",
    [
        "https://discord.com.evil.test/channels/33/22/100",
        "https://discord.com:invalid/channels/33/22/100",
        "https://[bad",
        "http://cdn.discordapp.com/attachments/22/500/a.png",
    ],
)
def test_malformed_or_lookalike_discord_urls_are_not_resolved(url):
    assert discord_message_link(url) is None
    assert discord_attachment_key(url) is None


def test_malformed_media_link_does_not_abort_message_intake():
    assert MaxwellBot._media_link_refs("look at https://[bad/picture.png") == []


def test_cdn_404_refetches_the_source_and_retries_once(monkeypatch):
    class Response:
        headers = {"Content-Type": "image/png"}
        content_length = 5

        def __init__(self, status):
            self.status = status
            self.content = self

        async def iter_chunked(self, _size):
            yield b"image"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

    class Session:
        def __init__(self):
            self.urls = []

        def get(self, url, **_kwargs):
            self.urls.append(url)
            return Response(200 if url == NEW else 404)

    async def run():
        channel = SimpleNamespace(id=22, fetch_message=AsyncMock())
        message = _message(channel)
        channel.fetch_message.return_value = _message(channel, url=NEW)
        owner = SimpleNamespace(
            _MAX_MEDIA_REDIRECTS=3, _REDIRECT_STATUSES={301, 302, 307, 308}
        )
        session = Session()
        monkeypatch.setattr(
            bot_module, "_get_shared_session", AsyncMock(return_value=session)
        )
        token = _current_inbound.set(message)
        try:
            result = await MaxwellBot._fetch_public_payload(owner, OLD, 100)
        finally:
            _current_inbound.reset(token)
        assert result == (NEW, "image/png", b"image")
        assert session.urls == [OLD, NEW]

    asyncio.run(run())


def test_history_timeout_keeps_the_rows_already_received(monkeypatch):
    import user_install as ui

    async def run():
        class Channel:
            async def history(self, *, limit):
                yield SimpleNamespace(id=100, content="keep this", author=None)
                await asyncio.Event().wait()

        monkeypatch.setattr(ui, "USER_INSTALL_HISTORY_TIMEOUT", 0.01, raising=False)
        interaction = SimpleNamespace(channel=Channel(), channel_id=22, data={})
        rows = await asyncio.wait_for(
            ui.snapshot_channel_history(None, interaction), timeout=0.2
        )
        assert len(rows) == 1 and rows[0]["content"] == "keep this"

    asyncio.run(run())
