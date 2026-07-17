import asyncio
import json
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


def _native_call(name, args, call_id="call_1"):
    """Build a raw OpenAI-style tool_call the native dispatcher expects."""
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def _native_bot(tools, **control):
    """A stub bot good enough to drive _dispatch_tool_calls (native path).

    Wires everything the rewritten _execute_tool_by_name touches: reasoning
    recording, the destructive-confirm gate, platform compat, the breaker.
    """
    control_defaults = {
        "tools_enabled": True,
        "disabled_tools": [],
        "typing_indicator": False,
        "store_memory": False,
    }
    control_defaults.update(control)

    class _Breaker:
        def is_open(self, name):
            return False

        def record_failure(self, name):
            pass

        def record_success(self, name):
            pass

    bot = SimpleNamespace(
        _tool_breaker=_Breaker(),
        _control=control_defaults,
        tools=dict(tools),
        traces=[],
        _tainted_messages=set(),
        memory=FakeMemory(),
    )
    bot._message_tool_platform = lambda _message: "discord"
    bot._compatible_tool_names = lambda _platform: set(tools)
    bot.is_message_tainted = lambda _message: False
    bot._consume_destructive_confirm = lambda _author_id: False
    bot._render_custom_emojis = lambda text, _guild: text

    async def _record_llm_trace(message, payload):
        bot.traces.append(payload)

    bot._record_llm_trace = _record_llm_trace
    return bot


def memory_added(bot):
    return bot.memory.added


def test_dispatch_native_runs_tool_and_records_reasoning():
    tts = FakeTool("__TTS_SENT__")
    bot = _native_bot({"tts": tts})
    message = SimpleNamespace(guild=None, channel=SimpleNamespace(id=123))
    raw = [_native_call("tts", {"text": "say this", "reasoning": "user wants speech"})]

    async def run():
        return await MaxwellBot._dispatch_tool_calls(
            bot, message, "", native_tool_calls=raw
        )

    _, tool_results = asyncio.run(run())
    assert tool_results == ["Tool tts: __TTS_SENT__"]
    # reasoning must be stripped from the args passed to the tool
    assert tts.calls == [{"text": "say this"}]
    # and recorded to the trace attached to the real tool
    assert bot.traces and bot.traces[0]["tool"] == "tts"
    assert bot.traces[0]["thoughts"] == "user wants speech"


def test_tts_results_do_not_block_tool_followup():
    # TTS must not be treated as terminal no_response (allows multi-tool batches).
    assert _tool_results_need_followup(
        ["Tool tts: __TTS_SENT__", "Tool web_search: found 3 results"]
    )
    # TTS alone does not need followup (no FOLLOWUP_TOOL_NAMES hit).
    assert not _tool_results_need_followup(["Tool tts: __TTS_SENT__"])
    # web_search alone needs followup
    assert _tool_results_need_followup(["Tool web_search: found 3 results"])


def test_send_file_results_need_followup():
    # send_file must trigger a follow-up turn so the model can send more files
    # and finally reply. Without this, a batch of send_file with no terminal
    # send_message breaks the tool loop and silently caps the number of files.
    assert _tool_results_need_followup(
        ["Tool send_file: __FILE_SENT__ Sent file: a.txt (4 bytes)"]
    )
    assert _tool_results_need_followup(
        [
            "Tool send_file: __FILE_SENT__ Sent file: a.txt (4 bytes)",
            "Tool send_file: __FILE_SENT__ Sent file: b.txt (4 bytes)",
        ]
    )
    # A terminal send_message still wins -> no followup needed.
    assert not _tool_results_need_followup(
        [
            "Tool send_file: __FILE_SENT__ Sent file: a.txt (4 bytes)",
            "Tool send_message: __MESSAGE_SENT__ Here you go",
        ]
    )


def test_reaction_on_maxwell_message_invokes_handler():
    class NoopAsyncLock:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    calls = []
    replies = []
    maxwell_user = SimpleNamespace(id=42, display_name="Maxwell", bot=True)
    reacting_user = SimpleNamespace(
        id=99, display_name="alice", name="alice", bot=False
    )
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


def test_dispatch_native_runs_nonterminal_tool():
    react = FakeTool("Reacted with <:catjam:123>")
    bot = _native_bot({"react": react})
    message = SimpleNamespace(guild=None, channel=SimpleNamespace(id=123))
    raw = [_native_call("react", {"emoji": "catjam", "reasoning": "reacting"})]

    async def run():
        return await MaxwellBot._dispatch_tool_calls(
            bot, message, "", native_tool_calls=raw
        )

    _, tool_results = asyncio.run(run())
    assert tool_results == ["Tool react: Reacted with <:catjam:123>"]
    assert react.calls == [{"emoji": "catjam"}]


