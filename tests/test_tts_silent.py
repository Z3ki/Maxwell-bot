import asyncio
from types import SimpleNamespace

from bot import (
    MaxwellBot,
    ToolCircuitBreaker,
    _auto_format_discord,
    _telegram_html,
    _telegram_latest_message_label,
    _telegram_tool_followup_instruction,
    _tool_results_need_followup,
)


class FakeTool:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def get_description(self):
        return "fake tool"

    async def execute(self, message, **params):
        self.calls.append(params)
        return self.result


class FakeMemory:
    def __init__(self, messages=None):
        self.messages = list(messages or [])
        self.added = []

    async def add_to_channel_memory(self, channel_id, message):
        self.added.append((channel_id, message))

    async def get_channel_memory(self, channel_id):
        return list(self.messages)

    def get_server_prompt(self, server_id):
        return None


def test_process_tool_calls_preserves_no_response_marker_for_tts():
    tts = FakeTool("__NO_RESPONSE__")
    bot = SimpleNamespace(
        _tool_breaker=ToolCircuitBreaker(failure_threshold=999, recovery_seconds=0),
        _control={"tools_enabled": True, "disabled_tools": [], "typing_indicator": False},
        tools={"tts": tts},
    )
    message = SimpleNamespace()

    async def run():
        response, tool_results = await MaxwellBot._process_tool_calls(
            bot,
            message,
            '<tool:tts text="say this" />',
        )
        assert response == ""
        assert tool_results == ["Tool tts: __NO_RESPONSE__"]
        assert tts.calls == [{"text": "say this"}]

    asyncio.run(run())


def test_reaction_on_maxwell_message_invokes_handler():
    class NoopAsyncLock:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    calls = []
    replies = []
    maxwell_user = SimpleNamespace(id=42, display_name="Maxwell", bot=True)
    reacting_user = SimpleNamespace(id=99, display_name="alice", name="alice", bot=False)
    channel = SimpleNamespace(id=123)
    original = SimpleNamespace(
        id=777,
        author=maxwell_user,
        channel=channel,
        guild=SimpleNamespace(id=9),
    )

    async def original_reply(content=None, **kwargs):
        replies.append((content, kwargs))

    original.reply = original_reply
    reaction = SimpleNamespace(message=original, emoji="😂")

    async def handle_message(message, content):
        calls.append((message, content))

    bot = SimpleNamespace(
        user=maxwell_user,
        _load_control=lambda: None,
        _control={
            "bot_enabled": True,
            "reply_to_bots": True,
            "blocked_channels": [],
            "allowed_channels": [],
            "reply_mentions": True,
            "per_user_cooldown_seconds": 0,
        },
        _blacklist=set(),
        _cooldowns={},
        _stop_until={},
        _reaction_seen=set(),
        _get_channel_lock=lambda channel_id: NoopAsyncLock(),
        _get_reply_context=lambda message: "\nReplied-to message: Maxwell said hi",
        _handle_message=handle_message,
    )

    asyncio.run(MaxwellBot.on_reaction_add(bot, reaction, reacting_user))

    assert len(calls) == 1
    message, content = calls[0]
    assert "reacted to your message with 😂" in content
    assert "Replied-to message: Maxwell said hi" in content
    assert message.author is reacting_user
    assert message.reference.resolved is original
    assert message.mentions == [maxwell_user]
    assert message.suppress_typing is True

    asyncio.run(message.reply("hi"))
    assert replies == [("hi", {})]


def test_process_tool_calls_still_returns_other_tool_results():
    react = FakeTool("Reacted with <:catjam:123>")
    bot = SimpleNamespace(
        _tool_breaker=ToolCircuitBreaker(failure_threshold=999, recovery_seconds=0),
        _control={"tools_enabled": True, "disabled_tools": [], "typing_indicator": False},
        tools={"react": react},
    )
    message = SimpleNamespace()

    async def run():
        response, tool_results = await MaxwellBot._process_tool_calls(
            bot,
            message,
            '<tool:react emoji="catjam" />',
        )
        assert response == ""
        assert tool_results == ["Tool react: Reacted with <:catjam:123>"]
        assert react.calls == [{"emoji": "catjam"}]

    asyncio.run(run())


