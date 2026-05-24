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


def test_collect_tool_calls_accepts_shell_command_subtag():
    calls = collect_tool_calls('<tool:shell><command>neofetch</command></tool:shell>', TOOLS | {"shell"})

    assert [(name, params) for _start, _end, name, params in calls] == [("shell", {"command": "neofetch"})]


def test_collect_tool_calls_does_not_execute_nested_tool_tags():
    response = '<tool:reasoning_log><thoughts><tool:shell command="neofetch" /><tool:send_message>bad</tool:send_message></thoughts></tool:reasoning_log>'
    calls = collect_tool_calls(response, TOOLS | {"shell"})

    assert [(name, params) for _start, _end, name, params in calls] == [
        (
            "reasoning_log",
            {"thoughts": '<tool:shell command="neofetch" /><tool:send_message>bad</tool:send_message>'},
        )
    ]


def test_collect_tool_calls_ignores_json_reasoning_with_tool_tags():
    response = '{"thoughts":"<tool:shell command=\\"neofetch\\" />","intent":"reply"}'
    calls = collect_tool_calls(response, TOOLS | {"shell"})

    assert calls == []


def test_collect_tool_calls_does_not_scan_inside_tool_body_for_terminal_call():
    response = '<tool:reasoning_log>run shell then <tool:no_response /></tool:reasoning_log>'
    calls = collect_tool_calls(response, TOOLS | {"no_response"})

    assert [(name, params) for _start, _end, name, params in calls] == [
        ("reasoning_log", {"thoughts": "run shell then <tool:no_response />"})
    ]


def test_collect_tool_calls_accepts_shorthand_tool_close_tags():
    response = """<tool:reasoning_log>
thinking
</tool><tool:send_file>
<filename>bot.py</filename>
<content>await ctx.reply(response.text)
except Exception as e:
    await ctx.reply(f"Error querying AI: {e}")</content>
</tool><tool:send_message>Here you go</tool>"""
    calls = collect_tool_calls(response, TOOLS | {"send_file"})

    assert [(name, params) for _start, _end, name, params in calls] == [
        ("reasoning_log", {"thoughts": "thinking"}),
        (
            "send_file",
            {
                "filename": "bot.py",
                "content": 'await ctx.reply(response.text)\nexcept Exception as e:\n    await ctx.reply(f"Error querying AI: {e}")',
            },
        ),
        ("send_message", {"content": "Here you go"}),
    ]


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


def test_strip_tool_payload_leaks_keeps_normal_xml():
    text = '<div class="card">hello</div>\nactual reply'
    assert strip_tool_payload_leaks(text) == text


def test_strip_tool_payload_leaks_removes_shorthand_tool_blocks():
    text = '<tool:send_file><filename>bot.py</filename><content>print("hi")</content></tool>\nactual reply'
    assert strip_tool_payload_leaks(text) == "actual reply"
