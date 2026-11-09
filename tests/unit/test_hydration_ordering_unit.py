"""Hydration must only ever resolve a pointer to an *earlier* message, and an
empty reply must not silently borrow an unrelated preceding message when its
own reference cannot be loaded.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from context_hydration import (
    hydrate_turn_context,
    hydration_value,
    requester_can_read_history,
)


def _author(name, user_id, *, bot=False):
    return SimpleNamespace(id=user_id, display_name=name, name=name, bot=bot)


def _msg(mid, content, author_id=8, *, bot=False):
    return SimpleNamespace(
        id=mid,
        content=content,
        author=_author("x", author_id, bot=bot),
        attachments=[],
        embeds=[],
        stickers=[],
        mentions=[],
    )


class _Channel:
    def __init__(self, items):
        self.id = 42
        self._items = items

    def history(self, limit=15, before=None):
        async def gen():
            for item in self._items:
                yield item

        return gen()


def _bot(**kwargs):
    return SimpleNamespace(user=SimpleNamespace(id=1), memory=None, **kwargs)


def test_empty_reply_with_unresolvable_reference_does_not_borrow_preceding_message():
    unrelated = _msg(7, "an older unrelated line")
    message = SimpleNamespace(
        id=11,
        content="",
        author=_author("Sam", 4),
        channel=_Channel([unrelated]),
        reference=SimpleNamespace(message_id=10, resolved=None),
        attachments=[],
        embeds=[],
        stickers=[],
        mentions=[],
    )
    bot = _bot()
    asyncio.run(hydrate_turn_context(bot, message))
    ctx = hydration_value(bot, message, "hydrated_context", "")
    assert "an older unrelated line" not in (ctx or "")
    assert "could not be loaded" in (ctx or "")


def test_pointer_never_resolves_to_a_message_newer_than_itself():
    newer = _msg(10, "said after the pointer")
    live = _msg(9, "^", author_id=4)
    older = _msg(8, "said before the pointer")
    live.channel = _Channel([newer, live, older])
    bot = _bot()
    asyncio.run(hydrate_turn_context(bot, live))
    assert hydration_value(bot, live, "hydrated_target_id") == "8"
    assert "said before the pointer" in hydration_value(bot, live, "hydrated_context")


def test_pointer_still_resolves_to_the_immediately_previous_message():
    live = _msg(9, "^", author_id=4)
    older = _msg(8, "the one right before")
    live.channel = _Channel([live, older])
    bot = _bot()
    asyncio.run(hydrate_turn_context(bot, live))
    assert hydration_value(bot, live, "hydrated_target_id") == "8"


def test_pointer_reply_with_missing_target_does_not_fall_back_to_channel_history():
    live = _msg(11, "^", author_id=4)
    live.channel = _Channel([_msg(7, "UNRELATED_OLDER_MESSAGE")])
    live.reference = SimpleNamespace(message_id=10, resolved=None)
    bot = _bot()
    asyncio.run(hydrate_turn_context(bot, live))
    context = hydration_value(bot, live, "hydrated_context", "")
    assert "could not be loaded" in context
    assert "UNRELATED_OLDER_MESSAGE" not in context
    assert not hydration_value(bot, live, "hydrated_target_id")
    assert hydration_value(bot, live, "hydration_failed") is False


def test_rehydrating_an_edited_message_clears_object_and_slotted_cache_targets():
    class SlottedMessage:
        __slots__ = (
            "id",
            "content",
            "author",
            "channel",
            "reference",
            "attachments",
            "embeds",
            "stickers",
            "mentions",
        )

    for slotted in (False, True):
        live = _msg(9, "^", author_id=4)
        live.channel = _Channel([_msg(8, "OLD_TARGET_CONTENT")])
        live.reference = None
        if slotted:
            cached = SlottedMessage()
            for key, value in vars(live).items():
                setattr(cached, key, value)
            live = cached
        bot = _bot()
        asyncio.run(hydrate_turn_context(bot, live))
        assert hydration_value(bot, live, "hydrated_target_id") == "8"
        live.content = "A new independent request."
        asyncio.run(hydrate_turn_context(bot, live))
        assert hydration_value(bot, live, "hydrated_context") is None
        assert hydration_value(bot, live, "hydrated_target") is None
        assert hydration_value(bot, live, "hydrated_target_id") == ""
        assert hydration_value(bot, live, "hydration_failed") is False


def test_guild_pointer_hydration_requires_requester_view_and_history_permissions():
    for view, read in ((False, True), (True, False), (False, False)):
        accesses = []
        live = _msg(9, "^", author_id=4)
        live.guild = SimpleNamespace(id=99)

        class Channel(_Channel):
            def permissions_for(
                self, requester, author=live.author, view=view, read=read
            ):
                assert requester is author
                return SimpleNamespace(view_channel=view, read_message_history=read)

            def history(self, *args, seen=accesses, **kwargs):
                seen.append("discord")
                return super().history(*args, **kwargs)

        class Memory:
            async def get_channel_memory(self, *args, seen=accesses, **kwargs):
                seen.append("memory")
                return [{"message_id": "8", "content": "OLD_PRIVATE_MESSAGE"}]

        live.channel = Channel([_msg(8, "OLD_PRIVATE_MESSAGE")])
        bot = _bot()
        bot.memory = Memory()
        asyncio.run(hydrate_turn_context(bot, live))
        assert accesses == []
        assert not hydration_value(bot, live, "hydrated_target_id")
        assert "OLD_PRIVATE_MESSAGE" not in str(
            hydration_value(bot, live, "hydrated_context")
        )


def test_resolved_reply_target_is_not_hydrated_when_requester_lacks_history():
    live = _msg(9, "^", author_id=4)
    live.guild = SimpleNamespace(id=99)
    live.channel = SimpleNamespace(
        id=42,
        permissions_for=lambda _requester: SimpleNamespace(
            view_channel=True, read_message_history=False
        ),
    )
    live.reference = SimpleNamespace(message_id=8, resolved=_msg(8, "OLD_SECRET"))
    bot = _bot()
    asyncio.run(hydrate_turn_context(bot, live))
    assert not hydration_value(bot, live, "hydrated_target_id")
    assert "OLD_SECRET" not in hydration_value(bot, live, "hydrated_context")
    assert "could not be loaded" in hydration_value(bot, live, "hydrated_context")


def test_history_permission_lookup_fails_closed_and_allows_authorized_requester():
    live = _msg(9, "^", author_id=4)
    live.guild = SimpleNamespace(id=99)
    live.channel = SimpleNamespace(id=42)
    assert requester_can_read_history(live) is False

    def broken_permissions(_requester):
        raise RuntimeError("member permissions unavailable")

    live.channel.permissions_for = broken_permissions
    assert requester_can_read_history(live) is False
    seen = []

    def allowed_permissions(requester):
        seen.append(requester)
        return SimpleNamespace(view_channel=True, read_message_history=True)

    live.channel.permissions_for = allowed_permissions
    assert requester_can_read_history(live) is True
    assert seen == [live.author]
    live.guild = None
    live.channel = SimpleNamespace(id=42)
    assert requester_can_read_history(live) is True


def test_history_permissions_support_raw_and_wrapped_uncached_guild_interactions():
    for wrapped in (False, True):
        source = SimpleNamespace(
            id=42,
            permissions_for=lambda _requester: SimpleNamespace(
                view_channel=True, read_message_history=True
            ),
        )
        interaction = SimpleNamespace(
            channel=source,
            channel_id=42,
            guild=None,
            guild_id=99,
            user=_author("Sam", 4),
            permissions=SimpleNamespace(view_channel=True, read_message_history=False),
        )
        message = (
            SimpleNamespace(interaction=interaction, channel=source, guild=None)
            if wrapped
            else interaction
        )
        assert requester_can_read_history(message) is False
        interaction.permissions.read_message_history = True
        assert requester_can_read_history(message) is True
        other = SimpleNamespace(
            id=43,
            permissions_for=lambda _requester: SimpleNamespace(
                view_channel=True, read_message_history=False
            ),
        )
        assert requester_can_read_history(message, other) is False
        interaction.channel = None
        assert requester_can_read_history(message, source) is True
        interaction.permissions = None
        source.permissions_for = None
        assert requester_can_read_history(message, source) is False
