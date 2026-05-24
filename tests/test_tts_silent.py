import asyncio
from types import SimpleNamespace

from bot import MaxwellBot, _telegram_html, _tool_results_need_followup


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
            '{"tool":"tts","text":"say this"}',
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
            '{"tool":"react","emoji":"catjam"}',
        )
        assert response == ""
        assert tool_results == ["Tool react: Reacted with <:catjam:123>"]
        assert react.calls == [{"emoji": "catjam"}]

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
            '{"tool":"react","emoji":"catjam"}',
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
            '{"tool":"react","emoji":"catjam"}',
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
            '{"tool":"react","emoji":"catjam"}',
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