def test_dispatch_native_records_tool_history_in_memory():
    react = FakeTool("Reacted with <:catjam:123>")
    bot = _native_bot({"react": react}, store_memory=True)
    message = SimpleNamespace(guild=None, channel=SimpleNamespace(id=123))
    raw = [_native_call("react", {"emoji": "catjam"})]

    async def run():
        return await MaxwellBot._dispatch_tool_calls(
            bot, message, "", native_tool_calls=raw
        )

    _, tool_results = asyncio.run(run())
    assert tool_results == ["Tool react: Reacted with <:catjam:123>"]
    # native path stores the full "Tool name: result" line as tool_result
    assert memory_added(bot) == [
        (
            "123",
            {
                "author": "Tool",
                "content": 'Called react with {"emoji": "catjam"} -> Tool react: Reacted with <:catjam:123>',
                "is_tool": True,
                "tool_name": "react",
                "tool_params": {"emoji": "catjam"},
                "tool_result": "Tool react: Reacted with <:catjam:123>",
            },
        )
    ]


def test_dispatch_native_strips_disabled_tool():
    react = FakeTool("Reacted")
    bot = _native_bot({"react": react}, disabled_tools=["react"])
    message = SimpleNamespace(guild=None, channel=SimpleNamespace(id=123))
    raw = [_native_call("react", {"emoji": "catjam"})]

    async def run():
        return await MaxwellBot._dispatch_tool_calls(
            bot, message, "", native_tool_calls=raw
        )

    _, tool_results = asyncio.run(run())
    assert tool_results == ["Tool react: Error - tool is disabled"]
    assert react.calls == []


def test_dispatch_native_rejects_platform_incompatible_tool():
    react = FakeTool("Reacted")
    bot = _native_bot({"react": react})
    # react is Discord-only; pretend this turn is telegram so it's incompatible.
    bot._message_tool_platform = lambda _message: "telegram"
    bot._compatible_tool_names = lambda _platform: set()  # nothing compatible on tg here
    message = SimpleNamespace(guild=None, channel=SimpleNamespace(id=123), tool_platform="telegram")
    raw = [_native_call("react", {"emoji": "catjam"})]

    async def run():
        return await MaxwellBot._dispatch_tool_calls(
            bot, message, "", native_tool_calls=raw
        )

    _, tool_results = asyncio.run(run())
    assert tool_results == ["Tool react: Error - tool is not available on this platform"]
    assert react.calls == []


def test_dispatch_native_skips_duplicate_terminal_tools():
    first = FakeTool("__MESSAGE_SENT__ Sent 1 chars")
    second = FakeTool("__MESSAGE_SENT__ Sent 2 chars")
    bot = _native_bot({"send_message": first, "no_response": second})
    message = SimpleNamespace(guild=None, channel=SimpleNamespace(id=123))
    raw = [
        _native_call("send_message", {"content": "hi"}, "c1"),
        _native_call("no_response", {}, "c2"),
    ]

    async def run():
        return await MaxwellBot._dispatch_tool_calls(
            bot, message, "", native_tool_calls=raw
        )

    _, tool_results = asyncio.run(run())
    assert first.calls == [{"content": "hi"}]
    assert second.calls == []
    assert "Skipped duplicate terminal tool call" in tool_results[-1]


def test_dispatch_no_native_calls_just_sanitizes_text():
    # No tool calls -> nothing to run; leaked XML in the text gets scrubbed.
    bot = _native_bot({"send_message": FakeTool("sent")})
    message = SimpleNamespace(guild=None, channel=SimpleNamespace(id=123))

    async def run():
        return await MaxwellBot._dispatch_tool_calls(
            bot, message, "hello <tool:send_message>leaked</tool:send_message> world"
        )

    cleaned, tool_results = asyncio.run(run())
    assert tool_results == []
    assert "leaked" not in cleaned
    assert "hello" in cleaned and "world" in cleaned


def test_tool_prompt_filters_discord_only_tools_for_telegram():
    bot = SimpleNamespace(
        _tool_breaker=ToolCircuitBreaker(failure_threshold=999, recovery_seconds=0),
        _control={"tools_enabled": True, "disabled_tools": [], "native_tool_calls": False},
        tools={"send_file": FakeTool("sent"), "react": FakeTool("Reacted")},
    )

    prompt = MaxwellBot._tool_system_prompt(bot, "telegram")

    assert "send_file:" in prompt
    assert "react:" not in prompt


def test_tool_prompt_keeps_discord_tools_for_discord():
    bot = SimpleNamespace(
        _tool_breaker=ToolCircuitBreaker(failure_threshold=999, recovery_seconds=0),
        _control={"tools_enabled": True, "disabled_tools": [], "native_tool_calls": False},
        tools={"send_file": FakeTool("sent"), "react": FakeTool("Reacted")},
    )

    prompt = MaxwellBot._tool_system_prompt(bot, "discord")

    assert "send_file:" in prompt
    assert "react:" in prompt


