"""After a real exchange, the whole room stays on watch without another @."""

import asyncio
from types import SimpleNamespace

from autonomy import _reply_relation_bit
from bot import MaxwellBot
from control_defaults import DEFAULT_CONTROL


def _bot(*, watch_seconds=180, debounce_seconds=0.05):
    bot = SimpleNamespace(
        _control={
            "conversation_watch_seconds": watch_seconds,
            "conversation_watch_debounce_seconds": debounce_seconds,
        },
        _conversation_watch={},
        _watch_debounce={},
        _channel_locks={},
        _typing_users={},
        _blacklist=set(),
        _replying_channels=set(),
        _recent_users={},
        _active_requests={},
        _active_request_user={},
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
    bot._content_without_self_mention = MaxwellBot._content_without_self_mention.__get__(
        bot
    )
    bot._is_bare_ping = MaxwellBot._is_bare_ping.__get__(bot)
    bot._soft_addressed = MaxwellBot._soft_addressed.__get__(bot)
    bot._reply_meta_from_message = MaxwellBot._reply_meta_from_message.__get__(bot)
    bot._replying_to_other = MaxwellBot._replying_to_other.__get__(bot)
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
    bot._channel_lock_timeout = MaxwellBot._channel_lock_timeout.__get__(bot)
    bot._watch_lock_timeout = MaxwellBot._watch_lock_timeout.__get__(bot)
    bot._requeue_after_lock_timeout = MaxwellBot._requeue_after_lock_timeout.__get__(
        bot
    )
    bot._should_interrupt_inflight = MaxwellBot._should_interrupt_inflight.__get__(
        bot
    )
    bot._track_task = lambda task: task
    bot._update_recent_users = MaxwellBot._update_recent_users.__get__(bot)
    bot._prune_typing = MaxwellBot._prune_typing.__get__(bot)
    bot._note_typing = MaxwellBot._note_typing.__get__(bot)
    bot._clear_typing = MaxwellBot._clear_typing.__get__(bot)
    bot._typing_in_channel = MaxwellBot._typing_in_channel.__get__(bot)
    bot._typing_channel_ids = MaxwellBot._typing_channel_ids.__get__(bot)
    bot._typing_prompt_lines = MaxwellBot._typing_prompt_lines.__get__(bot)
    bot._TYPING_TTL_SECONDS = MaxwellBot._TYPING_TTL_SECONDS
    bot._TYPING_WAIT_CAP_SECONDS = MaxwellBot._TYPING_WAIT_CAP_SECONDS
    bot._TYPING_WAIT_STEP_SECONDS = MaxwellBot._TYPING_WAIT_STEP_SECONDS
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


def test_watch_default_is_three_minutes():
    assert DEFAULT_CONTROL["conversation_watch_seconds"] == 180
    missing = SimpleNamespace(_control={})
    assert MaxwellBot._conversation_watch_seconds(missing) == 180.0
    garbage = SimpleNamespace(_control={"conversation_watch_seconds": "nope"})
    assert MaxwellBot._conversation_watch_seconds(garbage) == 180.0


def test_ambient_outside_watch_is_ignored():
    bot = _bot()
    msg = _plain_followup()
    assert MaxwellBot._directly_addressed(bot, msg) is False
    assert MaxwellBot._should_live_reply(bot, msg) is False
    asked = _plain_followup(content="wanna talk about something?")
    assert MaxwellBot._should_live_reply(bot, asked) is False
    named = _plain_followup(content="maxwell say hi")
    assert MaxwellBot._should_live_reply(bot, named) is False


def test_watch_shows_him_every_human_line():
    bot = _bot()
    msg = _plain_followup()

    async def run():
        MaxwellBot._arm_conversation_watch(bot, msg.channel.id)
        assert MaxwellBot._should_live_reply(bot, msg) is True
        asked = _plain_followup(content="wanna talk about something?")
        assert MaxwellBot._should_live_reply(bot, asked) is True
        named = _plain_followup(content="maxwell say hi")
        assert MaxwellBot._should_live_reply(bot, named) is True
        chatter = _plain_followup(content="lol")
        assert MaxwellBot._should_live_reply(bot, chatter) is True

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
        assert MaxwellBot._should_live_reply(bot, ambient) is True

    asyncio.run(run())


def test_everyone_mention_does_not_force_a_reply():
    bot = _bot()
    msg = _plain_followup(content="hello room")
    msg.mention_everyone = True
    assert MaxwellBot._soft_addressed(bot, msg) is True
    assert MaxwellBot._directly_addressed(bot, msg) is False
    assert MaxwellBot._should_live_reply(bot, msg) is False
    msg.content = "maxwell come here"
    assert MaxwellBot._should_live_reply(bot, msg) is False


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
            content="lol",
            author_id=99,
            display_name="Alice",
        )
        assert MaxwellBot._should_live_reply(bot, other) is True

    asyncio.run(run())


