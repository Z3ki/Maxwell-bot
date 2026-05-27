import asyncio
from types import SimpleNamespace

from bot import MaxwellBot, _telegram_html, _tool_results_need_followup, _auto_format_discord


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


def test_process_tool_calls_still_returns_other_tool_results():
    react = FakeTool("Reacted with <:catjam:123>")
    bot = SimpleNamespace(
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
        _control={"tools_enabled": True, "disabled_tools": []},
        tools={"send_file": FakeTool("sent"), "react": FakeTool("Reacted")},
    )

    prompt = MaxwellBot._tool_system_prompt(bot, "telegram")

    assert "send_file:" in prompt
    assert "react:" not in prompt


def test_tool_prompt_keeps_discord_tools_for_discord():
    bot = SimpleNamespace(
        _control={"tools_enabled": True, "disabled_tools": []},
        tools={"send_file": FakeTool("sent"), "react": FakeTool("Reacted")},
    )

    prompt = MaxwellBot._tool_system_prompt(bot, "discord")

    assert "send_file:" in prompt
    assert "react:" in prompt


def test_tool_prompt_requires_reasoning_before_terminal_action():
    bot = SimpleNamespace(
        _control={"tools_enabled": True, "disabled_tools": []},
        tools={"reasoning_log": FakeTool("__REASONING_RECORDED__"), "send_message": FakeTool("sent")},
    )

    prompt = MaxwellBot._tool_system_prompt(bot, "discord")

    assert "reasoning_log first" in prompt
    assert "Never stop after reasoning_log" in prompt


def test_ensure_reasoning_trace_backfills_missing_trace():
    reasoning = FakeTool("__REASONING_RECORDED__")
    bot = SimpleNamespace(tools={"reasoning_log": reasoning})
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
    bot = SimpleNamespace(tools={"reasoning_log": reasoning})
    message = SimpleNamespace()

    async def run():
        await MaxwellBot._ensure_reasoning_trace(bot, message, ["Tool reasoning_log: __REASONING_RECORDED__"], "hi", "reply")

    asyncio.run(run())

    assert reasoning.calls == []


def test_build_messages_includes_tool_history_outside_recent_count():
    memory = FakeMemory(
        [
            {"author": "Tool", "content": "Called search_messages with {} -> old result", "is_tool": True},
            {"author": "alice", "content": "old user message"},
            {"author": "alice", "content": "recent user message"},
        ]
    )
    bot = SimpleNamespace(
        _control={
            "base_personality": "test",
            "cross_context_enabled": False,
            "emoji_context_enabled": False,
            "long_term_memory_enabled": False,
            "memory_context_budget": 30000,
            "memory_history_messages": 1,
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
        assert "[Tool] Called search_messages with {} -> old result" in context
        assert "recent user message" in context
        assert "old user message" not in context

    asyncio.run(run())


def test_shell_tool_results_trigger_followup():
    assert _tool_results_need_followup(["Tool shell: __SHELL_SENT__\n$ date\nSat May 23"])


def test_telegram_html_renders_code_blocks():
    rendered = _telegram_html("before\n```ansi\n$ whoami\nmaxwell\n```\nafter <ok>")

    assert "before" in rendered
    assert '<pre><code class="language-ansi">$ whoami\nmaxwell</code></pre>' in rendered
    assert "after &lt;ok&gt;" in rendered


def test_no_response_tool_results_do_not_trigger_followup():
    assert not _tool_results_need_followup(["Tool no_response: __NO_RESPONSE__"])


def test_reasoning_log_with_send_message_does_not_trigger_followup():
    assert not _tool_results_need_followup([
        "Tool reasoning_log: __REASONING_RECORDED__",
        "Tool send_message: __MESSAGE_SENT__ Sent 10 chars"
    ])


def test_tool_prompt_has_no_nested_tags_rule():
    bot = SimpleNamespace(
        _control={"tools_enabled": True, "disabled_tools": []},
        tools={"reasoning_log": FakeTool("__REASONING_RECORDED__")},
    )

    prompt = MaxwellBot._tool_system_prompt(bot, "discord")

    assert "plain text" in prompt.lower()
    assert "never put" in prompt.lower()


def test_build_messages_has_single_formatting_instruction():
    memory = FakeMemory()
    bot = SimpleNamespace(
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


def test_auto_format_does_not_bold_casual_text():
    assert _auto_format_discord("hey what's up") == "hey what's up"
    assert _auto_format_discord("lol that's funny") == "lol that's funny"
    assert _auto_format_discord("nah I'm good") == "nah I'm good"
    assert _auto_format_discord("ok") == "ok"
    assert _auto_format_discord("hi") == "hi"
