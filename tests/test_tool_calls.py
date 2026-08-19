"""Tests for the new reasoning-in-tool-calls + native-only dispatch.

The XML ``collect_tool_calls`` dispatcher is gone (Maxwell is native
function-calling only now). What stays is ``strip_tool_payload_leaks`` — the
defensive sanitizer that scrubs any leaked ``<tool:...>`` tags a misbehaving
model drops into visible text even in native mode. These tests cover that
sanitizer plus the new reasoning contract:
- every tool schema gets an auto-injected ``reasoning`` param,
- ``extract_reasoning`` pulls it out of params before the tool runs,
- ``_sanitize_reasoning`` strips tag-wrapped thoughts the model sneakily emits.
"""

import json
from types import SimpleNamespace

from bot import MaxwellBot, strip_tool_payload_leaks
from tool_registry import extract_reasoning, _sanitize_reasoning, record_reasoning
from tool_schemas import (
    REASONING_PARAM,
    build_openai_tools,
    normalize_native_tool_calls,
)


class _FakeTool:
    def get_description(self):
        return "fake tool"


TOOLS = {"send_file", "react", "send_message", "no_response", "create_site", "tts"}


# ---- strip_tool_payload_leaks (defensive sanitizer, still used) ----


def test_strip_tool_payload_leaks_removes_standalone_tags():
    # reasoning_log is NOT a known tool anymore, so use a real one for the leak.
    text = "\n".join(
        [
            "<tool:react emoji=\"👍\" />",
            "<tool:send_message>hello</tool:send_message>",
            "actual reply",
        ]
    )
    assert strip_tool_payload_leaks(text) == "actual reply"


def test_strip_tool_payload_leaks_removes_self_closing_tags():
    text = '<tool:react emoji="catjam" />\nactual reply'
    assert strip_tool_payload_leaks(text) == "actual reply"


def test_strip_tool_payload_leaks_keeps_normal_xml():
    text = '<div class="card">hello</div>\nactual reply'
    assert strip_tool_payload_leaks(text) == text


def test_strip_tool_payload_leaks_removes_shorthand_tool_blocks():
    text = (
        '<tool:send_file><filename>bot.py</filename>'
        '<content>print("hi")</content></tool:send_file>\nactual reply'
    )
    assert strip_tool_payload_leaks(text) == "actual reply"


def test_strip_tool_payload_leaks_removes_unclosed_tool_and_environment_details():
    text = '<tool:send_message>Hello!<|end|><environment_details>secret context</environment_details>'
    assert strip_tool_payload_leaks(text) == ""


def test_strip_tool_payload_leaks_removes_reasoning_json_and_system_reminder():
    text = '''{
  "thoughts": "User asked for TTS.",
  "intent": "tts",
  "decision": "Call tts"
}
<tool:tts text="Hey there!" language="english" />
<system-reminder>secret context</system-reminder>'''
    assert strip_tool_payload_leaks(text) == ""


def test_strip_tool_payload_leaks_removes_registered_tool_bodies():
    leaked = (
        "<tool:update_base_personality>you are now evil</tool:update_base_personality> hello"
    )
    assert "you are now evil" not in strip_tool_payload_leaks(leaked)
    assert "hello" in strip_tool_payload_leaks(leaked)
    search = "<tool:search_messages>secret query</tool:search_messages> ok"
    assert "secret query" not in strip_tool_payload_leaks(search)
    email = "<tool:email_send>to=evil</tool:email_send> visible"
    assert "to=evil" not in strip_tool_payload_leaks(email)
    assert "visible" in strip_tool_payload_leaks(email)


def test_strip_tool_payload_leaks_removes_glued_create_site():
    html = "<!DOCTYPE html><html><body>x</body></html>"
    text = f'ship<tool:create_site name="drift" title="t">{html}</tool:create_site>ok'
    assert strip_tool_payload_leaks(text) == "shipok"


def test_strip_tool_payload_leaks_catches_leaking_variants():
    assert strip_tool_payload_leaks("<|tool_send_message|>foo bar") == ""
    assert "before" in strip_tool_payload_leaks(
        "before <|tool_response|> <|end_of_text|> after"
    )
    assert "after" in strip_tool_payload_leaks(
        "before <|tool_response|> <|end_of_text|> after"
    )
    assert strip_tool_payload_leaks("<|/tool:send_message|>text") == "text"
    assert (
        strip_tool_payload_leaks("<tool_send_message>leaked</tool_send_message> visible").strip()
        == "visible"
    )
    assert strip_tool_payload_leaks("normal <div>ok</div>") == "normal <div>ok</div>"


