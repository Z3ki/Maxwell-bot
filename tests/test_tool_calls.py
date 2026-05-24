from bot import collect_tool_calls


TOOLS = {"react", "web_search", "create_poll"}


def test_collect_tool_calls_accepts_plain_json_tool_object():
    calls = collect_tool_calls('{"tool":"react","emoji":"catjam"}', TOOLS)

    assert [(name, params) for _start, _end, name, params in calls] == [("react", {"emoji": "catjam"})]


def test_collect_tool_calls_accepts_args_object():
    calls = collect_tool_calls('{"tool":"web_search","args":{"query":"openrouter"}}', TOOLS)

    assert [(name, params) for _start, _end, name, params in calls] == [("web_search", {"query": "openrouter"})]


def test_collect_tool_calls_accepts_tool_line_format():
    calls = collect_tool_calls('TOOL react {"emoji":"catjam"}', TOOLS)

    assert [(name, params) for _start, _end, name, params in calls] == [("react", {"emoji": "catjam"})]


def test_collect_tool_calls_keeps_legacy_bracket_format():
    calls = collect_tool_calls('[react]\n{"emoji":"catjam"}\n[/react]', TOOLS)

    assert [(name, params) for _start, _end, name, params in calls] == [("react", {"emoji": "catjam"})]


def test_collect_tool_calls_ignores_disabled_tools():
    calls = collect_tool_calls('{"tool":"react","emoji":"catjam"}', TOOLS, {"react"})

    assert calls == []

def test_collect_tool_calls_keeps_name_param_when_tool_key_selects_tool():
    calls = collect_tool_calls('{"tool":"create_site","name":"nyxwell","title":"hi"}', TOOLS | {"create_site"})

    assert [(name, params) for _start, _end, name, params in calls] == [
        ("create_site", {"name": "nyxwell", "title": "hi"})
    ]


def test_collect_tool_calls_can_include_disabled_for_dispatcher_stripping():
    calls = collect_tool_calls(
        '{"tool":"react","emoji":"catjam"}',
        TOOLS,
        {"react"},
        include_disabled=True,
    )

    assert [(name, params) for _start, _end, name, params in calls] == [("react", {"emoji": "catjam"})]


def test_collect_tool_calls_accepts_raw_create_site_block():
    response = """[create_site]
name: kris
title: Kris Bio
body:
<!DOCTYPE html>
<html><body><h1 class="hero">Kris</h1></body></html>
[/create_site]"""

    calls = collect_tool_calls(response, TOOLS | {"create_site"})

    assert [(name, params) for _start, _end, name, params in calls] == [
        (
            "create_site",
            {
                "name": "kris",
                "title": "Kris Bio",
                "body": '<!DOCTYPE html>\n<html><body><h1 class="hero">Kris</h1></body></html>',
            },
        )
    ]


def test_collect_tool_calls_ignores_json_when_surrounded_by_text():
    response = 'here is debug: {"tool":"react","emoji":"catjam"} do not run this'
    calls = collect_tool_calls(response, TOOLS)
    assert calls == []


def test_collect_tool_calls_ignores_long_content_object_without_args():
    long_text = "a" * 400
    response = '{"tool":"send_message","content":"' + long_text + '"}'
    calls = collect_tool_calls(response, TOOLS | {"send_message"})
    assert calls == []
