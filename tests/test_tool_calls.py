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

from bot import strip_tool_payload_leaks
from tool_registry import extract_reasoning, _sanitize_reasoning, record_reasoning
from tool_schemas import REASONING_PARAM, build_openai_tools


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
