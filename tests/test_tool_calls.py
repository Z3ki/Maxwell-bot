from bot import collect_tool_calls, strip_tool_payload_leaks


TOOLS = {"react", "web_search", "create_poll", "send_message", "reasoning_log", "create_site", "tts"}


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


def test_collect_tool_calls_accepts_unclosed_terminal_before_end_marker():
    response = '<tool:send_message>Hello!<|end|><environment_details>secret context</environment_details>'
    calls = collect_tool_calls(response, TOOLS)

    assert [(name, params) for _start, _end, name, params in calls] == [("send_message", {"content": "Hello!"})]


def test_collect_tool_calls_accepts_pipe_tool_format():
    calls = collect_tool_calls("<|tool:send_message>Hello! What's up?<|end|>", TOOLS)

    assert [(name, params) for _start, _end, name, params in calls] == [
        ("send_message", {"content": "Hello! What's up?"})
    ]


def test_collect_tool_calls_accepts_pipe_tool_call_begin_format():
    calls = collect_tool_calls("<|tool_call_begin|>tts|>text=Test tts language=spanish<|tool_call_end|>", TOOLS)

    assert [(name, params) for _start, _end, name, params in calls] == [
        ("tts", {"text": "Test tts", "language": "spanish"})
    ]


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


def test_collect_tool_calls_accepts_leaking_tool_token_format():
    # Models sometimes emit <|tool_send_message|>text<|/tool_send_message|> or without close
    calls = collect_tool_calls("<|tool_send_message|>hello world<|/tool_send_message|>", TOOLS)
    assert [(name, params) for _start, _end, name, params in calls] == [
        ("send_message", {"content": "hello world"})
    ]

    calls2 = collect_tool_calls("<|tool_send_message|>just an emoji 🔥", TOOLS)
    assert [(name, params) for _start, _end, name, params in calls2] == [
        ("send_message", {"content": "just an emoji 🔥"})
    ]


def test_collect_tool_calls_accepts_tool_underscore_prefix():
    calls = collect_tool_calls("<tool_send_message>hi there</tool_send_message>", TOOLS)
    assert [(name, params) for _start, _end, name, params in calls] == [
        ("send_message", {"content": "hi there"})
    ]


def test_collect_tool_calls_accepts_glued_tool_tag_without_whitespace():
    """Production failure: models emit 'ship<tool:create_site ...>' with no space.

    The whitespace-before-'<' guard used to skip these, so create_site never ran
    and the HTML payload leaked into Discord as normal messages.
    """
    html = (
        "<!DOCTYPE html><html><head><title>X</title></head>"
        "<body><h1>hi</h1></body></html>"
    )
    response = (
        f'build the other two pages, then ship'
        f'<tool:create_site name="drift-pricing" title="drift — pricing">'
        f"{html}"
        f"</tool:create_site>"
        f"<tool:send_message>done</tool:send_message>"
    )
    calls = collect_tool_calls(response, TOOLS)
    by_name = {name: params for _s, _e, name, params in calls}
    assert "create_site" in by_name
    assert by_name["create_site"]["name"] == "drift-pricing"
    assert by_name["create_site"]["title"] == "drift — pricing"
    assert by_name["create_site"]["body"] == html
    assert by_name["send_message"]["content"] == "done"


def test_strip_tool_payload_leaks_removes_glued_create_site():
    html = "<!DOCTYPE html><html><body>x</body></html>"
    text = f'ship<tool:create_site name="drift" title="t">{html}</tool:create_site>ok'
    assert strip_tool_payload_leaks(text) == "shipok"


def test_create_site_html_body_keeps_full_document_with_title_and_body_tags():
    """HTML <title>/<body> must not be harvested as tool params or truncate the site."""
    html = (
        "<!DOCTYPE html>\n"
        "<html><head><title>Page Title</title></head>\n"
        "<body><h1>Hello</h1><p>name test</p></body></html>"
    )
    response = (
        f'<tool:create_site name="drift" title="My Startup">{html}</tool:create_site>'
    )
    calls = collect_tool_calls(response, TOOLS)
    assert len(calls) == 1
    _s, _e, name, params = calls[0]
    assert name == "create_site"
    assert params["name"] == "drift"
    assert params["title"] == "My Startup"  # attribute, not HTML <title>
    assert params["body"] == html


def test_create_site_structured_body_with_nested_html_body_tag():
    """Balanced matching: tool <body> may contain nested HTML <body> tags."""
    inner = "<!DOCTYPE html><html><body><p>hi</p></body></html>"
    response = (
        "<tool:create_site>"
        "<name>drift</name>"
        "<title>My Site</title>"
        f"<body>{inner}</body>"
        "</tool:create_site>"
    )
    calls = collect_tool_calls(response, TOOLS)
    assert len(calls) == 1
    params = calls[0][3]
    assert params["name"] == "drift"
    assert params["title"] == "My Site"
    assert params["body"] == inner


def test_create_site_leading_scalars_then_raw_html():
    response = (
        "<tool:create_site>"
        "<name>drift</name>"
        "<title>My Site</title>"
        "<!DOCTYPE html><html><body>hi</body></html>"
        "</tool:create_site>"
    )
    calls = collect_tool_calls(response, TOOLS)
    params = calls[0][3]
    assert params["name"] == "drift"
    assert params["title"] == "My Site"
    assert params["body"] == "<!DOCTYPE html><html><body>hi</body></html>"


def test_strip_tool_payload_leaks_catches_leaking_variants():
    # Pure tool token blocks (even malformed) get fully stripped like other payloads.
    assert strip_tool_payload_leaks("<|tool_send_message|>foo bar") == ""
    # Stray tokens in middle get removed (bodies after lone tokens may remain if not part of a full match)
    assert "before" in strip_tool_payload_leaks("before <|tool_response|> <|end_of_text|> after")
    assert "after" in strip_tool_payload_leaks("before <|tool_response|> <|end_of_text|> after")
    assert strip_tool_payload_leaks("<|/tool:send_message|>text") == "text"
    # XML malformed gets removed as full block (incl body) via range logic now that normalize+close work
    assert strip_tool_payload_leaks("<tool_send_message>leaked</tool_send_message> visible").strip() == "visible"
    assert strip_tool_payload_leaks("normal <div>ok</div>") == "normal <div>ok</div>"
