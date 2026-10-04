"""Behavioral contracts for visible output, exercised through the real module."""

import json
import random
import string

import pytest

from maxwell_core.transport.output_safety import (
    _sanitize_visible_reply,
    extract_json_object,
    strip_tool_payload_leaks,
)


@pytest.mark.parametrize(
    "text",
    [
        "Read [Python documentation](https://docs.python.org/3/).",
        "References: [official source](https://example.org/a?q=one#two), [2](https://example.org/b).",
        "![diagram](https://example.org/diagram.png)",
        "[guide][reference]\n\n[reference]: https://example.org/guide",
        "Use values[index] and [optional] settings.",
        "Range: [0, 10]. Coordinates: [x, y].",
        "An array looks like [1, 2, 3].",
        "- [x] Finished\n- [ ] Pending",
        "Aviso [opcional]: consulta [documentación](https://example.org/es).",
        "普通文本 [可选]，以及 [文档](https://example.org/zh)。",
        "Available tools include [shell] and [web_search].",
    ],
)
def test_legitimate_markdown_and_bracketed_prose_survive(text):
    assert _sanitize_visible_reply(text, scrub_repeats=False) == text


@pytest.mark.parametrize("fence", ["```", "````", "~~~", "~~~~"])
@pytest.mark.parametrize(
    "body",
    [
        "values[index] = {'name': 'Alice'}\nprint(values[0])",
        'print("__NO_RESPONSE__")\nprint("<think>literal</think>")',
        "<system-reminder>this is an XML example</system-reminder>",
        '{"name": "example", "arguments": {"input": [1, 2]}}',
        '<tool:shell command="printf example"/>',
        "[RESPOND TO THIS] is a string in this code sample",
        "line\n\n\n\nline",
    ],
)
def test_fenced_code_samples_survive_visible_cleanup(fence, body):
    text = f"Example:\n{fence}python\n{body}\n{fence}\nThat is the complete sample."
    assert _sanitize_visible_reply(text) == text
    assert strip_tool_payload_leaks(text) == text


@pytest.mark.parametrize(
    "code",
    [
        "values[index]",
        "__NO_RESPONSE__",
        '<tool:shell command="example"/>',
        "<think>literal XML</think>",
        '{"name":"Alice","arguments":{"input":1}}',
        r"print('line\nline')",
        "https://example.org/<segment>",
    ],
)
def test_inline_code_samples_survive_visible_cleanup(code):
    text = f"Use `{code}` as the example."
    assert _sanitize_visible_reply(text) == text
    assert strip_tool_payload_leaks(text) == text


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "text", "text": "One line\nAnother line"},
        {"type": "text", "text": "See [guide](https://example.org/)."},
        [
            {"type": "text", "text": "First [part] "},
            {"type": "text", "text": "then [guide](https://example.org/)."},
        ],
    ],
)
def test_openai_text_parts_are_decoded_before_visible_cleanup(payload):
    parts = payload if isinstance(payload, list) else [payload]
    expected = "".join(part["text"] for part in parts)
    assert _sanitize_visible_reply(json.dumps(payload), scrub_repeats=False) == expected


@pytest.mark.parametrize("order", [(0, 1), (1, 0), (0, 1, 0), (1, 0, 1)])
def test_mixed_protocol_leaks_are_removed_in_source_order(order):
    variants = (
        '<tool:shell command="SECRET_PAYLOAD_XML"/>',
        "<|tool:shell>SECRET_PAYLOAD_PIPE<|/tool:shell>",
    )
    text = "Before " + " middle ".join(variants[index] for index in order) + " After"
    output = strip_tool_payload_leaks(text)
    assert "SECRET_PAYLOAD" not in output
    assert "<tool" not in output
    assert "<|tool" not in output
    assert output.startswith("Before ")
    assert output.endswith(" After")
    assert output.count("middle") == len(order) - 1


@pytest.mark.parametrize(
    "leak",
    [
        '[shell]{"command":"SECRET_PAYLOAD"}[/shell]',
        '[TOOL_CALL:shell]{"command":"SECRET_PAYLOAD"}[/shell]',
        '<tool:shell command="SECRET_PAYLOAD"/>',
        "<tool:shell>SECRET_PAYLOAD</tool:shell>",
        "<|tool:shell>SECRET_PAYLOAD<|/tool:shell>",
        "<think>SECRET_PAYLOAD</think>",
        "<system-reminder>SECRET_PAYLOAD</system-reminder>",
        'Called shell with {"command":"SECRET_PAYLOAD"} -> finished',
    ],
)
def test_real_protocol_leaks_are_removed_without_damaging_surrounding_text(leak):
    output = _sanitize_visible_reply("Before.\n" + leak + "\nAfter.")
    assert "SECRET_PAYLOAD" not in output
    assert output.startswith("Before.")
    assert output.endswith("After.")


@pytest.mark.parametrize("seed", range(8))
def test_output_cleanup_is_stable_for_generated_prose_and_leaks(seed):
    rng = random.Random(seed)
    for _ in range(25):
        label = "".join(rng.choices(string.ascii_letters, k=12))
        index = rng.randrange(1000)
        prose = f"Read [{label}](https://example.org/{index}) with [optional] values."
        leak = f'<tool:shell command="SECRET_{index}"/>'
        output = _sanitize_visible_reply(prose + "\n" + leak, scrub_repeats=False)
        assert output == prose
        assert _sanitize_visible_reply(output, scrub_repeats=False) == output


@pytest.mark.parametrize("seed", range(8))
def test_json_boundaries_survive_nested_values_and_escaped_strings(seed):
    rng = random.Random(seed)
    alphabet = string.ascii_letters + '{}[]\\"\n\t'
    for _ in range(25):
        value = {
            "string": "".join(rng.choices(alphabet, k=80)),
            "nested": {"items": [rng.randrange(1000), {"key": "} {"}]},
        }
        encoded = json.dumps(value)
        prefix = "prefix "
        text = prefix + " \t" + encoded + " trailing prose"
        result = extract_json_object(text, len(prefix))
        assert result is not None
        candidate, end = result
        assert json.loads(candidate) == value
        assert text[end:] == " trailing prose"


def test_standalone_fenced_tool_envelope_is_still_suppressed():
    text = '```json\n{"name":"shell","arguments":{"command":"SECRET"}}\n```'
    assert _sanitize_visible_reply(text) == ""


@pytest.mark.parametrize("depth", [1, 2, 4, 8, 12])
@pytest.mark.parametrize("kind", ["content", "text-part"])
def test_nested_response_envelopes_are_cleaned_in_one_pass(depth, kind):
    text = "Hello. __NO_RESPONSE__"
    for _ in range(depth):
        text = json.dumps(
            {"content": text} if kind == "content" else {"type": "text", "text": text}
        )
    output = _sanitize_visible_reply(text)
    assert output == "Hello."
    assert _sanitize_visible_reply(output) == output


def test_code_is_preserved_when_real_protocol_leaks_surround_it():
    example = '```python\nprint("<tool:shell command=example/>")\nvalues[0] = 1\n```'
    text = '<tool:react emoji="x"/>\n' + example + "\n<|tool:shell>SECRET<|/tool:shell>"
    assert _sanitize_visible_reply(text) == example
    assert strip_tool_payload_leaks(text) == example


def test_code_protection_tokens_cannot_collide_with_user_text():
    text = "Keep \ue000code0\ue001 exactly, then `values[index]`."
    assert _sanitize_visible_reply(text) == text