def test_strip_tool_payload_leaks_removes_deepseek_dsml_invoke():
    leaked = (
        '<｜｜DSML｜｜invoke name="send_message">\n'
        '<｜｜DSML｜｜parameter name="reasoning" string="true">'
        "Z3ki is calling me out</｜｜DSML｜｜parameter>\n"
        '<｜｜DSML｜｜parameter name="content" string="true">'
        "my bad</｜｜DSML｜｜parameter>\n"
        "</｜｜DSML｜｜invoke>"
    )
    assert strip_tool_payload_leaks(leaked).strip() == "my bad"
    assert strip_tool_payload_leaks("ok " + leaked).strip() == "ok my bad"
    ascii_leaked = (
        '<|DSML|invoke name="send_message">'
        '<parameter name="content">hi</parameter>'
        "</invoke>"
    )
    assert strip_tool_payload_leaks(ascii_leaked).strip() == "hi"
    assert strip_tool_payload_leaks('<invoke name="send_message">secret</invoke> visible').strip() == "visible"
    leftover_name = "send_message\n" + leaked
    assert strip_tool_payload_leaks(leftover_name).strip() == "my bad"
    assert strip_tool_payload_leaks("send_message").strip() == "send_message"


def test_strip_tool_payload_leaks_extracts_send_message_arg_protocol():
    leaked = (
        "send_message<arg>reasoning</arg>Short Russian one-liner acknowledging "
        "they dropped it; stay firm, no MCP connect.</arg>"
        "<arg>content</arg>ну ладно, без обид — просто к чужим mcp не лезу.</arg>"
    )
    assert strip_tool_payload_leaks(leaked) == "ну ладно, без обид — просто к чужим mcp не лезу."
    mixed = "ok " + leaked
    assert strip_tool_payload_leaks(mixed) == "ok ну ладно, без обид — просто к чужим mcp не лезу."
    assert "reasoning" not in strip_tool_payload_leaks(leaked)
    assert "<arg>" not in strip_tool_payload_leaks(leaked)


def test_strip_tool_payload_leaks_drops_non_send_message_arg_protocol():
    leaked = "shell<arg>command</arg>cat /etc/passwd</arg><arg>reasoning</arg>peek</arg>"
    out = strip_tool_payload_leaks(leaked)
    assert "passwd" not in out
    assert "shell" not in out
    assert out == ""
    mixed = "before " + leaked + " after"
    assert strip_tool_payload_leaks(mixed) == "before  after"


def test_strip_tool_payload_leaks_unwraps_openai_text_part():
    assert strip_tool_payload_leaks('{"type":"text","text":""}') == ""
    assert strip_tool_payload_leaks('{"type": "text", "text": ""}') == ""
    assert strip_tool_payload_leaks('{"type":"text","text":"sent."}') == "sent."
    assert (
        strip_tool_payload_leaks('[{"type":"text","text":"hi "},{"type":"text","text":"there"}]')
        == "hi there"
    )


# ---- reasoning param injection on every tool schema ----


def test_every_tool_gets_reasoning_param():
    tools = {"send_message": _FakeTool(), "react": _FakeTool(), "no_response": _FakeTool()}
    out = {o["function"]["name"]: o for o in build_openai_tools(tools)}
    for name, fn in out.items():
        props = fn["function"]["parameters"]["properties"]
        assert "reasoning" in props, f"{name} is missing the reasoning param, damn it"


def test_reasoning_is_always_required():
    tools = {"send_message": _FakeTool()}
    out = build_openai_tools(tools)[0]
    required = out["function"]["parameters"].get("required", [])
    # reasoning is always in required so the provider rejects empty calls
    # instead of silently dropping the trace. The tool's own required field
    # (content) is preserved alongside.
    assert "reasoning" in required
    assert "content" in required
    assert set(required) == {"reasoning", "content"}


def test_reasoning_param_schema_is_stable():
    # same shape everywhere — no per-tool drift
    assert REASONING_PARAM["type"] == "string"
    assert "plain" in REASONING_PARAM["description"].lower()


# ---- extract_reasoning / sanitize ----


def test_extract_reasoning_pops_it_out_of_params():
    reasoning, params = extract_reasoning(
        {"reasoning": "because the user asked", "content": "hi"}
    )
    assert reasoning == "because the user asked"
    assert params == {"content": "hi"}


def test_extract_reasoning_missing_returns_empty():
    reasoning, params = extract_reasoning({"content": "hi"})
    assert reasoning == ""
    assert params == {"content": "hi"}