def test_watch_line_is_his_choice():
    bot = _bot()
    ambient = _plain_followup(content="EZE")
    assert MaxwellBot._should_live_reply(bot, ambient) is False

    async def run():
        MaxwellBot._arm_conversation_watch(bot, ambient.channel.id)
        assert MaxwellBot._should_live_reply(bot, ambient) is True
        assert MaxwellBot._watch_followup_is_directed(bot, ambient) is True

    asyncio.run(run())


def test_talking_about_him_is_still_his_choice():
    bot = _bot()
    msg = _plain_followup(content="that's why they don't have access to maxwell")

    async def run():
        MaxwellBot._arm_conversation_watch(bot, msg.channel.id)
        assert MaxwellBot._should_live_reply(bot, msg) is True
        about = _plain_followup(content="maxwell is down right now")
        assert MaxwellBot._should_live_reply(bot, about) is True

    asyncio.run(run())


def test_pinging_someone_else_is_still_his_choice():
    bot = _bot()
    alice = SimpleNamespace(id=99, display_name="Alice")
    msg = _plain_followup(content="maxwell is why you don't have access")
    msg.mentions = [alice]

    async def run():
        MaxwellBot._arm_conversation_watch(bot, msg.channel.id)
        assert MaxwellBot._addressing_someone_else(bot, msg) is True
        assert MaxwellBot._should_live_reply(bot, msg) is True

    asyncio.run(run())


def test_mention_still_counts_as_addressed():
    bot = _bot()
    msg = _plain_followup(content="ok")
    msg.mentions = [bot.user]
    assert MaxwellBot._directly_addressed(bot, msg) is True
    assert MaxwellBot._should_live_reply(bot, msg) is True


def test_reply_to_someone_else_is_his_choice_on_watch():
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
        assert MaxwellBot._should_live_reply(bot, msg) is False
        MaxwellBot._arm_conversation_watch(bot, msg.channel.id)
        assert MaxwellBot._replying_to_other(bot, msg) is True
        assert MaxwellBot._should_live_reply(bot, msg) is True
        msg.mentions = [bot.user]
        assert MaxwellBot._directly_addressed(bot, msg) is True
        assert MaxwellBot._should_live_reply(bot, msg) is True

    asyncio.run(run())


def test_watch_prompt_lets_him_decide():
    bot = _bot()
    msg = _plain_followup()

    async def run():
        assert MaxwellBot._conversation_watch_prompt(bot, msg, msg.channel.id) == []
        MaxwellBot._arm_conversation_watch(bot, msg.channel.id)
        lines = MaxwellBot._conversation_watch_prompt(bot, msg, msg.channel.id)
        assert any("Conversation watch is on in this room" in line for line in lines)
        assert any("can talk without an @" in line for line in lines)
        assert any("default to no_response" in line for line in lines)
        assert all("Soft follow-up" not in line for line in lines)
        msg._watch_followup = True
        lines = MaxwellBot._conversation_watch_prompt(bot, msg, msg.channel.id)
        assert any("Default is no_response" in line for line in lines)
        assert any("reply_to" in line for line in lines)
        assert all("Answer it" not in line for line in lines)
        assert all("speak if it's worth it" not in line for line in lines)

    asyncio.run(run())


def test_watch_prompt_says_channel_posts_are_room_chat():
    bot = _bot()
    msg = _plain_followup(content="lol")

    async def run():
        MaxwellBot._arm_conversation_watch(bot, msg.channel.id)
        lines = MaxwellBot._conversation_watch_prompt(bot, msg, msg.channel.id)
        text = " ".join(lines).lower()
        assert "posted to the channel" in text
        assert "not a ping" in text
        parent = SimpleNamespace(
            id=11,
            author=SimpleNamespace(id=99, display_name="Alice"),
            content="hello",
        )
        reply = _plain_followup(
            content="yeah",
            reference=SimpleNamespace(resolved=parent, message_id=11),
        )
        lines = MaxwellBot._conversation_watch_prompt(bot, reply, reply.channel.id)
        text = " ".join(lines).lower()
        assert "reply to someone else" in text
        ping = _plain_followup(content="hey")
        ping.mentions = [SimpleNamespace(id=99, display_name="Alice")]
        lines = MaxwellBot._conversation_watch_prompt(bot, ping, ping.channel.id)
        text = " ".join(lines).lower()
        assert "mentioned someone else" in text

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
        second = _plain_followup(content="lol")
        await MaxwellBot._maybe_live_reply(bot, first, first.content)
        await MaxwellBot._maybe_live_reply(bot, second, second.content)
        assert handled == []
        await asyncio.sleep(0.25)
        assert handled == ["lol"]

    asyncio.run(run())


