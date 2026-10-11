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
    assert host._dispatch_reply(message, "this", directed=True) == "dropped"


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
