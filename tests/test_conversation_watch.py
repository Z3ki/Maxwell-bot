"""After a real exchange, the whole room stays on watch without another @."""

import asyncio
from types import SimpleNamespace

from autonomy import _reply_relation_bit
from bot import MaxwellBot


def _bot(*, watch_seconds=120, debounce_seconds=0.05):
    bot = SimpleNamespace(
        _control={
            "conversation_watch_seconds": watch_seconds,
            "conversation_watch_debounce_seconds": debounce_seconds,
        },
        _conversation_watch={},
        _watch_debounce={},
        _channel_locks={},
        user=SimpleNamespace(id=1382894657624866889),
    )
    bot._conversation_watch_seconds = MaxwellBot._conversation_watch_seconds.__get__(
        bot
    )
    bot._arm_conversation_watch = MaxwellBot._arm_conversation_watch.__get__(bot)
    bot._conversation_watch_active = MaxwellBot._conversation_watch_active.__get__(bot)
    bot._conversation_watch_prompt = MaxwellBot._conversation_watch_prompt.__get__(bot)
    bot._message_addresses_self = MaxwellBot._message_addresses_self.__get__(bot)
    bot._directly_addressed = MaxwellBot._directly_addressed.__get__(bot)
    bot._soft_addressed = MaxwellBot._soft_addressed.__get__(bot)
    bot._reply_meta_from_message = MaxwellBot._reply_meta_from_message.__get__(bot)
    bot._replying_to_other = MaxwellBot._replying_to_other.__get__(bot)
    bot._WATCH_ADDRESS_RE = MaxwellBot._WATCH_ADDRESS_RE
    bot._WATCH_ABOUT_RE = MaxwellBot._WATCH_ABOUT_RE
    bot._addressing_someone_else = MaxwellBot._addressing_someone_else.__get__(bot)
    bot._watch_followup_is_directed = MaxwellBot._watch_followup_is_directed.__get__(
        bot
    )
    bot._should_live_reply = MaxwellBot._should_live_reply.__get__(bot)
    bot._arm_watch_from_own_message = MaxwellBot._arm_watch_from_own_message.__get__(
        bot
    )
    bot._watch_debounce_seconds = MaxwellBot._watch_debounce_seconds.__get__(bot)
    bot._cancel_watch_debounce = MaxwellBot._cancel_watch_debounce.__get__(bot)
    bot._queue_watch_reply = MaxwellBot._queue_watch_reply.__get__(bot)
    bot._touch_watch_debounce = MaxwellBot._touch_watch_debounce.__get__(bot)
    bot._flush_watch_reply = MaxwellBot._flush_watch_reply.__get__(bot)
    bot._maybe_live_reply = MaxwellBot._maybe_live_reply.__get__(bot)
    bot._get_channel_lock = MaxwellBot._get_channel_lock.__get__(bot)
    bot._track_task = lambda task: task
    return bot


def _plain_followup(
    *,
    content="wow fancy i am doing fine myself",
    author_id=1471821513824014480,
    display_name="Z3ki",
    reference=None,
):
    return SimpleNamespace(
        channel=SimpleNamespace(id=1506001126426808511),
        author=SimpleNamespace(id=author_id, bot=False, display_name=display_name),
        mentions=[],
        mention_everyone=False,
        role_mentions=[],
        content=content,
        guild=SimpleNamespace(me=None, get_member=lambda _uid: None),
        reference=reference,
    )


def test_ambient_watch_followup_is_ignored():
    bot = _bot()
    msg = _plain_followup()
    assert MaxwellBot._directly_addressed(bot, msg) is False
    assert MaxwellBot._should_live_reply(bot, msg) is False

    async def run():
        MaxwellBot._arm_conversation_watch(bot, msg.channel.id)
        assert MaxwellBot._should_live_reply(bot, msg) is False
        asked = _plain_followup(content="wanna talk about something?")
        assert MaxwellBot._should_live_reply(bot, asked) is False
        named = _plain_followup(content="maxwell say hi")
        assert MaxwellBot._should_live_reply(bot, named) is True

    asyncio.run(run())


def test_watch_is_the_room_not_one_user():
    bot = _bot()
    other = _plain_followup(
        content="maxwell you still there?",
        author_id=99,
        display_name="Alice",
    )

    async def run():
        MaxwellBot._arm_conversation_watch(bot, other.channel.id)
        assert MaxwellBot._should_live_reply(bot, other) is True
        ambient = _plain_followup(content="lol", author_id=99, display_name="Alice")
        assert MaxwellBot._should_live_reply(bot, ambient) is False

    asyncio.run(run())


