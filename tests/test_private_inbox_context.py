"""Operator mailbox context cannot leak to ordinary or public requests."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot import MaxwellBot, _current_inbound


def _message(uid, *, private=False):
    return SimpleNamespace(
        id=uid,
        author=SimpleNamespace(id=uid),
        guild=SimpleNamespace(id=77),
        channel=SimpleNamespace(id=22),
        user_install=private,
        response_visibility="private" if private else "public",
    )


@pytest.mark.parametrize("uid,private", [(1, False), (2, True), (2, False)])
def test_inbox_is_absent_from_public_or_nonoperator_prompts(uid, private):
    inbox = SimpleNamespace(
        load_items=AsyncMock(return_value=[]),
        planner_items=lambda *_a, **_k: [],
        render_planner=lambda *_a: "private mailbox secret",
    )
    owner = SimpleNamespace(inbox=inbox, _is_admin=lambda uid: uid == 1)

    async def run():
        parts = []
        token = _current_inbound.set(_message(uid, private=private))
        try:
            await MaxwellBot._append_inbox_dynamic(owner, parts)
        finally:
            _current_inbound.reset(token)
        assert parts == []
        inbox.load_items.assert_not_awaited()

    asyncio.run(run())


def test_concurrent_private_operator_turns_mark_only_their_own_notices():
    async def run():
        both_shown = asyncio.Event()
        marked = []
        shown_count = 0

        async def load():
            uid = _current_inbound.get().author.id
            return [{"id": f"notice-{uid}", "kind": "notice", "state": "unread"}]

        async def mark(iid, _state):
            marked.append((_current_inbound.get().author.id, iid))

        inbox = SimpleNamespace(
            load_items=load,
            mark=mark,
            planner_items=lambda items, **_kw: items,
            render_planner=lambda items: str(items),
        )
        owner = SimpleNamespace(inbox=inbox, _is_admin=lambda _uid: True)

        async def turn(uid):
            nonlocal shown_count
            token = _current_inbound.set(_message(uid, private=True))
            try:
                await MaxwellBot._append_inbox_dynamic(owner, [])
                shown_count += 1
                if shown_count == 2:
                    both_shown.set()
                await both_shown.wait()
                await MaxwellBot._mark_inbox_announced(owner)
            finally:
                _current_inbound.reset(token)

        await asyncio.gather(turn(1), turn(2))
        assert sorted(marked) == [(1, "notice-1"), (2, "notice-2")]

    asyncio.run(run())
