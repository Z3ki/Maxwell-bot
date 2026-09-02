"""The bot-side wiring: watch windows adapt, and extraction stops keying off phrases."""

import asyncio
from types import SimpleNamespace

import discord

from bot import MaxwellBot
from concurrency_safety import KeyedLocks


class FakeDM(discord.DMChannel):
    def __init__(self, cid="dm1"):
        self.id = cid


def _bot(control=None, admins=()):
    bot = SimpleNamespace(
        _control={
            "conversation_watch_enabled": True,
            "conversation_watch_seconds": 180,
            "conversation_watch_debounce_seconds": 1.0,
            "cross_context_enabled": True,
            "cross_context_extract_enabled": True,
            **(control or {}),
        },
        _conversation_watch={},
        _watch_states={},
        _last_extract_at={},
        _extracted_authors=set(),
        _channel_locks=KeyedLocks(),
        _cooldowns={},
        _recent_users={},
        _typing_users={},
        _watch_debounce={},
        _active_requests={},
        _active_request_user={},
        _last_bot_reply={},
        _last_bot_send={},
        _current_progress_by_channel={},
        bot_name="Maxwell",
        user=SimpleNamespace(id=1, display_name="Maxwell", name="maxwell"),
    )
    for name in (
        "_watch_state",
        "_watch_address_signal",
        "_note_watch_message",
        "_note_watch_silence",
        "_arm_conversation_watch",
        "_conversation_watch_seconds",
        "_conversation_watch_enabled",
        "_conversation_watch_active",
        "_watch_debounce_seconds",
        "_should_extract_context",
        "_extract_threshold",
        "_note_extraction_ran",
        "_directly_addressed",
        "_soft_addressed",
        "_addressing_someone_else",
        "_replying_to_other",
        "_reply_meta_from_message",
        "_prune_per_channel_state",
    ):
        setattr(bot, name, getattr(MaxwellBot, name).__get__(bot))
    bot._is_admin = lambda uid: str(uid) in {str(a) for a in admins}
    return bot


def _msg(content="hello", *, channel="ch1", author_id=7, dm=False, mentions=None):
    chan = FakeDM(channel) if dm else SimpleNamespace(id=channel, name="general")
    return SimpleNamespace(
        content=content,
        channel=chan,
        guild=None if dm else SimpleNamespace(id="g1", name="Guild", me=None),
        author=SimpleNamespace(id=author_id, display_name="Ada", name="ada", bot=False),
        mentions=mentions or [],
        role_mentions=[],
        mention_everyone=False,
        attachments=[],
        embeds=[],
        stickers=[],
        reference=None,
        type=SimpleNamespace(name="default"),
        id=1,
    )


# --------------------------------------------------------------------------
# Adaptive watch window
# --------------------------------------------------------------------------


def test_a_room_that_ignores_him_stops_being_watched_so_long():
    bot = _bot()

    async def run():
        loop = asyncio.get_running_loop()
        msg = _msg()
        # He speaks, arming the room the normal way.
        await MaxwellBot._arm_watch_from_own_message.__get__(bot)(msg)
        generous = bot._conversation_watch["ch1"] - loop.time()

        # Four soft follow-ups he declines to answer.
        for _ in range(4):
            follow = _msg(content="side chatter")
            follow._watch_followup = True
            bot._note_watch_silence(follow)
        bot._arm_conversation_watch("ch1")
        stingy = bot._conversation_watch["ch1"] - loop.time()

        assert stingy < generous / 2
        assert stingy > 0  # still watched, just briefly

    asyncio.run(run())


def test_declining_a_direct_ping_is_not_held_against_the_room():
    """Not answering a ping is a different decision from being ignored."""
    bot = _bot()

    async def run():
        for _ in range(4):
            bot._note_watch_silence(_msg())  # no _watch_followup
        assert bot._watch_state("ch1").silent_streak == 0

    asyncio.run(run())


def test_speaking_again_restores_the_full_window():
    bot = _bot()

    async def run():
        loop = asyncio.get_running_loop()
        for _ in range(4):
            follow = _msg()
            follow._watch_followup = True
            bot._note_watch_silence(follow)
        await MaxwellBot._arm_watch_from_own_message.__get__(bot)(_msg())
        assert bot._watch_state("ch1").silent_streak == 0
        assert bot._conversation_watch["ch1"] - loop.time() > 180

    asyncio.run(run())