def test_everyone_mention_does_not_force_a_reply():
    bot = _bot()
    msg = _plain_followup(content="hello room")
    msg.mention_everyone = True
    assert MaxwellBot._soft_addressed(bot, msg) is True
    assert MaxwellBot._directly_addressed(bot, msg) is False
    assert MaxwellBot._should_live_reply(bot, msg) is False
    msg.content = "maxwell come here"
    assert MaxwellBot._should_live_reply(bot, msg) is True


def test_watch_expires():
    bot = _bot()

    async def run():
        MaxwellBot._arm_conversation_watch(bot, "ch")
        assert MaxwellBot._conversation_watch_active(bot, "ch") is True
        bot._conversation_watch["ch"] = asyncio.get_running_loop().time() - 1
        assert MaxwellBot._conversation_watch_active(bot, "ch") is False

    asyncio.run(run())


def test_watch_disabled_when_seconds_zero():
    bot = _bot(watch_seconds=0)

    async def run():
        MaxwellBot._arm_conversation_watch(bot, "ch")
        assert bot._conversation_watch == {}
        assert MaxwellBot._conversation_watch_active(bot, "ch") is False

    asyncio.run(run())


def test_own_message_arms_the_channel():
    bot = _bot()
    own = SimpleNamespace(
        channel=SimpleNamespace(id=1506001126426808511),
        reference=None,
    )

    async def run():
        await MaxwellBot._arm_watch_from_own_message(bot, own)
        assert MaxwellBot._conversation_watch_active(bot, own.channel.id) is True
        other = _plain_followup(
            content="maxwell you still there?",
            author_id=99,
            display_name="Alice",
        )
        assert MaxwellBot._should_live_reply(bot, other) is True

    asyncio.run(run())


def test_trailing_vocative_is_a_watch_reply():
    bot = _bot()

    async def run():
        MaxwellBot._arm_conversation_watch(bot, 1506001126426808511)
        him = _plain_followup(content="dont be like him max")
        yell = _plain_followup(content="SAY SOMETHING MAXWELl")
        ambient = _plain_followup(content="EZE")
        assert MaxwellBot._watch_followup_is_directed(bot, him) is True
        assert MaxwellBot._should_live_reply(bot, him) is True
        assert MaxwellBot._watch_followup_is_directed(bot, yell) is True
        assert MaxwellBot._should_live_reply(bot, yell) is True
        assert MaxwellBot._should_live_reply(bot, ambient) is False

    asyncio.run(run())


def test_directed_watch_line_bypasses_reply_cooldown():
    """Per-user cooldown must not eat a vocative after a watch miss."""
    bot = _bot()
    directed = _plain_followup(content="dont be like him max")
    yell = _plain_followup(content="SAY SOMETHING MAXWELl")
    ambient = _plain_followup(content="EZE")
    assert (
        MaxwellBot._directly_addressed(bot, directed)
        or MaxwellBot._watch_followup_is_directed(bot, directed)
    ) is True
    assert (
        MaxwellBot._directly_addressed(bot, yell)
        or MaxwellBot._watch_followup_is_directed(bot, yell)
    ) is True
    assert (
        MaxwellBot._directly_addressed(bot, ambient)
        or MaxwellBot._watch_followup_is_directed(bot, ambient)
    ) is False


def test_talking_about_him_to_someone_else_is_not_a_watch_reply():
    bot = _bot()
    msg = _plain_followup(
        content="that's why they don't have access to maxwell"
    )

    async def run():
        MaxwellBot._arm_conversation_watch(bot, msg.channel.id)
        assert MaxwellBot._watch_followup_is_directed(bot, msg) is False
        assert MaxwellBot._should_live_reply(bot, msg) is False
        about = _plain_followup(content="maxwell is down right now")
        assert MaxwellBot._should_live_reply(bot, about) is False
        to_him = _plain_followup(content="hey maxwell")
        assert MaxwellBot._should_live_reply(bot, to_him) is True

    asyncio.run(run())


def test_pinging_someone_else_is_not_a_watch_reply():
    bot = _bot()
    alice = SimpleNamespace(id=99, display_name="Alice")
    msg = _plain_followup(content="maxwell is why you don't have access")
    msg.mentions = [alice]

    async def run():
        MaxwellBot._arm_conversation_watch(bot, msg.channel.id)
        assert MaxwellBot._addressing_someone_else(bot, msg) is True
        assert MaxwellBot._should_live_reply(bot, msg) is False

    asyncio.run(run())


