"""Reactions are context on the message. They never start a live reply."""

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot import MaxwellBot
from utils import format_reactions_annotation

BOT_PY = Path(__file__).resolve().parent.parent / "bot.py"


@pytest.fixture(autouse=True)
def _reaction_clock(monkeypatch):
    monkeypatch.setattr("bot_parts.reactions.time", SimpleNamespace(time=lambda: 123.0))


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
        _user_preferences=SimpleNamespace(
            get=lambda _uid: {"defaults": {"tracking": "on"}},
            content_allowed=lambda _uid, _started_at=None: True,
        ),
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
        {"emoji": "😂", "user_id": "99", "user_name": "alice", "privacy_started_at": 123.0}
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
        {"emoji": "😂", "user_id": "2", "user_name": "bob", "privacy_started_at": 123.0}
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


@pytest.mark.parametrize("policy", ["missing", "off", "unreadable", "disabled"])
def test_reaction_identity_never_enters_memory_without_consent(tmp_path, policy):
    from plugins.maxwell_extras.user_preferences import UserPreferenceStore

    bot = _reaction_bot()
    store = UserPreferenceStore(tmp_path / "preferences.json")
    if policy in {"off", "disabled"}:
        store.set_default("99", "tracking", "off" if policy == "off" else "on")
    if policy == "unreadable":
        store.path.write_text('{"users":')
    bot._user_preferences = None if policy == "missing" else store
    if policy == "disabled":
        bot._control["store_memory"] = False
    merge = AsyncMock()
    bot.memory = SimpleNamespace(merge_message_metadata=merge)
    message = SimpleNamespace(id=777, author=bot.user)
    reaction = SimpleNamespace(message=message, emoji="😂")
    user = SimpleNamespace(id=99, display_name="private-name", bot=False)

    asyncio.run(MaxwellBot.on_reaction_add(bot, reaction, user))

    assert bot._message_reactions == {}
    merge.assert_not_awaited()
    assert MaxwellBot._reactions_annotation_for(bot, message) == ""


def test_reaction_identity_is_kept_for_a_user_with_no_saved_choice(tmp_path):
    from plugins.maxwell_extras.user_preferences import UserPreferenceStore

    bot = _reaction_bot()
    bot._user_preferences = UserPreferenceStore(tmp_path / "preferences.json")
    message = SimpleNamespace(id=777, author=bot.user)
    reaction = SimpleNamespace(message=message, emoji="😂")
    user = SimpleNamespace(id=99, display_name="private-name", bot=False)

    asyncio.run(MaxwellBot.on_reaction_add(bot, reaction, user))

    assert "private-name" in MaxwellBot._reactions_annotation_for(bot, message)


def test_reaction_overlay_and_snapshot_recheck_consent_and_deletion_fence(tmp_path, monkeypatch):
    from plugins.maxwell_extras.user_preferences import UserPreferenceStore

    bot = _reaction_bot()
    store = UserPreferenceStore(tmp_path / "preferences.json")
    store.set_default("99", "tracking", "on")
    bot._user_preferences = store
    message = SimpleNamespace(id=777, author=bot.user)
    reaction = SimpleNamespace(message=message, emoji="😂")
    user = SimpleNamespace(id=99, display_name="private-name", bot=False)
    asyncio.run(MaxwellBot.on_reaction_add(bot, reaction, user))
    rows = list(bot._message_reactions["777"])
    assert "private-name" in MaxwellBot._reactions_annotation_for(bot, message)

    store.delete_personal("99")
    assert MaxwellBot._reactions_annotation_for(bot, message) == ""
    store.set_default("99", "tracking", "on")
    # Re-enabling tracking does not resurrect pre-deletion identities.
    assert MaxwellBot._reactions_annotation_for(bot, message) == ""
    bot._message_reactions.clear()
    assert MaxwellBot._reactions_annotation_for(
        bot, {"message_id": "777", "reactions": rows}
    ) == ""

    deleted_at = store._read()["users"]["99"]["deleted_at"]
    monkeypatch.setattr(
        "bot_parts.reactions.time", SimpleNamespace(time=lambda: deleted_at + 1)
    )
    merge = AsyncMock()
    bot.memory = SimpleNamespace(merge_message_metadata=merge)
    asyncio.run(MaxwellBot.on_reaction_add(bot, reaction, user))
    assert "private-name" in MaxwellBot._reactions_annotation_for(bot, message)
    assert merge.call_args.args[1]["reactions"][0]["privacy_started_at"] > deleted_at
