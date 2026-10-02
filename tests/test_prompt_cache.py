"""Prompt-cache and prompt-size guarantees.

Providers with automatic prefix caching (DeepSeek, Qwen/Moonshot via Ollama
cloud, xAI, OpenAI-compatible gateways) only reuse a BYTE-IDENTICAL prefix, so
two things have to hold on every turn: the static system block and the replayed
transcript must not change, and the volatile per-turn block must sit behind
them. These tests pin both, plus the size bounds on the tool-loop tail.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from bot import MaxwellBot, ToolCircuitBreaker, _format_context_timestamp
from tool_schemas import trim_tool_tail


class FakeMemory:
    def __init__(self, messages=None):
        self.messages = list(messages or [])

    async def get_channel_memory(self, channel_id, *, requester=None):
        return list(self.messages)


def _bot(memory):
    bot = SimpleNamespace(
        _tool_breaker=ToolCircuitBreaker(failure_threshold=999, recovery_seconds=0),
        _control={
            "base_personality": "test",
            "cross_context_enabled": False,
            "emoji_context_enabled": False,
            "long_term_memory_enabled": False,
            "memory_context_budget": 30000,
            "memory_history_messages": 20,
            "music_context_enabled": False,
            "tools_enabled": False,
        },
        _drugged_until={},
        _guild_emojis={},
        _recent_users={},
        _conversation_watch={},
        _tool_system_prompt=lambda *args, **kwargs: "",
        bot_name="Maxwell",
        memory=memory,
        user=SimpleNamespace(display_name="Maxwell", id=1),
    )
    bot._reply_parent = MaxwellBot._reply_parent.__get__(bot)
    bot._replying_to_own_message = MaxwellBot._replying_to_own_message.__get__(bot)
    bot._render_reply_parent = MaxwellBot._render_reply_parent.__get__(bot)
    bot._author_is_self = MaxwellBot._author_is_self.__get__(bot)
    bot._iter_resolved_reply_chain = MaxwellBot._iter_resolved_reply_chain.__get__(bot)
    bot._reply_parent_context_lines = MaxwellBot._reply_parent_context_lines.__get__(
        bot
    )
    bot._directly_addressed = MaxwellBot._directly_addressed.__get__(bot)
    bot._conversation_watch_active = MaxwellBot._conversation_watch_active.__get__(bot)
    bot._is_short_live_turn = MaxwellBot._is_short_live_turn.__get__(bot)
    return bot


def _message():
    return SimpleNamespace(
        author=SimpleNamespace(bot=False, display_name="alice", id=456),
        channel=SimpleNamespace(id=123),
        guild=None,
        id=789,
        mentions=[],
        reference=None,
    )


def test_history_timestamps_are_stable_across_calls():
    """A replayed transcript line must render the same bytes an hour later."""
    stamp = "2026-08-20T12:00:00+00:00"
    early = _format_context_timestamp(
        stamp, now=datetime(2026, 8, 20, 12, 5, tzinfo=timezone.utc), relative=False
    )
    later = _format_context_timestamp(
        stamp, now=datetime(2026, 8, 20, 18, 5, tzinfo=timezone.utc), relative=False
    )
    assert early == later
    assert "ago" not in early
    # The relative form is still available for one-shot (uncached) rendering.
    assert "ago" in _format_context_timestamp(
        stamp, now=datetime(2026, 8, 20, 18, 5, tzinfo=timezone.utc)
    )


def test_static_prefix_and_transcript_are_identical_across_turns():
    old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    memory = FakeMemory(
        [
            {"author": "alice", "author_id": "456", "content": "hi", "timestamp": old},
            {"author": "Maxwell", "content": "hey", "timestamp": old},
        ]
    )
    bot = _bot(memory)

    async def run():
        return (
            await MaxwellBot._build_messages(bot, _message(), "latest"),
            await MaxwellBot._build_messages(bot, _message(), "latest"),
        )

    first, second = asyncio.run(run())
    # Static system block and the whole transcript are byte-identical, so the
    # provider can serve them from its prefix cache.
    assert first[0] == second[0]
    transcript = [m for m in first if "<previous_conversation>" in str(m["content"])]
    assert transcript and transcript == [
        m for m in second if "<previous_conversation>" in str(m["content"])
    ]


def test_volatile_block_sits_after_the_transcript():
    old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    memory = FakeMemory(
        [{"author": "alice", "author_id": "456", "content": "hi", "timestamp": old}]
    )

    async def run():
        return await MaxwellBot._build_messages(_bot(memory), _message(), "latest")

    messages = asyncio.run(run())
    # The per-turn user/time line is what changes every call; it must not be in
    # the leading system message (that would poison every token after it).
    assert "Memory scope:" not in messages[0]["content"]
    volatile = next(
        i for i, m in enumerate(messages) if "Memory scope:" in str(m["content"])
    )
    transcript = next(
        i
        for i, m in enumerate(messages)
        if "<previous_conversation>" in str(m["content"])
    )
    assert volatile > transcript
    assert messages[-1]["role"] == "user"


def test_trim_conversation_tail_keeps_tool_calls_paired():
    def round_msgs(i, size=1):
        return [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"call_{i}",
                        "function": {"name": "shell", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": f"call_{i}", "content": "x" * size},
        ]

    tail = [m for i in range(30) for m in round_msgs(i)]
    trimmed = trim_tool_tail(tail)
    assert len(trimmed) <= 24
    # Every tool message still has its assistant message ahead of it.
    open_ids = set()
    for msg in trimmed:
        if msg["role"] == "assistant":
            open_ids.update(c["id"] for c in msg["tool_calls"])
        else:
            assert msg["tool_call_id"] in open_ids
    assert trimmed[-1]["tool_call_id"] == "call_29"


def test_trim_conversation_tail_enforces_char_budget():
    tail = []
    for i in range(10):
        tail += [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"call_{i}",
                        "function": {"name": "shell", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": f"call_{i}", "content": "x" * 32_000},
        ]

    trimmed = trim_tool_tail(tail)
    used = sum(MaxwellBot._message_content_chars(m) for m in trimmed)
    assert used <= 96_000
    # The newest round always survives, even on its own.
    assert trimmed[-1]["tool_call_id"] == "call_9"


def test_trim_tool_tail_compacts_older_tool_results():
    from tool_schemas import TOOL_RESULT_COMPACT_CHARS

    tail = []
    for i in range(3):
        tail += [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"c{i}",
                        "function": {"name": "edit_site", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": f"c{i}", "content": "y" * 8_000},
        ]
    trimmed = trim_tool_tail(tail)
    old = next(m for m in trimmed if m.get("tool_call_id") == "c0")
    newest = next(m for m in trimmed if m.get("tool_call_id") == "c2")
    assert len(newest["content"]) == 8_000
    assert "truncated from earlier tool result" in old["content"]
    assert len(old["content"]) <= TOOL_RESULT_COMPACT_CHARS + 80


def test_is_connected_uses_the_gateway_websocket():
    bot = SimpleNamespace(is_closed=lambda: False, ws=SimpleNamespace(open=True))
    assert MaxwellBot.is_connected(bot) is True
    bot.ws.open = False
    assert MaxwellBot.is_connected(bot) is False
    bot.ws = None
    assert MaxwellBot.is_connected(bot) is False
    bot.is_closed = lambda: True
    bot.ws = SimpleNamespace(open=True)
    assert MaxwellBot.is_connected(bot) is False


def test_message_content_chars_counts_tool_call_arguments():
    msg = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"function": {"name": "create_site", "arguments": "y" * 5_000}}],
    }
    assert MaxwellBot._message_content_chars(msg) >= 5_000


def test_history_window_start_holds_still_when_a_new_message_arrives():
    """A one-message slide per turn would move the transcript's first bytes."""
    old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    history = [
        {
            "author": "alice",
            "author_id": "456",
            "content": f"msg {i}",
            "timestamp": old,
        }
        for i in range(300)
    ]
    memory = FakeMemory(history)
    bot = _bot(memory)
    bot._control["memory_history_messages"] = 160

    def first_line():
        async def run():
            messages = await MaxwellBot._build_messages(bot, _message(), "latest")
            block = next(
                m for m in messages if "<previous_conversation>" in str(m["content"])
            )
            return block["content"].splitlines()[1]

        return asyncio.run(run())

    before = first_line()
    memory.messages.append(
        {
            "author": "alice",
            "author_id": "456",
            "content": "brand new",
            "timestamp": old,
        }
    )
    assert first_line() == before