def test_watch_debounce_still_shows_an_aside():
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
        assert handled == ["that's why they don't have access to maxwell"]

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


def test_channel_lock_fails_fast_under_load():
    bot = _bot()
    assert MaxwellBot._channel_lock_timeout(bot) == 15.0
    bot._control["channel_lock_timeout_seconds"] = 120
    assert MaxwellBot._channel_lock_timeout(bot) == 60.0
    bot._control["channel_lock_timeout_seconds"] = 1
    assert MaxwellBot._channel_lock_timeout(bot) == 3.0
    assert MaxwellBot._watch_lock_timeout(bot) == 90.0


def test_watch_chatter_does_not_interrupt_inflight():
    bot = _bot()
    fake = SimpleNamespace(done=lambda: False)

    async def run():
        chatter = _plain_followup(content="lol")
        cid = str(chatter.channel.id)
        bot._active_requests[cid] = fake
        bot._active_request_user[cid] = str(chatter.author.id)
        MaxwellBot._arm_conversation_watch(bot, chatter.channel.id)
        ping = _plain_followup(content=f"<@{bot.user.id}> make it a cat")
        ping.mentions = [bot.user]
        assert MaxwellBot._should_live_reply(bot, chatter) is True
        assert MaxwellBot._should_interrupt_inflight(bot, chatter) is False
        assert MaxwellBot._should_interrupt_inflight(bot, ping) is True

    asyncio.run(run())


def test_lock_timeout_requeues_a_live_reply():
    bot = _bot()
    queued = []
    bot._queue_watch_reply = lambda msg, content, directed=True: queued.append(
        (content, directed)
    )

    async def run():
        msg = _plain_followup(content="make an image of a cat")
        MaxwellBot._arm_conversation_watch(bot, msg.channel.id)
        MaxwellBot._requeue_after_lock_timeout(bot, msg)

    asyncio.run(run())
    assert queued == [("make an image of a cat", True)]


def test_bare_mention_is_a_ping_with_no_text():
    bot = _bot()
    msg = _plain_followup(content=f"<@{bot.user.id}>")
    msg.mentions = [bot.user]
    assert MaxwellBot._content_without_self_mention(bot, msg.content) == ""
    assert MaxwellBot._is_bare_ping(bot, msg) is True
    assert MaxwellBot._is_bare_ping(bot, msg, "") is True
    msg.content = f"<@{bot.user.id}> hey"
    assert MaxwellBot._is_bare_ping(bot, msg) is False


def test_bare_ping_with_an_attachment_is_not_empty():
    bot = _bot()
    msg = _plain_followup(content=f"<@{bot.user.id}>")
    msg.mentions = [bot.user]
    msg.attachments = [SimpleNamespace(filename="pic.png")]
    assert MaxwellBot._is_bare_ping(bot, msg) is False


def test_watch_prompt_names_who_is_typing():
    bot = _bot()
    msg = _plain_followup()
    channel = msg.channel
    user = SimpleNamespace(
        id=1471821513824014480, display_name="Z3ki", name="z3ki", bot=False
    )
    MaxwellBot._note_typing(bot, channel, user)
    lines = MaxwellBot._conversation_watch_prompt(bot, msg, channel.id)
    assert any("Z3ki(1471821513824014480) is typing" in line for line in lines)
    assert any("Wait for them to send" in line for line in lines)
    MaxwellBot._clear_typing(bot, channel.id, user.id)
    assert MaxwellBot._conversation_watch_prompt(bot, msg, channel.id) == []


def test_typing_expires_and_ignores_self_and_bots():
    bot = _bot()
    channel = SimpleNamespace(id=1506001126426808511)
    human = SimpleNamespace(id=7, display_name="Alice", name="alice", bot=False)
    MaxwellBot._note_typing(bot, channel, human)
    assert MaxwellBot._typing_in_channel(bot, channel.id)[0]["name"] == "Alice"
    MaxwellBot._note_typing(
        bot, channel, SimpleNamespace(id=bot.user.id, display_name="Maxwell", bot=False)
    )
    MaxwellBot._note_typing(
        bot, channel, SimpleNamespace(id=99, display_name="Botty", bot=True)
    )
    assert [p["id"] for p in MaxwellBot._typing_in_channel(bot, channel.id)] == ["7"]
    bot._typing_users[str(channel.id)]["7"]["expires_at"] = 0
    assert MaxwellBot._typing_in_channel(bot, channel.id) == []
