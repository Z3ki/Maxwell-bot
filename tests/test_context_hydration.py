"""Pointer tokens and Discord replies must carry their target into the prompt."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from bot import MaxwellBot
from context_hydration import (
    hydrate_turn_context,
    hydration_value,
    is_pointer_only,
    message_text_without_mentions,
)
from maxwell_core.prompts.conversation import ConversationPromptBuilder


def test_pointer_tokens_are_only_the_whole_message():
    assert is_pointer_only("^")
    assert is_pointer_only("^^")
    assert is_pointer_only("^ ^")
    assert is_pointer_only("this")
    assert is_pointer_only("THAT")
    assert is_pointer_only("this that")
    assert not is_pointer_only("this is cool")
    assert not is_pointer_only("hello")
    assert not is_pointer_only("")
    message = SimpleNamespace(content="<@123> ^^ <@&9>")
    assert is_pointer_only(message_text_without_mentions(message))


def _author(name, user_id, *, bot=False):
    return SimpleNamespace(id=user_id, display_name=name, name=name, bot=bot)


def test_pointer_hydrates_the_preceding_non_bot_message():
    human = SimpleNamespace(
        id=3,
        content="hello from above",
        author=_author("Ada", 8),
        attachments=[],
        embeds=[],
        stickers=[],
        mentions=[],
    )
    bot_line = SimpleNamespace(
        id=2,
        content="I already spoke",
        author=_author("Maxwell", 1, bot=True),
        attachments=[],
        embeds=[],
        stickers=[],
        mentions=[],
    )

    class Channel:
        id = 42

        def history(self, limit=15, before=None):
            async def gen():
                yield bot_line
                yield human

            return gen()

    message = SimpleNamespace(
        id=9,
        content="^",
        author=_author("Sam", 4),
        channel=Channel(),
        reference=None,
        attachments=[],
        embeds=[],
        stickers=[],
        mentions=[],
    )
    bot = SimpleNamespace(user=SimpleNamespace(id=1), _self_ids=lambda: {1})

    async def run():
        await hydrate_turn_context(bot, message)

    asyncio.run(run())
    assert hydration_value(bot, message, "hydration_failed", False) is False
    assert "hello from above" in message.hydrated_context
    assert message.hydrated_target_id == "3"


def test_discord_reference_beats_the_previous_channel_message():
    parent = SimpleNamespace(
        id=11,
        content="the replied message",
        author=_author("Bea", 6),
        attachments=[],
        embeds=[],
        stickers=[],
        mentions=[],
    )
    message = SimpleNamespace(
        id=12,
        content="^",
        author=_author("Sam", 4),
        channel=SimpleNamespace(id=1),
        reference=SimpleNamespace(message_id=11, resolved=parent),
        attachments=[],
        embeds=[],
        stickers=[],
        mentions=[],
    )
    bot = SimpleNamespace(user=SimpleNamespace(id=1))

    async def run():
        await hydrate_turn_context(bot, message)

    asyncio.run(run())
    assert "the replied message" in message.hydrated_context
    assert message.hydrated_target is parent


def test_pointer_with_media_is_not_an_unresolved_pointer():
    async def run(payload):
        message = SimpleNamespace(
            id=2,
            content="this",
            author=_author("Sam", 4),
            channel=SimpleNamespace(id="room"),
            reference=None,
            attachments=payload.get("attachments", []),
            embeds=payload.get("embeds", []),
            stickers=payload.get("stickers", []),
            mentions=[],
        )
        bot = SimpleNamespace(user=SimpleNamespace(id=1), memory=None)
        await hydrate_turn_context(bot, message)
        assert hydration_value(bot, message, "hydration_failed", False) is False

    asyncio.run(run({"attachments": [SimpleNamespace(filename="pic.png")]}))
    asyncio.run(run({"embeds": [SimpleNamespace(title="card")]}))
    asyncio.run(run({"stickers": [SimpleNamespace(name="wave")]}))


def test_unresolved_pointer_is_not_dispatched():
    message = SimpleNamespace(
        id=1,
        content="this",
        author=_author("Sam", 4),
        channel=SimpleNamespace(id="room"),
        reference=None,
        attachments=[],
        embeds=[],
        stickers=[],
        mentions=[],
    )
    bot = SimpleNamespace(user=SimpleNamespace(id=1), memory=None)

    async def run():
        await hydrate_turn_context(bot, message)

    asyncio.run(run())
    assert message.hydration_failed is True
    host = SimpleNamespace()
    host._dispatch_reply = MaxwellBot._dispatch_reply.__get__(host)
    assert asyncio.run(host._dispatch_reply(message, "this", directed=True)) == "dropped"


def test_live_prompt_includes_hydrated_context():
    host = SimpleNamespace(
        _recent_users={},
        user=SimpleNamespace(id=1),
        _control={"music_context_enabled": False},
        _is_bare_ping=lambda *_args, **_kwargs: False,
        _reply_parent_context_lines=lambda _message: [],
        _get_music_context=lambda _message: "",
    )
    hooks = SimpleNamespace(apply_prompt_budget=lambda _host, messages: messages)
    message = SimpleNamespace(
        id=4,
        content="^",
        author=_author("Sam", 4),
        channel=SimpleNamespace(id="room"),
        mentions=[],
        hydrated_context="Resolved reference target (Ada). hello from above",
        reference=None,
        attachments=[],
        embeds=[],
        stickers=[],
    )
    built = ConversationPromptBuilder(host, hooks)._append_live_request(
        message, "^", False, "", []
    )
    content = built[-1]["content"]
    assert "[RESPOND TO THIS]" in content
    assert "hello from above" in content


def test_pointer_uses_scoped_stored_history_when_discord_history_is_unavailable(tmp_path):
    from conversation_memory import ConversationMemoryManager

    async def scenario():
        memory = ConversationMemoryManager(str(tmp_path))
        memory.preferences.set_default("8", "tracking", "on")
        try:
            await memory.add_to_channel_memory("42", {
                "message_id": "3", "guild_id": "30", "author": "Ada", "author_id": "8",
                "content": "the earlier message in this channel",
            })
            await memory.add_to_channel_memory("42", {
                "message_id": "4", "guild_id": "31", "author_id": "9",
                "content": "private other guild must not hydrate",
            })
            message = SimpleNamespace(
                id=9, content="^", author=_author("Sam", 4),
                channel=SimpleNamespace(id=42), guild=SimpleNamespace(id=30), reference=None,
                attachments=[], embeds=[], stickers=[], mentions=[],
            )
            bot = SimpleNamespace(user=SimpleNamespace(id=1), memory=memory)
            await hydrate_turn_context(bot, message)
            assert message.hydrated_target_id == "3"
            assert "earlier message in this channel" in message.hydrated_context
            assert "private other guild" not in message.hydrated_context
        finally:
            await memory.flush()
    asyncio.run(scenario())
