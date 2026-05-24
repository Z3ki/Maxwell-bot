from bot import collect_tool_calls, strip_tool_payload_leaks


TOOLS = {"react", "web_search", "create_poll", "send_message", "reasoning_log", "create_site"}


def test_collect_tool_calls_accepts_self_closing_namespace_tags():
    calls = collect_tool_calls('<tool:react emoji="catjam" />', TOOLS)

    assert [(name, params) for _start, _end, name, params in calls] == [("react", {"emoji": "catjam"})]


def test_collect_tool_calls_accepts_self_closing_plain_tags():
    calls = collect_tool_calls('<react emoji="catjam" />', TOOLS)

    assert [(name, params) for _start, _end, name, params in calls] == [("react", {"emoji": "catjam"})]


def test_collect_tool_calls_accepts_tags_with_sub_elements():
    calls = collect_tool_calls(
        '<tool:web_search><query>openrouter</query></tool:web_search>',
        TOOLS
    )

    assert [(name, params) for _start, _end, name, params in calls] == [("web_search", {"query": "openrouter"})]


def test_collect_tool_calls_accepts_tags_with_attributes_and_sub_elements():
    calls = collect_tool_calls(
        '<tool:web_search engine="google"><query>openrouter</query></tool:web_search>',
        TOOLS
    )

    assert [(name, params) for _start, _end, name, params in calls] == [("web_search", {"engine": "google", "query": "openrouter"})]


def test_collect_tool_calls_accepts_default_fallback_parameter():
    calls = collect_tool_calls('<tool:send_message>hello world</tool:send_message>', TOOLS)

    assert [(name, params) for _start, _end, name, params in calls] == [("send_message", {"content": "hello world"})]


def test_collect_tool_calls_ignores_disabled_tools():
    calls = collect_tool_calls('<tool:react emoji="catjam" />', TOOLS, {"react"})

    assert calls == []


def test_collect_tool_calls_can_include_disabled_for_dispatcher_stripping():
    calls = collect_tool_calls(
        '<tool:react emoji="catjam" />',
        TOOLS,
        {"react"},
        include_disabled=True,
    )

    assert [(name, params) for _start, _end, name, params in calls] == [("react", {"emoji": "catjam"})]


def test_collect_tool_calls_accepts_multiple_tags():
    response = '\n'.join([
        '<tool:reasoning_log>thinking...</tool:reasoning_log>',
        '<tool:send_message>hello</tool:send_message>',
    ])
    calls = collect_tool_calls(response, TOOLS)

    assert [(name, params) for _start, _end, name, params in calls] == [
        ("reasoning_log", {"thoughts": "thinking..."}),
        ("send_message", {"content": "hello"}),
    ]


def test_strip_tool_payload_leaks_removes_standalone_tags():
    text = '\n'.join([
        '<tool:reasoning_log>thinking...</tool:reasoning_log>',
        '<tool:send_message>hello</tool:send_message>',
        "actual reply",
    ])
    assert strip_tool_payload_leaks(text) == "actual reply"


def test_strip_tool_payload_leaks_removes_self_closing_tags():
    text = '<tool:react emoji="catjam" />\nactual reply'
    assert strip_tool_payload_leaks(text) == "actual reply"