def test_mention_still_counts_as_addressed():
    bot = _bot()
    msg = _plain_followup(content="ok")
    msg.mentions = [bot.user]
    assert MaxwellBot._directly_addressed(bot, msg) is True
    assert MaxwellBot._should_live_reply(bot, msg) is True


def test_reply_to_someone_else_is_not_a_live_reply():
    bot = _bot()
    other = SimpleNamespace(
        id=99,
        author=SimpleNamespace(id=99, display_name="Alice"),
        content="i already said that",
    )
    msg = _plain_followup(
        content="wanna talk about something?",
        reference=SimpleNamespace(resolved=other),
    )

    async def run():
        MaxwellBot._arm_conversation_watch(bot, msg.channel.id)
        assert MaxwellBot._replying_to_other(bot, msg) is True
        assert MaxwellBot._should_live_reply(bot, msg) is False
        msg.mentions = [bot.user]
        assert MaxwellBot._directly_addressed(bot, msg) is True
        assert MaxwellBot._should_live_reply(bot, msg) is True

    asyncio.run(run())


def test_watch_prompt_tells_him_the_room_is_on_watch():
    bot = _bot()
    msg = _plain_followup()

    async def run():
        assert MaxwellBot._conversation_watch_prompt(bot, msg, msg.channel.id) == []
        MaxwellBot._arm_conversation_watch(bot, msg.channel.id)
        lines = MaxwellBot._conversation_watch_prompt(bot, msg, msg.channel.id)
        assert any("Conversation watch is on in this room" in line for line in lines)
        assert any("Keep talking here" in line for line in lines)
        assert all("Soft follow-up" not in line for line in lines)
        msg._watch_followup = True
        lines = MaxwellBot._conversation_watch_prompt(bot, msg, msg.channel.id)
        assert any("Conversation watch is on in this room" in line for line in lines)
        assert any("Soft follow-up" in line for line in lines)
        assert any("Answer it" in line for line in lines)
        assert all("Speak only if" not in line for line in lines)
        assert all("do not jump in" not in line for line in lines)

    asyncio.run(run())


def test_watch_debounce_collapses_a_burst_into_one_reply():
    bot = _bot()
    handled = []

    async def handle(message, content=None):
        handled.append(getattr(message, "content", ""))

    bot._handle_message = handle

    async def run():
        MaxwellBot._arm_conversation_watch(bot, 1506001126426808511)
        first = _plain_followup(content="hey maxwell")
        second = _plain_followup(content="maxwell say hi")
        await MaxwellBot._maybe_live_reply(bot, first, first.content)
        await MaxwellBot._maybe_live_reply(bot, second, second.content)
        assert handled == []
        await asyncio.sleep(0.25)
        assert handled == ["maxwell say hi"]

    asyncio.run(run())


def test_watch_debounce_skips_if_they_start_talking_to_someone_else():
    bot = _bot()
    handled = []

    async def handle(message, content=None):
        handled.append(getattr(message, "content", ""))

    bot._handle_message = handle

    async def run():
        MaxwellBot._arm_conversation_watch(bot, 1506001126426808511)
        ping = _plain_followup(content="hey maxwell")
        aside = _plain_followup(
            content="that's why they don't have access to maxwell"
        )
        await MaxwellBot._maybe_live_reply(bot, ping, ping.content)
        await MaxwellBot._maybe_live_reply(bot, aside, aside.content)
        await asyncio.sleep(0.25)
        assert handled == []

    asyncio.run(run())


def test_hard_ping_does_not_wait_for_watch_debounce():
    bot = _bot()
    handled = []

    async def handle(message, content=None):
        handled.append("now")

    bot._handle_message = handle

    async def run():
        msg = _plain_followup(content="ok")
        msg.mentions = [bot.user]
        await MaxwellBot._maybe_live_reply(bot, msg, msg.content)
        assert handled == ["now"]

    asyncio.run(run())


def test_reply_relation_includes_quote():
    bit = _reply_relation_bit(
        {
            "reply_to_author": "Alice",
            "reply_to_author_id": "99",
            "reply_to_self": False,
            "reply_to_content": "  i already   said that  ",
        }
    )
    assert bit == 'reply_to=Alice(99) "i already said that"'
    self_bit = _reply_relation_bit(
        {
            "reply_to_author": "Maxwell",
            "reply_to_author_id": "1",
            "reply_to_self": True,
            "reply_to_content": "hey",
        }
    )
    assert self_bit == 'reply_to=you/Maxwell(1) "hey"'