def test_process_tool_calls_handles_unclosed_send_message_without_leaking_environment_details():
    send_message = FakeTool("__MESSAGE_SENT__ Sent 6 chars")
    bot = SimpleNamespace(
        _tool_breaker=ToolCircuitBreaker(failure_threshold=999, recovery_seconds=0),
        _control={"tools_enabled": True, "disabled_tools": [], "typing_indicator": False},
        tools={"send_message": send_message},
    )
    message = SimpleNamespace(guild=None)
    response = '<tool:send_message>Hello!<|end|><environment_details>secret context</environment_details>'

    async def run():
        cleaned, tool_results = await MaxwellBot._process_tool_calls(bot, message, response)
        assert cleaned == ""
        assert tool_results == ["Tool send_message: __MESSAGE_SENT__ Sent 6 chars"]
        assert send_message.calls == [{"content": "Hello!"}]

    asyncio.run(run())


def test_process_tool_calls_handles_reasoning_json_tts_without_leaking_system_reminder():
    tts = FakeTool("__NO_RESPONSE__")
    bot = SimpleNamespace(
        _tool_breaker=ToolCircuitBreaker(failure_threshold=999, recovery_seconds=0),
        _control={"tools_enabled": True, "disabled_tools": [], "typing_indicator": False},
        tools={"tts": tts},
    )
    message = SimpleNamespace()
    response = '''{
  "thoughts": "User asked for a TTS.",
  "intent": "Provide a text-to-speech response.",
  "decision": "Call the tts tool.",
  "tool_plan": "Use tts with text Hey there."
}
<tool:tts text="Hey there!" language="english" />
<system-reminder>secret context</system-reminder>'''

    async def run():
        cleaned, tool_results = await MaxwellBot._process_tool_calls(bot, message, response)
        assert cleaned == ""
        assert tool_results == ["Tool tts: __NO_RESPONSE__"]
        assert tts.calls == [{"text": "Hey there!", "language": "english"}]

    asyncio.run(run())


def test_process_tool_calls_handles_pipe_tts_format():
    tts = FakeTool("__NO_RESPONSE__")
    bot = SimpleNamespace(
        _tool_breaker=ToolCircuitBreaker(failure_threshold=999, recovery_seconds=0),
        _control={"tools_enabled": True, "disabled_tools": [], "typing_indicator": False},
        tools={"tts": tts},
    )
    message = SimpleNamespace()

    async def run():
        cleaned, tool_results = await MaxwellBot._process_tool_calls(
            bot,
            message,
            "<|tool_call_begin|>tts|>text=Test tts language=spanish<|tool_call_end|>",
        )
        assert cleaned == ""
        assert tool_results == ["Tool tts: __NO_RESPONSE__"]
        assert tts.calls == [{"text": "Test tts", "language": "spanish"}]

    asyncio.run(run())


def test_process_tool_calls_records_tool_history_in_memory():
    react = FakeTool("Reacted with <:catjam:123>")
    memory = FakeMemory()
    bot = SimpleNamespace(
        _tool_breaker=ToolCircuitBreaker(failure_threshold=999, recovery_seconds=0),
        _control={"tools_enabled": True, "disabled_tools": [], "typing_indicator": False, "store_memory": True},
        tools={"react": react},
        memory=memory,
    )
    message = SimpleNamespace(channel=SimpleNamespace(id=123))

    async def run():
        response, tool_results = await MaxwellBot._process_tool_calls(
            bot,
            message,
            '<tool:react emoji="catjam" />',
        )
        assert response == ""
        assert tool_results == ["Tool react: Reacted with <:catjam:123>"]
        assert memory.added == [
            (
                "123",
                {
                    "author": "Tool",
                    "content": 'Called react with {"emoji": "catjam"} -> Reacted with <:catjam:123>',
                    "is_tool": True,
                    "tool_name": "react",
                    "tool_params": {"emoji": "catjam"},
                    "tool_result": "Reacted with <:catjam:123>",
                },
            )
        ]

    asyncio.run(run())


def test_process_tool_calls_strips_disabled_tool_call():
    react = FakeTool("Reacted with <:catjam:123>")
    bot = SimpleNamespace(
        _tool_breaker=ToolCircuitBreaker(failure_threshold=999, recovery_seconds=0),
        _control={"tools_enabled": True, "disabled_tools": ["react"], "typing_indicator": False},
        tools={"react": react},
    )
    message = SimpleNamespace()

    async def run():
        response, tool_results = await MaxwellBot._process_tool_calls(
            bot,
            message,
            '<tool:react emoji="catjam" />',
        )
        assert response == ""
        assert tool_results == ["Tool react: Error - tool is disabled"]
        assert react.calls == []

    asyncio.run(run())


