"""Regression tests for Discord MESSAGE_UPDATE context refreshes."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from bot import MaxwellBot


class _Memory:
    def __init__(self):
        self.rows = []

    async def add_to_channel_memory(self, channel_id, row):
        self.rows.append((str(channel_id), dict(row)))


def _message(message_id, content, *, embeds=None):
    author = SimpleNamespace(id=11, display_name="Alice", bot=False)
    channel = SimpleNamespace(id=22, name="chat", guild=None)
    return SimpleNamespace(
        id=message_id,
        content=content,
        author=author,
        channel=channel,
        guild=None,
        embeds=list(embeds or []),
        attachments=[],
        stickers=[],
        components=[],
        poll=None,
        mentions=[],
        channel_mentions=[],
        role_mentions=[],
        mention_everyone=False,
        reference=None,
        created_at=datetime(2026, 8, 23, tzinfo=timezone.utc),
        type=SimpleNamespace(name="default"),
    )


def _bot():
    bot = object.__new__(MaxwellBot)
    bot._control = {
        "store_memory": True,
        "process_images": True,
        "process_audio": False,
        "ignore_users": [],
        "allowed_channels": [],
        "blocked_channels": [],
    }
    bot._blacklist = set()
    bot._message_snapshots = {}
    bot._message_update_state = {}
    bot._inflight_context = {}
    bot._media_context = {}
    bot._recent_users = {}
    bot.memory = _Memory()
    bot._is_admin = lambda _user_id: False
    bot._update_recent_users = lambda *_args: None

    async def add_message_to_memory(channel_id, row, _message=None):
        await bot.memory.add_to_channel_memory(channel_id, row)

    bot.add_message_to_memory = add_message_to_memory
    bot._cache_media_context = MaxwellBot._cache_media_context.__get__(bot)
    return bot


def test_message_edit_replaces_memory_text_and_visual_cache():
    bot = _bot()
    old = _message(77, "old text")
    new_embed = SimpleNamespace(
        title="new preview",
        description="updated embed description",
        url="https://example.com/post",
        image=SimpleNamespace(url="https://cdn.example/image.png"),
        thumbnail=None,
        video=None,
        author=None,
        provider=None,
        fields=[],
        footer=None,
    )
    after = _message(77, "new text", embeds=[new_embed])
    bot._media_context["22"] = [
        {
            "b64": "b2xk",
            "mime_type": "image/jpeg",
            "filename": "old.jpg",
            "message_id": 77,
            "uses_left": 1,
        }
    ]

    async def extract(message):
        return [
            {
                "b64": "bmV3",
                "mime_type": "image/png",
                "filename": "new.png",
                "is_image": True,
                "message_id": message.id,
                "url": "https://cdn.example/image.png",
            }
        ]

    bot._extract_context_media = extract
    asyncio.run(MaxwellBot.on_message_edit(bot, old, after))

    assert "new text" in bot.memory.rows[-1][1]["content"]
    assert "old text" not in bot.memory.rows[-1][1]["content"]
    cached = bot._media_context["22"]
    assert [item["filename"] for item in cached] == ["new.png"]


def test_embed_mutation_refreshes_text_without_redownloading_same_media():
    bot = _bot()
    calls = []

    def embed(description):
        return SimpleNamespace(
            title="preview",
            description=description,
            url="https://example.com/post",
            image=SimpleNamespace(url="https://cdn.example/image.png"),
            thumbnail=None,
            video=None,
            author=None,
            provider=None,
            fields=[],
            footer=None,
        )

    first = _message(88, "look", embeds=[embed("first")])
    second = _message(88, "look", embeds=[embed("second")])

    async def extract(message):
        calls.append(message.embeds[0].description)
        return [
            {
                "b64": "aW1hZ2U=",
                "mime_type": "image/png",
                "filename": "preview.png",
                "is_image": True,
                "message_id": message.id,
                "url": "https://cdn.example/image.png",
            }
        ]

    bot._extract_context_media = extract
    assert asyncio.run(MaxwellBot._refresh_edited_message(bot, first)) is True
    assert asyncio.run(MaxwellBot._refresh_edited_message(bot, second)) is True

    assert calls == ["first"]
    assert "second" in bot.memory.rows[-1][1]["content"]
    assert len(bot._media_context["22"]) == 1


def test_text_edit_notifies_inflight_turn_without_dispatching_reply():
    bot = _bot()
    before = _message(99, "before")
    after = _message(99, "after")
    state = MaxwellBot._begin_inflight_context(bot, before, before.content)
    state["media"] = [
        {
            "b64": "aW1hZ2U=",
            "mime_type": "image/png",
            "filename": "current.png",
            "is_image": True,
            "message_id": 99,
        }
    ]
    bot._message_update_state["99"] = (
        MaxwellBot._message_update_fingerprint(before),
        MaxwellBot._message_media_fingerprint(before),
        0.0,
    )
    bot._extract_context_media = lambda _message: None

    async def no_media(_message):
        raise AssertionError("text-only edit should not download media")

    bot._extract_context_media = no_media
    assert asyncio.run(
        MaxwellBot._refresh_edited_message(bot, after, before=before)
    ) is True
    assert state["latest_content"] == "after"
    assert state["latest_media"] == state["media"]
    assert state["version"] == 1


def test_raw_partial_update_merges_with_cached_message():
    bot = _bot()
    cached = _message(101, "keep this text")
    payload = SimpleNamespace(
        cached_message=cached,
        message_id=101,
        channel_id=22,
        data={
            "id": "101",
            "channel_id": "22",
            "embeds": [
                {
                    "title": "late preview",
                    "description": "unfurled",
                    "url": "https://example.com/post",
                }
            ],
        },
    )

    merged = asyncio.run(MaxwellBot._message_from_raw_update(bot, payload))

    assert merged.content == "keep this text"
    assert merged.embeds[0].title == "late preview"
    assert merged.author.id == 11
    assert merged.author.bot is False


def test_raw_unfurl_keeps_reply_and_the_live_channel():
    """A YouTube preview must not swap the DM message for a dead snapshot.

    Discord sends MESSAGE_UPDATE with just the new embed. get_channel() often
    misses that DM. The turn still has to reply() and channel.send().
    """
    bot = _bot()
    sent = []

    class Channel:
        id = 22
        guild = None
        name = "dm"

        async def send(self, content=None, file=None, **kwargs):
            sent.append(("send", file))
            return SimpleNamespace(id=5, attachments=[])

    class Live:
        id = 303
        content = "Grab this thumbnail https://youtu.be/tR0wNrPp2mM"
        author = SimpleNamespace(id=11, display_name="h3", bot=False)
        channel = Channel()
        guild = None

        async def reply(self, content=None, file=None, **kwargs):
            sent.append(("reply", file))
            return SimpleNamespace(id=6, attachments=[])

    live = Live()
    state = MaxwellBot._begin_inflight_context(bot, live, live.content)
    # A cache miss must not replace the DM channel that can already send.
    bot.get_channel = lambda *_args, **_kwargs: SimpleNamespace(id=22)
    payload = SimpleNamespace(
        cached_message=None,
        message_id=303,
        channel_id=22,
        data={
            "id": "303",
            "channel_id": "22",
            "embeds": [
                {
                    "title": "video",
                    "url": "https://youtu.be/tR0wNrPp2mM",
                    "thumbnail": {
                        "url": "https://i.ytimg.com/vi/tR0wNrPp2mM/hqdefault.jpg"
                    },
                }
            ],
        },
    )

    merged = asyncio.run(MaxwellBot._message_from_raw_update(bot, payload))

    assert merged.content == live.content
    assert merged.embeds[0].title == "video"
    assert merged.author.id == 11
    assert callable(merged.reply)
    assert callable(merged.channel.send)
    asyncio.run(merged.reply(file=b"thumb"))
    assert sent == [("reply", b"thumb")]

    assert asyncio.run(MaxwellBot._refresh_edited_message(bot, merged)) is True
    assert state["latest_message"].content == live.content
    assert callable(state["message"].reply)
    asyncio.run(state["message"].reply(file=b"again"))
    assert sent[-1] == ("reply", b"again")


def test_raw_author_without_bot_flag_is_human():
    bot = _bot()
    payload = SimpleNamespace(
        cached_message=None,
        message_id=202,
        channel_id=22,
        data={
            "id": "202",
            "channel_id": "22",
            "content": "hello",
            "author": {"id": "11", "username": "alice"},
        },
    )
    merged = asyncio.run(MaxwellBot._message_from_raw_update(bot, payload))
    assert merged.author.bot is False
    assert merged.author.display_name == "alice"


def test_raw_update_preserves_creation_time_and_decodes_discord_reference():
    bot = _bot()
    cached = _message(404, "before")
    parent = _message(403, "parent")
    cached.reference = SimpleNamespace(message_id=403, resolved=parent)
    payload = SimpleNamespace(
        cached_message=cached, message_id=404, channel_id=22,
        data={
            "content": "after",
            "mentions": [{"id": "1", "username": "Maxwell", "bot": True}],
            "mention_roles": ["99"],
            "message_reference": {"message_id": "403", "channel_id": "22"},
        },
    )
    merged = asyncio.run(bot._message_from_raw_update(payload))
    assert merged.created_at == cached.created_at
    assert merged.type == cached.type
    assert merged.mentions[0].id == 1
    assert merged.raw_role_mentions == [99]
    assert bot._reply_parent(merged) is parent
    assert bot._message_memory_item(merged)["timestamp"] == cached.created_at.isoformat()


def test_uncached_raw_update_uses_discord_creation_timestamp():
    bot = _bot()
    payload = SimpleNamespace(
        cached_message=None, message_id=405, channel_id=22,
        data={
            "author": {"id": "11", "username": "Alice"},
            "timestamp": "2026-08-23T04:05:06+00:00",
        },
    )
    merged = asyncio.run(bot._message_from_raw_update(payload))
    assert merged.created_at == datetime(2026, 8, 23, 4, 5, 6, tzinfo=timezone.utc)


def test_embed_text_edit_refreshes_inflight_summary_without_binary_download():
    from unittest.mock import AsyncMock

    bot = _bot()
    before = _message(500, "look", embeds=[SimpleNamespace(description="old description")])
    after = _message(500, "look", embeds=[SimpleNamespace(description="new description")])
    state = bot._begin_inflight_context(before, before.content)
    state["media"] = [{
        "is_text": True, "source": "embed", "message_id": 500,
        "text": "old description", "mime_type": "text/plain",
    }]
    bot._message_update_state["500"] = (
        bot._message_update_fingerprint(before),
        bot._message_media_fingerprint(before), 0.0,
    )
    bot._extract_context_media = AsyncMock()
    asyncio.run(bot._refresh_edited_message(after))
    bot._extract_context_media.assert_not_awaited()
    summary = bot._format_media_summary(state["latest_media"], [])
    assert "new description" in summary
    assert "old description" not in summary


def test_inflight_edit_clears_stale_pointer_hydration():
    bot = _bot()
    before = _message(600, "^")
    before.hydrated_context = "obsolete private target"
    before.hydrated_target = {"message_id": "599", "content": "obsolete private target"}
    after = _message(600, "unrelated explicit request")
    after.hydrated_context = before.hydrated_context
    after.hydrated_target = before.hydrated_target
    state = bot._begin_inflight_context(before, before.content)
    state.update(version=1, latest_message=after, latest_media=[])

    async def build(message, content, **_kwargs):
        assert not getattr(message, "hydrated_context", None)
        assert getattr(message, "hydrated_target", None) is None
        return [{"role": "user", "content": content}]

    bot._build_messages = build
    result = asyncio.run(bot._apply_inflight_refresh(
        state, before, before.content, [], [], "", []
    ))
    assert result[-1] == [{"role": "user", "content": after.content}]


def test_raw_edited_mentions_dispatch_by_identity_not_user_object_equality():
    from unittest.mock import AsyncMock

    bot = _bot()
    bot._connection = SimpleNamespace(user=SimpleNamespace(id=1))
    bot._request_journal = SimpleNamespace()
    bot._request_state = AsyncMock(return_value=None)
    bot.on_message = AsyncMock()
    before = _message(700, "original")
    payload = SimpleNamespace(
        cached_message=before, message_id=700, channel_id=22,
        data={
            "content": "<@1> question",
            "mentions": [{"id": "1", "username": "Maxwell", "bot": True}],
        },
    )
    after = asyncio.run(bot._message_from_raw_update(payload))
    asyncio.run(bot._maybe_reply_to_edited_mention(before, after))
    bot.on_message.assert_awaited_once_with(after)


def test_removing_last_attachment_clears_inflight_pixels():
    from unittest.mock import AsyncMock

    bot = _bot()
    before = _message(800, "look")
    before.attachments = [SimpleNamespace(
        id=1, filename="old.png", url="https://example.com/old.png",
        content_type="image/png", size=3,
    )]
    after = _message(800, "look")
    state = bot._begin_inflight_context(before, before.content)
    state["media"] = [{
        "b64": "b2xk", "mime_type": "image/png", "is_image": True,
        "filename": "old.png", "message_id": 800,
    }]
    bot._message_update_state["800"] = (
        bot._message_update_fingerprint(before),
        bot._message_media_fingerprint(before), 0.0,
    )
    bot._extract_context_media = AsyncMock(return_value=[])
    asyncio.run(bot._refresh_edited_message(after))
    assert state["media"] == []
    assert state["latest_media"] == []


def test_update_during_hydration_is_not_marked_consumed(monkeypatch):
    from unittest.mock import AsyncMock

    bot = _bot()
    before = _message(900, "before")
    first = _message(900, "first edit")
    newest = _message(900, "newest edit")
    state = bot._begin_inflight_context(before, before.content)
    state.update(version=1, latest_message=first, latest_media=[])

    async def hydrate(_bot, message):
        if message is first:
            state.update(version=2, latest_message=newest, latest_media=[])

    monkeypatch.setattr("bot_parts.message_updates.hydrate_turn_context", hydrate)
    bot._build_messages = AsyncMock(return_value=[])
    result = asyncio.run(bot._apply_inflight_refresh(
        state, before, before.content, [], [], "", []
    ))
    assert result[0] is newest
    assert result[1] == newest.content
    assert state["seen_version"] == 2


def test_unfetchable_late_embed_wait_propagates_cancellation(monkeypatch):
    from bot_parts import message_updates
    from unittest.mock import AsyncMock
    import pytest

    async def scenario():
        bot = _bot()
        message = _message(901, "https://example.test/image.png")
        entered = asyncio.Event()

        async def wait_for_embed(_seconds):
            entered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(
            message_updates, "asyncio", SimpleNamespace(sleep=wait_for_embed)
        )
        continued = AsyncMock()

        async def turn():
            await bot._wait_for_late_embeds(message, message.content)
            await continued()

        task = asyncio.create_task(turn())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        continued.assert_not_awaited()

    asyncio.run(scenario())


def test_raw_update_does_not_copy_an_inflight_reply_parent():
    bot = _bot()

    class PrivateChannel:
        id = 22

        async def send(self, content=None, **kwargs):
            return None

    turn = _message(500, "PRIVATE_PROMPT")
    turn.channel = PrivateChannel()

    async def private_reply(content=None, **kwargs):
        return None

    turn.reply = private_reply
    state = MaxwellBot._begin_inflight_context(bot, turn, turn.content)
    state["message_ids"].add("99")

    async def fail_fetch(_mid):
        raise RuntimeError("message cache miss")

    bot.get_channel = lambda *_args, **_kwargs: SimpleNamespace(
        id=22, guild=None, name="room", fetch_message=fail_fetch,
    )
    payload = SimpleNamespace(
        cached_message=None,
        message_id=99,
        channel_id=22,
        data={"id": "99", "channel_id": "22", "embeds": [{"title": "preview"}]},
    )
    merged = asyncio.run(MaxwellBot._message_from_raw_update(bot, payload))
    assert merged.content != "PRIVATE_PROMPT"
    assert getattr(merged.author, "id", None) != 11
    assert not callable(getattr(merged, "reply", None))
    assert not callable(getattr(merged.channel, "send", None))
    assert merged.channel is not turn.channel


