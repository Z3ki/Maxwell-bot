"""Discord forwards put the original text/media on message_snapshots."""

import asyncio
from types import SimpleNamespace

from bot import MaxwellBot
from utils import (
    iter_message_snapshots,
    message_combined_content,
    message_has_visible_payload,
    message_reference_is_forward,
    render_discord_context_text,
)


def _forward_type():
    return SimpleNamespace(name="forward", value=1)


def _snapshot(*, content="", attachments=None, embeds=None, stickers=None):
    return SimpleNamespace(
        content=content,
        attachments=list(attachments or []),
        embeds=list(embeds or []),
        stickers=list(stickers or []),
        components=[],
        poll=None,
        type="MessageType.default",
    )


def _forwarded_message(
    *,
    content="",
    snapshot=None,
    mentions=None,
    author_id=99,
    channel=None,
    resolved=None,
):
    snap = snapshot or _snapshot(content="original caption")
    return SimpleNamespace(
        id=55,
        content=content,
        attachments=[],
        embeds=[],
        stickers=[],
        components=[],
        poll=None,
        mentions=list(mentions or []),
        author=SimpleNamespace(id=author_id, display_name="Alice", bot=False),
        channel=channel or SimpleNamespace(id=9, __class__=object),
        guild=None,
        message_snapshots=[snap],
        reference=SimpleNamespace(
            type=_forward_type(),
            resolved=resolved,
            message_id=88,
            channel_id=77,
            guild_id=66,
        ),
        flags=SimpleNamespace(forwarded=True),
        type="MessageType.default",
        created_at=None,
    )


def test_snapshot_helpers_see_forwarded_text_and_files():
    att = SimpleNamespace(filename="cat.png", content_type="image/png")
    msg = _forwarded_message(
        snapshot=_snapshot(content="look at this cat", attachments=[att])
    )
    assert message_reference_is_forward(msg) is True
    assert message_has_visible_payload(msg) is True
    assert iter_message_snapshots(msg)[0].content == "look at this cat"
    assert message_combined_content(msg) == "look at this cat"


def test_comment_plus_forward_keeps_both_texts():
    msg = _forwarded_message(
        content="what is this",
        snapshot=_snapshot(content="original caption"),
    )
    assert message_combined_content(msg) == "what is this\noriginal caption"


def test_context_text_includes_forwarded_caption_and_image():
    att = SimpleNamespace(
        filename="shot.png",
        content_type="image/png",
        duration=None,
        waveform=None,
    )
    msg = _forwarded_message(
        snapshot=_snapshot(content="the secret photo", attachments=[att])
    )
    rendered = render_discord_context_text(msg)
    assert "forwarded message" in rendered
    assert "the secret photo" in rendered
    assert "shot.png" in rendered
    assert "#77" in rendered


def test_silent_image_forward_is_not_an_empty_message():
    att = SimpleNamespace(
        filename="pic.jpg",
        content_type="image/jpeg",
        duration=None,
        waveform=None,
    )
    msg = _forwarded_message(snapshot=_snapshot(attachments=[att]))
    assert not msg.content
    assert not msg.attachments
    assert message_has_visible_payload(msg) is True
    rendered = render_discord_context_text(msg)
    assert "forwarded message" in rendered
    assert "pic.jpg" in rendered


def test_forward_of_maxwell_is_not_a_reply_ping():
    bot = SimpleNamespace(user=SimpleNamespace(id=1), _message_snapshots={})
    original = SimpleNamespace(id=88, author=bot.user, content="I said this")
    msg = _forwarded_message(resolved=original)
    assert MaxwellBot._directly_addressed(bot, msg) is False
    assert MaxwellBot._reply_parent(bot, msg) is None
    assert MaxwellBot._reply_meta_from_message(bot, msg) == {}


def test_mention_plus_forward_still_addresses_maxwell():
    bot = SimpleNamespace(user=SimpleNamespace(id=1), _message_snapshots={})
    bot._directly_addressed = MaxwellBot._directly_addressed.__get__(bot)
    bot._content_without_self_mention = MaxwellBot._content_without_self_mention.__get__(
        bot
    )
    msg = _forwarded_message(mentions=[bot.user], content=f"<@{bot.user.id}>")
    assert MaxwellBot._directly_addressed(bot, msg) is True
    assert MaxwellBot._is_bare_ping(bot, msg) is False


