"""Pinging Maxwell on a self-reply includes the parent message in full."""

from types import SimpleNamespace

from bot import MaxwellBot


def _bot():
    bot = SimpleNamespace(
        user=SimpleNamespace(id=1382894657624866889),
        _recent_users={},
    )
    bot._reply_parent = MaxwellBot._reply_parent.__get__(bot)
    bot._replying_to_own_message = MaxwellBot._replying_to_own_message.__get__(bot)
    bot._render_reply_parent = MaxwellBot._render_reply_parent.__get__(bot)
    bot._reply_parent_context_lines = MaxwellBot._reply_parent_context_lines.__get__(
        bot
    )
    bot._directly_addressed = MaxwellBot._directly_addressed.__get__(bot)
    return bot


def _parent(
    *,
    author,
    content="the long thing I said",
    embeds=None,
    attachments=None,
    components=None,
):
    return SimpleNamespace(
        id=1,
        author=author,
        content=content,
        embeds=embeds or [],
        attachments=attachments or [],
        stickers=[],
        components=components or [],
        mentions=[],
        channel_mentions=[],
        role_mentions=[],
        poll=None,
        interaction=None,
        type="MessageType.default",
        created_at=None,
        guild=None,
    )


def _self_reply(bot, *, ping=True, parent=None, parent_content="the long thing I said"):
    author = SimpleNamespace(id=147, display_name="Z3ki", bot=False)
    parent = parent or _parent(author=author, content=parent_content)
    msg = SimpleNamespace(
        author=author,
        content="what about this",
        mentions=[bot.user] if ping else [],
        channel=SimpleNamespace(id=9, __class__=object),
        reference=SimpleNamespace(resolved=parent),
        guild=None,
    )
    return msg, parent


def test_replying_to_own_message_is_detected():
    bot = _bot()
    msg, _parent = _self_reply(bot)
    assert MaxwellBot._replying_to_own_message(bot, msg) is True
    other = SimpleNamespace(
        id=2,
        author=SimpleNamespace(id=99, display_name="Alice"),
        content="nope",
    )
    msg.reference = SimpleNamespace(resolved=other)
    assert MaxwellBot._replying_to_own_message(bot, msg) is False


def test_ping_on_own_reply_includes_full_parent_not_a_snip():
    bot = _bot()
    author = SimpleNamespace(id=147, display_name="Z3ki", bot=False)
    long = "A" * 500 + " important ending"
    parent = _parent(
        author=author,
        content=long,
        embeds=[
            SimpleNamespace(
                title="clip title",
                description="the whole caption goes here",
                url="https://example.com/x",
                author=None,
                fields=[],
                image=None,
                thumbnail=None,
                footer=None,
            )
        ],
        attachments=[
            SimpleNamespace(
                filename="shot.png",
                content_type="image/png",
                duration=None,
                waveform=None,
            ),
            SimpleNamespace(
                filename="voice.ogg",
                content_type="audio/ogg",
                duration=4.2,
                waveform=b"xx",
            ),
        ],
        components=[
            SimpleNamespace(
                label="Open",
                url="https://example.com/go",
                custom_id="",
                emoji=None,
                options=[],
                placeholder="",
                children=None,
            )
        ],
    )
    msg, _p = _self_reply(bot, parent=parent)
    assert MaxwellBot._directly_addressed(bot, msg) is True
    lines = MaxwellBot._reply_parent_context_lines(bot, msg)
    blob = "\n".join(lines)
    assert "their own earlier message" in blob
    assert "important ending" in blob
    assert "clip title" in blob
    assert "the whole caption goes here" in blob
    assert "shot.png" in blob
    assert "voice.ogg" in blob
    assert "Open" in blob
    assert "They are answering" not in blob


def test_ping_on_someone_elses_reply_still_loads_the_full_parent():
    bot = _bot()
    alice = SimpleNamespace(id=99, display_name="Alice")
    parent = _parent(author=alice, content="B" * 500 + " alice ending")
    msg, _p = _self_reply(bot, parent=parent)
    lines = MaxwellBot._reply_parent_context_lines(bot, msg)
    blob = "\n".join(lines)
    assert "alice ending" in blob
    assert "This is a reply to Alice(99)" in blob
    assert "They are answering Alice" in blob


def test_unpinged_reply_to_someone_else_stays_a_short_pointer():
    bot = _bot()
    alice = SimpleNamespace(id=99, display_name="Alice")
    parent = _parent(author=alice, content="C" * 500 + " hidden ending")
    msg, _p = _self_reply(bot, ping=False, parent=parent)
    lines = MaxwellBot._reply_parent_context_lines(bot, msg)
    blob = "\n".join(lines)
    assert "This is a reply to Alice(99)" in blob
    assert "They are answering Alice" in blob
    assert "hidden ending" not in blob
    assert len(blob) < 600


def test_reply_media_message_id_sees_embeds_and_stickers():
    embed_msg = SimpleNamespace(
        reference=SimpleNamespace(
            resolved=SimpleNamespace(
                id=11,
                attachments=[],
                stickers=[],
                embeds=[SimpleNamespace(url="https://x")],
            )
        )
    )
    sticker_msg = SimpleNamespace(
        reference=SimpleNamespace(
            resolved=SimpleNamespace(
                id=12,
                attachments=[],
                stickers=[SimpleNamespace(name="wave")],
                embeds=[],
            )
        )
    )
    empty_msg = SimpleNamespace(
        reference=SimpleNamespace(
            resolved=SimpleNamespace(
                id=13, attachments=[], stickers=[], embeds=[]
            )
        )
    )
    assert MaxwellBot._reply_media_message_id(embed_msg) == 11
    assert MaxwellBot._reply_media_message_id(sticker_msg) == 12
    assert MaxwellBot._reply_media_message_id(empty_msg) is None
