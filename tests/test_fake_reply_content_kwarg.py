"""Reactions are context on the message. They never start a live reply."""

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace

from bot import MaxwellBot
from utils import format_reactions_annotation

BOT_PY = Path(__file__).resolve().parent.parent / "bot.py"


def test_reaction_path_has_no_fake_reply():
    tree = ast.parse(BOT_PY.read_text())
    fakes = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "fake_reply"
    ]
    assert fakes == []


def test_format_reactions_annotation():
    assert format_reactions_annotation([]) == ""
    assert (
        format_reactions_annotation(
            [
                {"emoji": "😂", "user_name": "alice", "user_id": "1"},
                {"emoji": "😂", "user_name": "bob", "user_id": "2"},
                {"emoji": "👍", "user_name": "z3ki", "user_id": "3"},
            ]
        )
        == "[reactions: 😂 alice, bob; 👍 z3ki]"
    )
    assert (
        format_reactions_annotation([{"emoji": "🔥", "count": 3}])
        == "[reactions: 🔥×3]"
    )


def _reaction_bot():
    maxwell_user = SimpleNamespace(id=42, display_name="Maxwell", bot=True)
    bot = SimpleNamespace(
        user=maxwell_user,
        _load_control=lambda: None,
        _control={"bot_enabled": True, "reply_to_bots": True, "ignore_users": []},
        _blacklist=set(),
        _message_reactions={},
        _message_reactions_order=[],
        memory=None,
        _MAX_REACTION_MESSAGES=MaxwellBot._MAX_REACTION_MESSAGES,
        _MAX_REACTORS_PER_MESSAGE=MaxwellBot._MAX_REACTORS_PER_MESSAGE,
    )
    bot._remember_reaction_message = MaxwellBot._remember_reaction_message.__get__(bot)
    bot._record_message_reaction = MaxwellBot._record_message_reaction.__get__(bot)
    bot._reactions_annotation_for = MaxwellBot._reactions_annotation_for.__get__(bot)
    bot._persist_message_reactions = MaxwellBot._persist_message_reactions.__get__(bot)
    bot._note_reaction = MaxwellBot._note_reaction.__get__(bot)
    return bot


def test_reaction_on_maxwell_message_is_recorded_not_answered():
    calls = []
    maxwell_user = SimpleNamespace(id=42, display_name="Maxwell", bot=True)
    reacting_user = SimpleNamespace(
        id=99, display_name="alice", name="alice", bot=False
    )
    original = SimpleNamespace(
        id=777,
        author=maxwell_user,
        channel=SimpleNamespace(id=123),
        guild=SimpleNamespace(id=9),
    )
    reaction = SimpleNamespace(message=original, emoji="😂")
    bot = _reaction_bot()
    bot.user = maxwell_user

    async def handle_message(message, content):
        calls.append((message, content))

    bot._handle_message = handle_message

    asyncio.run(MaxwellBot.on_reaction_add(bot, reaction, reacting_user))

    assert calls == []
    assert bot._message_reactions["777"] == [
        {"emoji": "😂", "user_id": "99", "user_name": "alice"}
    ]
    assert (
        MaxwellBot._reactions_annotation_for(bot, original)
        == "[reactions: 😂 alice]"
    )


def test_second_person_same_emoji_is_kept():
    bot = _reaction_bot()
    message = SimpleNamespace(id=5, author=bot.user)
    alice = SimpleNamespace(id=1, display_name="alice", bot=False)
    bob = SimpleNamespace(id=2, display_name="bob", bot=False)

    async def run():
        await MaxwellBot.on_reaction_add(
            bot, SimpleNamespace(message=message, emoji="😂"), alice
        )
        await MaxwellBot.on_reaction_add(
            bot, SimpleNamespace(message=message, emoji="😂"), bob
        )
        await MaxwellBot.on_reaction_remove(
            bot, SimpleNamespace(message=message, emoji="😂"), alice
        )

    asyncio.run(run())
    assert bot._message_reactions["5"] == [
        {"emoji": "😂", "user_id": "2", "user_name": "bob"}
    ]


def test_reply_parent_includes_reactions():
    bot = _reaction_bot()
    bot._recent_users = {}
    parent = SimpleNamespace(
        id=9,
        content="said hi",
        author=SimpleNamespace(id=42, display_name="Maxwell"),
        mentions=[],
        channel_mentions=[],
        role_mentions=[],
        guild=None,
        created_at=None,
        reactions=[],
    )
    bot._message_reactions["9"] = [
        {"emoji": "👍", "user_id": "1", "user_name": "z3ki"}
    ]
    rendered = MaxwellBot._render_reply_parent(bot, SimpleNamespace(channel=None), parent)
    assert "said hi" in rendered
    assert "[reactions: 👍 z3ki]" in rendered