def test_tool_prompt_describes_reasoning_inside_tool_calls():
    # No more reasoning_log tool — the prompt must tell the model reasoning
    # rides inside each tool's `reasoning` param, and chat goes via send_message.
    bot = SimpleNamespace(
        _tool_breaker=ToolCircuitBreaker(failure_threshold=999, recovery_seconds=0),
        _control={"tools_enabled": True, "disabled_tools": [], "native_tool_calls": True},
        tools={"send_message": FakeTool("sent")},
    )

    prompt = MaxwellBot._tool_system_prompt(bot, "discord")

    assert "reasoning_log" not in prompt.lower()
    assert "reasoning" in prompt.lower()
    assert "send_message" in prompt
    # native-only: must NOT teach the XML tool-call FORM (the lone mention of
    # <tool:name> is the "do not invent" warning, which is fine — assert the
    # instructional FORM/Examples block is gone instead).
    assert "TOOL CALL FORMAT" not in prompt
    assert "<tool:send_file>" not in prompt


def test_ensure_reasoning_trace_backfills_only_when_no_tool_ran():
    # New contract: per-tool reasoning is recorded by record_reasoning during
    # dispatch. _ensure_reasoning_trace only fires for the pure-text fallback
    # (no tools ran at all -> tool_results empty). Use the backfill tool.
    from bot_tools import ReasoningLogTool

    class FakeBot:
        def __init__(self):
            self.traces = []

        async def _record_llm_trace(self, message, payload):
            self.traces.append(payload)

    bot = FakeBot()
    bot._reasoning_backfill = ReasoningLogTool(bot=bot)
    message = SimpleNamespace()

    async def run():
        await MaxwellBot._ensure_reasoning_trace(
            bot, message, [], "hi", "reply"
        )

    asyncio.run(run())
    assert len(bot.traces) == 1
    t = bot.traces[0]
    assert t["intent"] == "forced_trace"
    assert t["decision"] == "reply"
    assert "without any tool call" in t["thoughts"]
    assert t["data"]["response_preview"] == "hi"


def test_ensure_reasoning_trace_skips_when_tools_ran():
    # If tools ran this turn, reasoning was already recorded per-call -> no backfill.
    from bot_tools import ReasoningLogTool

    class FakeBot:
        def __init__(self):
            self.traces = []

        async def _record_llm_trace(self, message, payload):
            self.traces.append(payload)

    bot = FakeBot()
    bot._reasoning_backfill = ReasoningLogTool(bot=bot)
    message = SimpleNamespace()

    async def run():
        await MaxwellBot._ensure_reasoning_trace(
            bot,
            message,
            ["Tool send_message: __MESSAGE_SENT__ Sent 2 chars"],
            "hi",
            "send_message",
        )

    asyncio.run(run())
    assert bot.traces == []