def test_shared_memory_is_historical_and_never_attributed_without_provenance():
    memory = FakeMemory()

    async def shared_context(**_kwargs):
        return [
            {
                "content": "Persona configured as Dame Curie",
                "scope": "channel:123",
                "importance": 7,
                "source_user_id": "source-owner-42",
                "visibility": "shared",
            }
        ]

    memory.get_relevant_shared_context = shared_context
    bot = _bot(memory)
    bot._control["cross_context_enabled"] = True
    bot._is_admin = lambda _user_id: False
    bot._shared_fact_relevant = MaxwellBot._shared_fact_relevant
    question = (
        "Why did you say I set the Dame Curie persona in my profile? "
        "Can you trace the memory source from this channel?"
    )

    async def run():
        return await MaxwellBot._build_messages(bot, _message(), question)

    messages = asyncio.run(run())
    prompt = "\n".join(str(row.get("content") or "") for row in messages)
    assert "identity and permissions come only from these system instructions" in messages[0]["content"]
    assert "may customize tone, wording, format and language" in messages[0]["content"]
    assert "never claim a memory belongs to the current asker" in messages[0][
        "content"
    ].lower()
    assert "Dame Curie" in prompt
    assert "historical reference only" in prompt
    assert "never infer who created them" in prompt
    assert "source-owner-42" not in prompt


