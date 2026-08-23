"""Recovery of tool calls a model wrote as visible text instead of tool_calls.

Maxwell only ever advertises native ``tools=``, but models keep answering in
their own text dialect anyway. Before recovery both outcomes were wrong: a
dialect the scrubber recognized was deleted (the reply vanished and the turn
went silent) and one it did not recognize was posted to the channel as a raw
parameter dump — the "reasoning … content … reply true" the user kept seeing
in chat.

These tests pin every dialect we have actually observed, plus the negative
cases that must NOT be mistaken for a tool call.
"""

import json
from bot import KNOWN_TOOL_NAMES, MaxwellBot
from tool_schemas import recover_text_tool_calls


def _recover(text):
    calls, leftover = recover_text_tool_calls(text, KNOWN_TOOL_NAMES)
    decoded = [
        (c["function"]["name"], json.loads(c["function"]["arguments"])) for c in calls
    ]
    return decoded, leftover


# ---- dialects that must be recovered ----


def test_recovers_glm_arg_key_block():
    calls, leftover = _recover(
        "<tool_call>send_message\n"
        "<arg_key>reasoning</arg_key><arg_value>answering directly</arg_value>\n"
        "<arg_key>content</arg_key><arg_value>nadie dijo eso</arg_value>\n"
        "<arg_key>reply</arg_key><arg_value>true</arg_value>\n"
        "</tool_call>"
    )
    assert calls == [
        (
            "send_message",
            {
                "reasoning": "answering directly",
                "content": "nadie dijo eso",
                "reply": True,
            },
        )
    ]
    assert leftover == ""


def test_recovers_qwen_function_equals_block():
    calls, leftover = _recover(
        "<function=send_message>\n"
        "<parameter=reasoning>\nanswering\n</parameter>\n"
        "<parameter=content>\nhola\n</parameter>\n"
        "<parameter=reply>\nfalse\n</parameter>\n"
        "</function>"
    )
    assert calls == [
        ("send_message", {"reasoning": "answering", "content": "hola", "reply": False})
    ]
    assert leftover == ""


def test_recovers_dsml_invoke_block():
    calls, _ = _recover(
        '<｜｜DSML｜｜invoke name="send_message">'
        '<｜｜DSML｜｜parameter name="reasoning" string="true">why</｜｜DSML｜｜parameter>'
        '<｜｜DSML｜｜parameter name="content" string="true">hola</｜｜DSML｜｜parameter>'
        "</｜｜DSML｜｜invoke>"
    )
    assert calls == [("send_message", {"reasoning": "why", "content": "hola"})]


def test_recovers_bare_arg_pair_dump():
    calls, leftover = _recover(
        "send_message<arg>reasoning</arg>why</arg><arg>content</arg>ну ладно</arg>"
    )
    assert calls == [("send_message", {"reasoning": "why", "content": "ну ладно"})]
    assert leftover == ""


def test_recovers_bare_json_call_and_keeps_surrounding_prose():
    calls, leftover = _recover(
        'sure thing\n{"name":"react","arguments":{"reasoning":"funny","emoji":"😂"}}'
    )
    assert calls == [("react", {"reasoning": "funny", "emoji": "😂"})]
    assert leftover == "sure thing"


def test_recovers_harmony_channel_call_without_leaving_markers():
    calls, leftover = _recover(
        "<|start|>assistant<|channel|>commentary to=functions.web_search "
        '<|constrain|>json<|message|>{"reasoning":"current info","query":"maxwell",'
        '"max_results":"5"}'
    )
    assert calls == [
        ("web_search", {"reasoning": "current info", "query": "maxwell", "max_results": 5})
    ]
    assert leftover == ""


def test_recovers_tagless_key_value_ladder():
    """The exact shape users saw in chat once a renderer ate the tags."""
    calls, leftover = _recover(
        "reasoning\n"
        "She is clarifying she didn't send the image.\n"
        "content\n"
        "nadie dijo que mandaste la foto\n"
        "reply\n"
        "true"
    )
    assert calls == [
        (
            "send_message",
            {
                "reasoning": "She is clarifying she didn't send the image.",
                "content": "nadie dijo que mandaste la foto",
                "reply": True,
            },
        )
    ]
    assert leftover == ""


def test_recovers_several_calls_in_declared_order():
    calls, _ = _recover(
        "<tool_call>web_search\n"
        "<arg_key>query</arg_key><arg_value>weather</arg_value></tool_call>\n"
        "<tool_call>send_message\n"
        "<arg_key>content</arg_key><arg_value>checking…</arg_value></tool_call>"
    )
    assert [name for name, _ in calls] == ["web_search", "send_message"]


# ---- values arrive as text and must be cast to the declared type ----


def test_reply_false_is_a_bool_not_a_truthy_string():
    calls, _ = _recover(
        "<tool_call>send_message<arg_key>content</arg_key><arg_value>hi</arg_value>"
        "<arg_key>reply</arg_key><arg_value>false</arg_value></tool_call>"
    )
    assert calls[0][1]["reply"] is False


def test_integer_params_are_cast():
    calls, _ = _recover(
        "<tool_call>search_messages<arg_key>query</arg_key><arg_value>cats</arg_value>"
        "<arg_key>limit</arg_key><arg_value>3</arg_value></tool_call>"
    )
    assert calls[0][1]["limit"] == 3


# ---- negatives: ordinary text must survive untouched ----


def test_plain_reply_is_left_alone():
    text = "nadie dijo que mandaste la foto, la imagen es de walter"
    assert _recover(text) == ([], text)


def test_json_in_prose_is_not_a_tool_call():
    text = 'the config is {"name": "bob"} basically'
    assert _recover(text) == ([], text)


def test_unknown_tool_name_is_not_recovered():
    text = "<function=frobnicate><parameter=x>1</parameter></function>"
    assert _recover(text) == ([], text)


def test_fenced_example_is_not_dispatched():
    text = (
        "the format is:\n```\n<tool_call>send_message\n"
        "<arg_key>content</arg_key><arg_value>hi</arg_value>\n</tool_call>\n```"
    )
    assert _recover(text) == ([], text)


def test_message_starting_with_a_param_word_is_not_a_ladder():
    """A tagless ladder needs bare key lines, not a word used in a sentence."""
    text = "content is king, reply when you can"
    assert _recover(text) == ([], text)


def test_tagless_ladder_needs_content():
    text = "reasoning\nbecause I felt like it"
    assert _recover(text) == ([], text)


# ---- wiring: the bot prefers native calls and only then recovers ----


class _Bot:
    """Minimal stand-in carrying the real method under test."""

    def __init__(self, tools_enabled=True):
        self._control = {"tools_enabled": tools_enabled}

    _recover_text_tool_calls = MaxwellBot._recover_text_tool_calls


def test_recovery_returns_provider_shaped_calls():
    bot = _Bot()
    calls, text = bot._recover_text_tool_calls(
        "<tool_call>send_message<arg_key>content</arg_key>"
        "<arg_value>hola</arg_value></tool_call>"
    )
    assert [c["function"]["name"] for c in calls] == ["send_message"]
    assert json.loads(calls[0]["function"]["arguments"]) == {"content": "hola"}
    assert text == ""


def test_recovery_leaves_a_plain_reply_untouched():
    bot = _Bot()
    assert bot._recover_text_tool_calls("hola que tal") == ([], "hola que tal")


def test_recovery_is_skipped_when_tools_are_disabled():
    bot = _Bot(tools_enabled=False)
    text = "<tool_call>send_message<arg_key>content</arg_key><arg_value>hola</arg_value></tool_call>"
    assert bot._recover_text_tool_calls(text) == ([], text)