def test_bare_ping_with_forwarded_image_is_not_empty():
    bot = SimpleNamespace(user=SimpleNamespace(id=1), _message_snapshots={})
    bot._directly_addressed = MaxwellBot._directly_addressed.__get__(bot)
    bot._content_without_self_mention = MaxwellBot._content_without_self_mention.__get__(
        bot
    )
    att = SimpleNamespace(filename="x.png", content_type="image/png")
    msg = _forwarded_message(
        content=f"<@{bot.user.id}>",
        mentions=[bot.user],
        snapshot=_snapshot(attachments=[att]),
    )
    assert MaxwellBot._is_bare_ping(bot, msg) is False


def test_reply_to_a_forward_still_resolves_the_wrapper():
    bot = SimpleNamespace(user=SimpleNamespace(id=1), _message_snapshots={})
    forwarded = _forwarded_message()
    reply = SimpleNamespace(
        author=SimpleNamespace(id=99, display_name="Alice"),
        content="what is that",
        mentions=[],
        channel=SimpleNamespace(id=9, __class__=object),
        reference=SimpleNamespace(resolved=forwarded, message_id=forwarded.id),
        guild=None,
    )
    assert MaxwellBot._reply_parent(bot, reply) is forwarded


def _media_bot():
    async def _no_stickers(_message, _max_size):
        return []

    bot = SimpleNamespace(
        _control={"process_images": True, "process_audio": False},
        config=SimpleNamespace(ENABLE_VIDEO_INPUT=True),
        _payload_attr_list=MaxwellBot._payload_attr_list,
        _media_item=MaxwellBot._media_item,
        _extract_sticker_emoji_media=_no_stickers,
        _media_link_refs=MaxwellBot._media_link_refs,
        _LINK_IMAGE_EXTS=MaxwellBot._LINK_IMAGE_EXTS,
        _LINK_AUDIO_EXTS=MaxwellBot._LINK_AUDIO_EXTS,
        _LINK_VIDEO_EXTS=MaxwellBot._LINK_VIDEO_EXTS,
        _recent_users={},
    )
    bot._max_media_bytes = MaxwellBot._max_media_bytes.__get__(bot)
    return bot


def test_extract_media_reads_forwarded_attachments():
    class _Att:
        filename = "cat.png"
        content_type = "image/png"
        size = 4
        url = "https://cdn.discordapp.com/cat.png"

        async def read(self):
            return b"\x89PNG"

    bot = _media_bot()
    msg = _forwarded_message(snapshot=_snapshot(attachments=[_Att()]))
    images, media = asyncio.run(MaxwellBot._extract_media(bot, msg))
    assert images
    assert media[0]["filename"] == "cat.png"
    assert media[0]["source"] == "forward"


def test_linked_media_and_embeds_come_from_snapshots():
    bot = _media_bot()
    msg = _forwarded_message(
        snapshot=_snapshot(
            content="https://cdn.example.com/x.png",
            embeds=[SimpleNamespace(title="preview", url="https://example.com")],
        )
    )
    assert MaxwellBot._message_carries_media(bot, msg) is True
    refs = MaxwellBot._media_link_refs(message_combined_content(msg))
    assert refs and refs[0][0].endswith("/x.png")
    embeds = MaxwellBot._payload_attr_list(msg, "embeds", 8)
    assert embeds and embeds[0].title == "preview"


def test_reply_media_id_sees_snapshot_media_on_parent():
    parent = _forwarded_message(
        snapshot=_snapshot(
            attachments=[SimpleNamespace(filename="a.png", content_type="image/png")]
        )
    )
    reply = SimpleNamespace(
        reference=SimpleNamespace(resolved=parent, message_id=parent.id)
    )
    assert MaxwellBot._reply_media_message_id(reply) == parent.id


def test_memory_content_includes_forwarded_attachment_name():
    bot = _media_bot()
    att = SimpleNamespace(filename="secret.png", content_type="image/png")
    msg = _forwarded_message(snapshot=_snapshot(content="peek", attachments=[att]))
    text = MaxwellBot._message_memory_content(bot, msg)
    assert "secret.png" in text
    assert "peek" in text
    assert "forwarded message" in text