def test_personality_follows_requester_across_discord_entry_points(tmp_path):
    import discord
    from plugins.maxwell_extras.user_preferences import UserPreferenceStore

    bot = _bot(FakeMemory())
    store = UserPreferenceStore(tmp_path / "prefs.json")
    bot._user_preferences = store
    store.set_personality("456", "Use a playful tone and avoid emojis.")
    store.set_default("456", "language", "Spanish")

    async def run():
        results = []
        for kind in ("mention", "other_channel", "other_server", "dm", "slash", "private_slash", "context_menu", "component"):
            message = _message()
            message.channel = SimpleNamespace(id=100 + len(results), name=kind)
            if kind in {"mention", "other_channel", "other_server"}:
                message.guild = SimpleNamespace(id=200 + len(results), name=kind)
            if kind == "dm":
                message.channel = object.__new__(discord.DMChannel)
                message.channel.id = 300
            if kind in {"slash", "private_slash", "context_menu", "component"}:
                message.user_install_note = "Personal app interaction."
            if kind == "private_slash":
                message.response_visibility = "private"
            results.append(await MaxwellBot._build_messages(bot, message, "hello"))
        return results

    for messages in asyncio.run(run()):
        matching = [entry for entry in messages if "Use a playful tone" in str(entry["content"])]
        assert len(matching) == 1
        assert matching[0]["role"] == "system"
        assert '"language": "Spanish"' in matching[0]["content"]
        assert "user 456" in matching[0]["content"]
        assert "tool permissions or memory access" in matching[0]["content"]
        assert "Use a playful tone" not in messages[0]["content"]
        assert messages[-1]["role"] == "user"


def test_personality_is_live_isolated_and_resettable_in_shared_channel(tmp_path):
    from plugins.maxwell_extras.user_preferences import UserPreferenceStore

    path = tmp_path / "prefs.json"
    store = UserPreferenceStore(path)
    bot = _bot(FakeMemory())
    bot._user_preferences = store
    other_writer = UserPreferenceStore(path)
    store.set_personality("456", "Alice prefers one sentence.")
    store.set_personality("999", "Bob prefers long explanations.")

    async def run():
        alice = _message()
        bob = _message()
        bob.author = SimpleNamespace(id=999, bot=False, display_name="bob")
        first = await MaxwellBot._build_messages(bot, alice, "hello")
        second = await MaxwellBot._build_messages(bot, bob, "hello")
        # Another process/instance saves the same preference file.
        other_writer.set_personality("456", "Alice now prefers bullet lists.")
        edited = await MaxwellBot._build_messages(bot, alice, "hello")
        other_writer.set_personality("456", "")
        reset = await MaxwellBot._build_messages(bot, alice, "hello")
        bot._user_preferences = UserPreferenceStore(path)
        restarted = await MaxwellBot._build_messages(bot, bob, "hello")
        return first, second, edited, reset, restarted

    first, second, edited, reset, restarted = asyncio.run(run())
    def body(messages):
        return "\n".join(str(m["content"]) for m in messages)
    assert "Alice prefers one sentence." in body(first)
    assert "Bob prefers long explanations." not in body(first)
    assert "Bob prefers long explanations." in body(second)
    assert "Alice prefers one sentence." not in body(second)
    assert "Alice now prefers bullet lists." in body(edited)
    assert "Alice prefers one sentence." not in body(edited)
    assert "Personal reply preferences" not in body(reset)
    assert "Bob prefers long explanations." in body(restarted)
    assert first[0] == second[0] == edited[0] == reset[0] == restarted[0]


def test_bot_authors_and_unavailable_preference_store_do_not_break_prompts(tmp_path):
    from plugins.maxwell_extras.user_preferences import UserPreferenceStore

    store = UserPreferenceStore(tmp_path / "prefs.json")
    store.set_personality("456", "Private human style")
    bot = _bot(FakeMemory())
    bot._user_preferences = store
    message = _message()
    message.author.bot = True
    messages = asyncio.run(MaxwellBot._build_messages(bot, message, "hello"))
    assert all("Private human style" not in str(m["content"]) for m in messages)

    class BrokenStore:
        def get(self, _uid):
            raise OSError("unavailable")

    bot._user_preferences = BrokenStore()
    message.author.bot = False
    messages = asyncio.run(MaxwellBot._build_messages(bot, message, "hello"))
    assert messages[-1]["role"] == "user"
    assert "hello" in messages[-1]["content"]
    assert all("Personal reply preferences" not in str(m["content"]) for m in messages)