def test_build_messages_caps_tool_history_outside_recent_count():
    memory = FakeMemory(
        [
            {
                "author": "Tool",
                "content": "Called search_messages with {} -> tool 1",
                "is_tool": True,
            },
            {
                "author": "Tool",
                "content": "Called search_messages with {} -> tool 2",
                "is_tool": True,
            },
            {
                "author": "Tool",
                "content": "Called search_messages with {} -> tool 3",
                "is_tool": True,
            },
            {
                "author": "Tool",
                "content": "Called search_messages with {} -> tool 4",
                "is_tool": True,
            },
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
        _recent_users={},
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


def test_shell_tool_results_trigger_followup():
    assert _tool_results_need_followup(
        ["Tool shell: __SHELL_SENT__\n$ date\nSat May 23"]
    )


def test_telegram_html_renders_code_blocks():
    rendered = _telegram_html("before\n```ansi\n$ whoami\nmaxwell\n```\nafter <ok>")

    assert "before" in rendered
    assert '<pre><code class="language-ansi">$ whoami\nmaxwell</code></pre>' in rendered
    assert "after &lt;ok&gt;" in rendered


def test_telegram_audio_turn_uses_stable_latest_message_label():
    assert (
        _telegram_latest_message_label("", has_media=True) == "[audio message attached]"
    )
    assert (
        _telegram_latest_message_label("make an image", has_media=True)
        == "make an image"
    )


def test_telegram_tool_followup_keeps_audio_turn_context_available():
    instruction = _telegram_tool_followup_instruction(has_original_media=True)

    assert "Original media isn't reattached here" in instruction
    assert "send_message" in instruction
    # native-only: no XML tag forms in the followup instruction
    assert "<tool:send_message>" not in instruction


def test_telegram_tool_followup_without_media_does_not_claim_audio_context():
    instruction = _telegram_tool_followup_instruction(has_original_media=False)

    assert "No original media is attached" in instruction
    assert "Original media isn't reattached here" not in instruction


def test_no_response_tool_results_do_not_trigger_followup():
    assert not _tool_results_need_followup(["Tool no_response: __NO_RESPONSE__"])


def test_reasoning_log_with_send_message_does_not_trigger_followup():
    assert not _tool_results_need_followup(
        [
            "Tool reasoning_log: __REASONING_RECORDED__",
            "Tool send_message: __MESSAGE_SENT__ Sent 10 chars",
        ]
    )


def test_tool_prompt_keeps_reasoning_plain_text_rule():
    bot = SimpleNamespace(
        _tool_breaker=ToolCircuitBreaker(failure_threshold=999, recovery_seconds=0),
        _control={"tools_enabled": True, "disabled_tools": [], "native_tool_calls": True},
        tools={"send_message": FakeTool("sent")},
    )

    prompt = MaxwellBot._tool_system_prompt(bot, "discord")

    assert "plain text" in prompt.lower()
    # reasoning is plain text only — no nested tags / JSON / <thoughts>
    assert "no xml" in prompt.lower() or "no nested" in prompt.lower() or "plain text only" in prompt.lower()


def test_tool_prompt_native_mode_no_xml_instructions():
    bot = SimpleNamespace(
        _tool_breaker=ToolCircuitBreaker(failure_threshold=999, recovery_seconds=0),
        _control={"tools_enabled": True, "disabled_tools": [], "native_tool_calls": True},
        tools={"send_message": FakeTool("sent"), "react": FakeTool("Reacted")},
    )

    prompt = MaxwellBot._tool_system_prompt(bot, "discord")

    assert "native function/tool calling" in prompt.lower()
    assert "XML text tags only" not in prompt
    assert "send_message:" in prompt


def test_prompt_budget_trims_large_background_blocks():
    bot = SimpleNamespace(
        _tool_breaker=ToolCircuitBreaker(failure_threshold=999, recovery_seconds=0),
        _control={"prompt_context_budget": 10000},
    )
    messages = [
        {"role": "system", "content": "core"},
        {"role": "system", "content": "x" * 50000},
        {"role": "user", "content": "latest"},
    ]

    trimmed = MaxwellBot._apply_prompt_budget(bot, messages)

    assert sum(MaxwellBot._message_content_chars(m) for m in trimmed) <= 10000
    assert "prompt budget trimmed" in trimmed[1]["content"]


def test_shared_fact_relevance_filters_broad_vague_context():
    assert (
        MaxwellBot._shared_fact_relevant(
            "lol", {"scope": "guild:1", "content": "project alpha uses postgres"}
        )
        is False
    )
    assert (
        MaxwellBot._shared_fact_relevant(
            "what database does project alpha use",
            {"scope": "guild:1", "content": "project alpha uses postgres"},
        )
        is True
    )
    assert (
        MaxwellBot._shared_fact_relevant(
            "lol", {"scope": "user:1", "content": "likes terse replies"}
        )
        is True
    )


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
        _recent_users={},
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
        # Current style instruction uses "Limit: N chars." (the formatting bound).
        # The old "don't force it into one-liners" phrase was removed during prompt refactor.
        assert "Limit:" in system_content or "chars." in system_content

    asyncio.run(run())


def test_cached_media_context_requires_latest_visual_reference():
    message = SimpleNamespace(reference=None)

    assert not MaxwellBot._should_use_cached_media_context(message, "lol ok")
    assert MaxwellBot._should_use_cached_media_context(message, "what's in that image?")
    assert MaxwellBot._should_use_cached_media_context(message, "look at this")


def test_cached_media_context_allowed_for_attachment_reply():
    replied = SimpleNamespace(
        id=123, attachments=[SimpleNamespace(filename="old.png")], embeds=[]
    )
    message = SimpleNamespace(reference=SimpleNamespace(resolved=replied))

    assert MaxwellBot._should_use_cached_media_context(message, "what is that")


def test_cached_media_context_can_filter_by_reply_message_id():
    bot = SimpleNamespace(
        _tool_breaker=ToolCircuitBreaker(failure_threshold=999, recovery_seconds=0),
        _media_context={
            "c": [
                {
                    "b64": "old",
                    "mime_type": "image/png",
                    "filename": "old.png",
                    "message_id": 1,
                },
                {
                    "b64": "right",
                    "mime_type": "image/png",
                    "filename": "right.png",
                    "message_id": 2,
                },
            ]
        },
    )

    media = MaxwellBot._get_media_context(bot, "c", message_id=2)

    assert [item["filename"] for item in media] == ["right.png"]


def test_current_image_does_not_mix_cached_media_without_prior_reference():
    assert not MaxwellBot._should_mix_cached_with_current("look at this")
    assert MaxwellBot._should_mix_cached_with_current(
        "compare this with the previous image"
    )


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