def test_process_tool_calls_strips_platform_incompatible_tool_call():
    react = FakeTool("Reacted")
    bot = SimpleNamespace(
        _tool_breaker=ToolCircuitBreaker(failure_threshold=999, recovery_seconds=0),
        _control={"tools_enabled": True, "disabled_tools": [], "typing_indicator": False},
        tools={"react": react},
    )
    message = SimpleNamespace(tool_platform="telegram")

    async def run():
        response, tool_results = await MaxwellBot._process_tool_calls(
            bot,
            message,
            '<tool:react emoji="catjam" />',
        )
        assert response == ""
        assert tool_results == ["Tool react: Error - tool is not available on this platform"]
        assert react.calls == []

    asyncio.run(run())


def test_tool_prompt_filters_discord_only_tools_for_telegram():
    bot = SimpleNamespace(
        _tool_breaker=ToolCircuitBreaker(failure_threshold=999, recovery_seconds=0),
        _control={"tools_enabled": True, "disabled_tools": []},
        tools={"send_file": FakeTool("sent"), "react": FakeTool("Reacted")},
    )

    prompt = MaxwellBot._tool_system_prompt(bot, "telegram")

    assert "send_file:" in prompt
    assert "react:" not in prompt


def test_tool_prompt_keeps_discord_tools_for_discord():
    bot = SimpleNamespace(
        _tool_breaker=ToolCircuitBreaker(failure_threshold=999, recovery_seconds=0),
        _control={"tools_enabled": True, "disabled_tools": []},
        tools={"send_file": FakeTool("sent"), "react": FakeTool("Reacted")},
    )

    prompt = MaxwellBot._tool_system_prompt(bot, "discord")

    assert "send_file:" in prompt
    assert "react:" in prompt


def test_tool_prompt_requires_reasoning_before_terminal_action():
    bot = SimpleNamespace(
        _tool_breaker=ToolCircuitBreaker(failure_threshold=999, recovery_seconds=0),
        _control={"tools_enabled": True, "disabled_tools": []},
        tools={"reasoning_log": FakeTool("__REASONING_RECORDED__"), "send_message": FakeTool("sent")},
    )

    prompt = MaxwellBot._tool_system_prompt(bot, "discord")

    assert "reasoning_log first" in prompt
    assert "Never stop after reasoning_log" in prompt


def test_ensure_reasoning_trace_backfills_missing_trace():
    reasoning = FakeTool("__REASONING_RECORDED__")
    bot = SimpleNamespace(_tool_breaker=ToolCircuitBreaker(failure_threshold=999, recovery_seconds=0), tools={"reasoning_log": reasoning})
    message = SimpleNamespace()

    async def run():
        await MaxwellBot._ensure_reasoning_trace(bot, message, ["Tool send_message: __MESSAGE_SENT__ Sent 2 chars"], "hi", "send_message")

    asyncio.run(run())

    assert reasoning.calls == [
        {
            "intent": "forced_trace",
            "decision": "send_message",
            "thoughts": "Auto-recorded because the model did not call reasoning_log before terminal output.",
            "data": {
                "response_preview": "hi",
                "response_chars": 2,
                "tool_results": ["Tool send_message: __MESSAGE_SENT__ Sent 2 chars"],
            },
        }
    ]


def test_ensure_reasoning_trace_skips_existing_trace():
    reasoning = FakeTool("__REASONING_RECORDED__")
    bot = SimpleNamespace(_tool_breaker=ToolCircuitBreaker(failure_threshold=999, recovery_seconds=0), tools={"reasoning_log": reasoning})
    message = SimpleNamespace()

    async def run():
        await MaxwellBot._ensure_reasoning_trace(bot, message, ["Tool reasoning_log: __REASONING_RECORDED__"], "hi", "reply")

    asyncio.run(run())

    assert reasoning.calls == []