def test_the_debounce_follows_the_rooms_pace():
    bot = _bot()

    async def run():
        loop = asyncio.get_running_loop()
        state = bot._watch_state("fast")
        base = loop.time()
        for tick in range(6):
            state.observe_message(base + tick * 0.2)
        assert bot._watch_debounce_seconds("fast") < 1.0
        # An unmeasured room keeps the configured value.
        assert bot._watch_debounce_seconds("never-seen") == 1.0
        assert bot._watch_debounce_seconds() == 1.0

    asyncio.run(run())


def test_the_watch_registry_does_not_grow_without_bound():
    bot = _bot()

    async def run():
        for n in range(500):
            bot._watch_state(f"room-{n}")
        assert len(bot._watch_states) <= 300

    asyncio.run(run())


def test_reading_who_a_message_is_aimed_at():
    bot = _bot()
    alice = SimpleNamespace(id=99, display_name="Alice")

    async def run():
        plain = bot._watch_address_signal(_msg("lol"))
        assert plain.direct is False and plain.names_him is False
        assert plain.text_length == 3

        named = bot._watch_address_signal(_msg("maxwell you around?"))
        assert named.names_him is True
        assert named.is_question is True
        assert named.direct is False  # named is not pinged

        at_alice = bot._watch_address_signal(_msg("hey", mentions=[alice]))
        assert at_alice.mentions_other is True

        # Discord's `self.user in message.mentions` is an identity check, so
        # the ping has to carry the client's own user object.
        pinged = bot._watch_address_signal(_msg("hey", mentions=[bot.user]))
        assert pinged.direct is True

    asyncio.run(run())


# --------------------------------------------------------------------------
# Context extraction
# --------------------------------------------------------------------------


def test_reactions_no_longer_cost_a_watcher_call():
    bot = _bot()

    async def run():
        for chatter in ("lol", "ok", "EZE", "hahaha", "k"):
            assert bot._should_extract_context(_msg(chatter)) is False

    asyncio.run(run())


def test_a_fact_phrased_outside_the_old_trigger_list_is_caught():
    bot = _bot()

    async def run():
        assert (
            bot._should_extract_context(
                _msg("everyone just calls me Z, that's the name I go by")
            )
            is True
        )

    asyncio.run(run())


def test_extraction_can_be_switched_off_entirely():
    bot = _bot(control={"cross_context_extract_enabled": False})

    async def run():
        assert bot._should_extract_context(_msg("Ana owns the DNS for z3ki.dev")) is False

    asyncio.run(run())


def test_an_empty_message_with_no_media_is_skipped():
    bot = _bot()

    async def run():
        assert bot._should_extract_context(_msg("")) is False

    asyncio.run(run())


def test_one_room_cannot_monopolise_the_extractor():
    bot = _bot()

    async def run():
        marginal = _msg("I prefer dark mode")
        assert bot._should_extract_context(marginal) is True
        bot._note_extraction_ran(marginal)
        assert bot._should_extract_context(_msg("bro that's wild")) is False
        # Something genuinely specific still gets through right away.
        assert (
            bot._should_extract_context(
                _msg("the staging box is prod-2 and Ana owns https://z3ki.dev")
            )
            is True
        )

    asyncio.run(run())


def test_the_threshold_is_configurable():
    strict = _bot(control={"cross_context_extract_threshold": 0.95})
    loose = _bot(control={"cross_context_extract_threshold": 0.0})

    async def run():
        text = _msg("Ana owns the DNS for z3ki.dev")
        assert strict._should_extract_context(text) is False
        assert loose._should_extract_context(_msg("lol")) is True

    asyncio.run(run())


def test_a_garbage_threshold_falls_back_to_the_default():
    bot = _bot(control={"cross_context_extract_threshold": "nope"})
    assert bot._extract_threshold() == 0.25


# --------------------------------------------------------------------------
# Per-channel state
# --------------------------------------------------------------------------


def test_per_channel_state_is_trimmed_back_under_its_caps():
    bot = _bot()
    for n in range(3000):
        bot._cooldowns[f"u{n}"] = float(n)
    bot._typing_users = {"quiet": {}, "busy": {"u1": {}}}
    bot._active_requests = {"done": SimpleNamespace(done=lambda: True)}
    bot._active_request_user = {"done": "u1"}

    removed = bot._prune_per_channel_state()

    assert removed > 0
    assert len(bot._cooldowns) <= 2000
    assert "quiet" not in bot._typing_users
    assert "busy" in bot._typing_users
    assert bot._active_requests == {}
    assert bot._active_request_user == {}