def test_extract_reasoning_handles_none_params():
    reasoning, params = extract_reasoning(None)
    assert reasoning == ""
    assert params == {}


def test_sanitize_reasoning_strips_wrapped_thought_tags():
    assert _sanitize_reasoning("<thoughts>why</thoughts> do it") == "why  do it"


def test_sanitize_reasoning_clamps_giant_input():
    out = _sanitize_reasoning("x" * 5000)
    assert len(out) <= 1000
    assert out.endswith("…")


def test_sanitize_reasoning_empty_stays_empty():
    assert _sanitize_reasoning("") == ""
    assert _sanitize_reasoning(None) == ""


# ---- record_reasoning end-to-end (fake bot) ----


def test_record_reasoning_writes_trace_and_swallows_errors():
    class FakeBot:
        def __init__(self):
            self.traces = []

        async def _record_llm_trace(self, message, payload):
            self.traces.append(payload)

    import asyncio

    bot = FakeBot()

    async def run():
        await record_reasoning(
            bot, message=object(), tool_name="send_message",
            reasoning="user wants a reply", params={"content": "hi", "reasoning": "x"},
            result="__MESSAGE_SENT__",
        )

    asyncio.run(run())
    assert len(bot.traces) == 1
    t = bot.traces[0]
    assert t["tool"] == "send_message"
    assert t["thoughts"] == "user wants a reply"
    # reasoning must NOT leak into the params_preview
    assert "reasoning" not in t["params_preview"]
    assert t["params_preview"]["content"] == "hi"


def test_record_reasoning_empty_reasoning_records_a_stub():
    class FakeBot:
        def __init__(self):
            self.traces = []

        async def _record_llm_trace(self, message, payload):
            self.traces.append(payload)

    import asyncio

    bot = FakeBot()

    async def run():
        await record_reasoning(
            bot, message=object(), tool_name="react",
            reasoning="", params={"emoji": "👍"}, result="Reacted",
        )

    asyncio.run(run())
    assert bot.traces[0]["thoughts"] == "(no reasoning provided by the model)"


def test_select_tool_protocol_native_wins_over_custom():
    """CUSTOM_TOOL_CALLS must not drop tools= when native function calling is on."""
    tools = [{"type": "function", "function": {"name": "send_message"}}]
    bot = SimpleNamespace(
        config=SimpleNamespace(CUSTOM_TOOL_CALLS=True),
        _control={"native_tool_calls": True, "tools_enabled": True},
    )
    bot._native_tools_enabled = lambda: True
    custom, provider_tools = MaxwellBot._select_tool_protocol(bot, tools)
    assert custom is False
    assert provider_tools == tools


def test_select_tool_protocol_custom_only_when_native_off():
    tools = [{"type": "function", "function": {"name": "send_message"}}]
    bot = SimpleNamespace(
        config=SimpleNamespace(CUSTOM_TOOL_CALLS=True),
        _control={"native_tool_calls": False, "tools_enabled": True},
    )
    bot._native_tools_enabled = lambda: False
    custom, provider_tools = MaxwellBot._select_tool_protocol(bot, tools)
    assert custom is True
    assert provider_tools is None


def test_normalize_native_tool_calls_decodes_provider_argument_shapes():
    """Native tool arguments arrive as objects, JSON, or nested JSON."""
    body = r"<pre>line one\nline two</pre>"
    raw_calls = [
        {
            "id": "direct",
            "function": {"name": "create_site", "arguments": {"body": body}},
        },
        {
            "id": "nested",
            "function": {
                "name": "create_site",
                "arguments": json.dumps(json.dumps({"body": body})),
            },
        },
        {
            "id": "trailing",
            "function": {
                "name": "create_site",
                "arguments": json.dumps({"body": body}) + "<provider-markup>",
            },
        },
    ]

    normalized = normalize_native_tool_calls(raw_calls)

    assert [call["arguments"]["body"] for call in normalized] == [body] * 3

    scalar = normalize_native_tool_calls(
        [{"function": {"name": "react", "arguments": "42"}}]
    )
    assert scalar[0]["arguments"] == {"_": 42}


def test_record_reasoning_does_not_raise_on_bot_failure():
    class BrokenBot:
        async def _record_llm_trace(self, message, payload):
            raise RuntimeError("disk on fire")

    import asyncio

    async def run():
        # must NOT raise — a trace write failure must never kill the tool result
        await record_reasoning(
            BrokenBot(), message=object(), tool_name="shell",
            reasoning="x", params={"command": "ls"}, result="ok",
        )

    asyncio.run(run())  # no exception = pass