def test_build_messages_caps_tool_history_outside_recent_count():
    memory = FakeMemory(
        [
            {"author": "Tool", "content": "Called search_messages with {} -> tool 1", "is_tool": True},
            {"author": "Tool", "content": "Called search_messages with {} -> tool 2", "is_tool": True},
            {"author": "Tool", "content": "Called search_messages with {} -> tool 3", "is_tool": True},
            {"author": "Tool", "content": "Called search_messages with {} -> tool 4", "is_tool": True},
            {"author": "alice", "content": "old user message"},
            {"author": "alice", "content": "recent user message"},
        ]
    )
    bot = SimpleNamespace(
        _tool_breaker=ToolCircuitBreaker(failure_threshold=999, recovery_seconds=0),
        _control={
            "base_personality": "test",
            "cross_context_enabled": False,
            "emoji_context_enabled": False,
            "long_term_memory_enabled": False,
            "memory_context_budget": 30000,
            "memory_history_messages": 1,
            "tool_history_messages": 3,
            "music_context_enabled": False,
            "tools_enabled": False,
        },
        _drugged_until={},
        _guild_emojis={},
        _tool_system_prompt=lambda: "",
        bot_name="Maxwell",
        memory=memory,
        user=SimpleNamespace(display_name="Maxwell"),
    )
    message = SimpleNamespace(
        author=SimpleNamespace(bot=False, display_name="alice", id=456),
        channel=SimpleNamespace(id=123),
        guild=None,
        id=789,
        mentions=[],
        reference=None,
    )

    async def run():
        messages = await MaxwellBot._build_messages(bot, message, "latest")
        context = "\n".join(m["content"] for m in messages)
        assert "tool 1" not in context
        assert "tool 2" in context
        assert "tool 3" in context
        assert "tool 4" in context
        assert "recent user message" in context
        assert "old user message" not in context

    asyncio.run(run())


def test_process_tool_calls_skips_duplicate_terminal_tools():
    first = FakeTool("__MESSAGE_SENT__ Sent 1 chars")
    second = FakeTool("__MESSAGE_SENT__ Sent 2 chars")
    bot = SimpleNamespace(
        _tool_breaker=ToolCircuitBreaker(failure_threshold=999, recovery_seconds=0),
        _control={"tools_enabled": True, "disabled_tools": [], "typing_indicator": False, "store_memory": False},
        tools={"send_message": first, "no_response": second},
        _message_tool_platform=lambda _message: "discord",
        _compatible_tool_names=lambda _platform: {"send_message", "no_response"},
    )
    message = SimpleNamespace(guild=None, channel=SimpleNamespace())

    async def run():
        _response, tool_results = await MaxwellBot._process_tool_calls(
            bot,
            message,
            "<tool:send_message>hi</tool:send_message><tool:no_response />",
        )
        assert first.calls == [{"content": "hi"}]
        assert second.calls == []
        assert "Skipped duplicate terminal tool call" in tool_results[-1]

    asyncio.run(run())


def test_shell_tool_results_trigger_followup():
    assert _tool_results_need_followup(["Tool shell: __SHELL_SENT__\n$ date\nSat May 23"])


def test_telegram_html_renders_code_blocks():
    rendered = _telegram_html("before\n```ansi\n$ whoami\nmaxwell\n```\nafter <ok>")

    assert "before" in rendered
    assert '<pre><code class="language-ansi">$ whoami\nmaxwell</code></pre>' in rendered
    assert "after &lt;ok&gt;" in rendered


def test_telegram_audio_turn_uses_stable_latest_message_label():
    assert _telegram_latest_message_label("", has_media=True) == "[audio message attached]"
    assert _telegram_latest_message_label("make an image", has_media=True) == "make an image"


def test_telegram_tool_followup_keeps_audio_turn_context_available():
    instruction = _telegram_tool_followup_instruction(has_original_media=True)

    assert "already attached to the first model pass" in instruction
    assert "do not say you cannot hear" in instruction
    assert "<tool:send_message>" in instruction


def test_telegram_tool_followup_without_media_does_not_claim_audio_context():
    instruction = _telegram_tool_followup_instruction(has_original_media=False)

    assert "No original Telegram media" in instruction
    assert "already attached to the first model pass" not in instruction


def test_no_response_tool_results_do_not_trigger_followup():
    assert not _tool_results_need_followup(["Tool no_response: __NO_RESPONSE__"])


def test_reasoning_log_with_send_message_does_not_trigger_followup():
    assert not _tool_results_need_followup([
        "Tool reasoning_log: __REASONING_RECORDED__",
        "Tool send_message: __MESSAGE_SENT__ Sent 10 chars"
    ])


