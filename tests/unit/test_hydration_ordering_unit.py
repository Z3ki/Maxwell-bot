"""Hydration must only ever resolve a pointer to an *earlier* message, and an
empty reply must not silently borrow an unrelated preceding message when its
own reference cannot be loaded.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from context_hydration import hydrate_turn_context, hydration_value


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