def test_tool_prompt_has_no_nested_tags_rule():
    bot = SimpleNamespace(
        _tool_breaker=ToolCircuitBreaker(failure_threshold=999, recovery_seconds=0),
        _control={"tools_enabled": True, "disabled_tools": []},
        tools={"reasoning_log": FakeTool("__REASONING_RECORDED__")},
    )

    prompt = MaxwellBot._tool_system_prompt(bot, "discord")

    assert "plain text" in prompt.lower()
    assert "never put" in prompt.lower()


def test_prompt_budget_trims_large_background_blocks():
    bot = SimpleNamespace(_tool_breaker=ToolCircuitBreaker(failure_threshold=999, recovery_seconds=0), _control={"prompt_context_budget": 10000})
    messages = [
        {"role": "system", "content": "core"},
        {"role": "system", "content": "x" * 50000},
        {"role": "user", "content": "latest"},
    ]

    trimmed = MaxwellBot._apply_prompt_budget(bot, messages)

    assert sum(MaxwellBot._message_content_chars(m) for m in trimmed) <= 10000
    assert "prompt budget trimmed" in trimmed[1]["content"]


def test_shared_fact_relevance_filters_broad_vague_context():
    assert MaxwellBot._shared_fact_relevant("lol", {"scope": "guild:1", "content": "project alpha uses postgres"}) is False
    assert MaxwellBot._shared_fact_relevant("what database does project alpha use", {"scope": "guild:1", "content": "project alpha uses postgres"}) is True
    assert MaxwellBot._shared_fact_relevant("lol", {"scope": "user:1", "content": "likes terse replies"}) is True


def test_build_messages_has_single_formatting_instruction():
    memory = FakeMemory()
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
        _tool_system_prompt=lambda: "",
        bot_name="Maxwell",
        memory=memory,
        user=SimpleNamespace(display_name="Maxwell"),
    )
    message = SimpleNamespace(
        author=SimpleNamespace(bot=False, display_name="alice", id=456),
        channel=SimpleNamespace(id=123),
        guild=None,
        id=789,
        mentions=[],
        reference=None,
    )

    async def run():
        messages = await MaxwellBot._build_messages(bot, message, "hello")
        system_content = messages[0]["content"]
        assert "MANDATORY" not in system_content
        assert "MUST format every response" not in system_content
        assert "Do not force markdown into tiny greetings" in system_content

    asyncio.run(run())


def test_cached_media_context_requires_latest_visual_reference():
    message = SimpleNamespace(reference=None)

    assert not MaxwellBot._should_use_cached_media_context(message, "lol ok")
    assert MaxwellBot._should_use_cached_media_context(message, "what's in that image?")
    assert MaxwellBot._should_use_cached_media_context(message, "look at this")


def test_cached_media_context_allowed_for_attachment_reply():
    replied = SimpleNamespace(id=123, attachments=[SimpleNamespace(filename="old.png")], embeds=[])
    message = SimpleNamespace(reference=SimpleNamespace(resolved=replied))

    assert MaxwellBot._should_use_cached_media_context(message, "what is that")


def test_cached_media_context_can_filter_by_reply_message_id():
    bot = SimpleNamespace(_tool_breaker=ToolCircuitBreaker(failure_threshold=999, recovery_seconds=0), _media_context={
        "c": [
            {"b64": "old", "mime_type": "image/png", "filename": "old.png", "message_id": 1},
            {"b64": "right", "mime_type": "image/png", "filename": "right.png", "message_id": 2},
        ]
    })

    media = MaxwellBot._get_media_context(bot, "c", message_id=2)

    assert [item["filename"] for item in media] == ["right.png"]


def test_current_image_does_not_mix_cached_media_without_prior_reference():
    assert not MaxwellBot._should_mix_cached_with_current("look at this")
    assert MaxwellBot._should_mix_cached_with_current("compare this with the previous image")


def test_media_summary_does_not_tell_model_to_force_old_images():
    active = [{"filename": "old.png", "mime_type": "image/png", "message_id": 1}]

    summary = MaxwellBot._format_media_summary([], active)

    assert "Only discuss them when relevant to the latest message" in summary
    assert "Use these actual image attachments when answering" not in summary


def test_auto_format_does_not_bold_casual_text():
    assert _auto_format_discord("hey what's up") == "hey what's up"
    assert _auto_format_discord("lol that's funny") == "lol that's funny"
    assert _auto_format_discord("nah I'm good") == "nah I'm good"
    assert _auto_format_discord("ok") == "ok"
    assert _auto_format_discord("hi") == "hi"
