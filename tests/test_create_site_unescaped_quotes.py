r"""Regression tests for the create_site unescaped-quote parser bug.

2026-08-02: Z3ki's "Mat Dickie" cartographer page in #boing — the
model emitted a 14 KB ``create_site`` tool call with the HTML body
containing unescaped ``"`` (from ``target="_blank"``,
``href="..."``) and a typo'd literal backslash (``</div\`` instead of
``</div>``). The parser walked to EOF looking for a balanced close,
never extracted the tool call, and dumped the whole malformed blob
into chat as raw visible text. ``create_site`` never ran.

These tests pin down the ``_safe_parse_tool_call_candidate`` /
``_repair_unescaped_html_quotes`` recovery path.
"""

import json

from providers import (
    _repair_unescaped_html_quotes,
    _safe_parse_tool_call_candidate,
)


def _wrap(arguments_obj: dict) -> str:
    """Build a candidate tool-call JSON for a given arguments dict."""
    return json.dumps({"name": "create_site", "arguments": arguments_obj})


def test_repair_returns_none_for_clean_json():
    # Clean JSON should never hit the repair path; the caller tries
    # json.loads first and only calls _repair_* on failure.
    args = {"reasoning": "r", "name": "old", "title": "T", "body": "<p>hi</p>"}
    candidate = _wrap(args)
    # clean parse succeeds, repair is a no-op (returns None since nothing
    # needed repairing)
    assert _safe_parse_tool_call_candidate(candidate)["name"] == "create_site"
    assert _repair_unescaped_html_quotes(candidate) is None


def test_repair_fixes_unescaped_html_attribute_quotes():
    # The canonical bug: body has unescaped " from target="_blank".
    original_body = (
        '<a href="https://x.example/" target="_blank" rel="noopener">link</a>'
    )
    # The LLM emitted this candidate WITHOUT escaping the inner quotes
    # in the body — the body string closes early at the first inner `"`.
    candidate = (
        '{"name": "create_site", "arguments": '
        '{"reasoning": "r", "name": "old", "title": "T", "body": "'
        + original_body
        + '"}}'
    )
    obj = _safe_parse_tool_call_candidate(candidate)
    assert obj is not None
    assert obj["name"] == "create_site"
    assert obj["arguments"]["body"] == original_body


def test_repair_escapes_bare_quotes_in_body():
    """The exact bug: ``body`` field has unescaped ``"`` from HTML
    attributes. Repair must escape them and the candidate must parse
    back to a dict with the original body content.
    """
    original_body = '<a href="https://x.example/" target="_blank">link</a>'
    # Construct a candidate where the LLM forgot to escape the inner
    # quotes — the body string closes early at the first inner `"`.
    candidate = (
        '{"name": "create_site", "arguments": '
        '{"reasoning": "r", "name": "old", "title": "T", "body": "'
        + '<a href="https://x.example/" target="_blank">link</a>'
        + '"}}'
    )
    obj = _safe_parse_tool_call_candidate(candidate)
    assert obj is not None, "repair should have unblocked the parse"
    assert obj["name"] == "create_site"
    assert obj["arguments"]["body"] == original_body


def test_repair_escapes_literal_newlines_in_body():
    """The LLM sometimes emits bare newlines inside the body field
    (it treats the JSON string as a plain string and inserts real
    ``\\n``). The repair must escape them as ``\\n`` so JSON parses.
    """
    body_with_real_newline = "<p>line one\nline two</p>"
    candidate = (
        '{"name": "create_site", "arguments": '
        '{"reasoning": "r", "name": "old", "title": "T", "body": "'
        + body_with_real_newline
        + '"}}'
    )
    obj = _safe_parse_tool_call_candidate(candidate)
    assert obj is not None
    assert obj["arguments"]["body"] == body_with_real_newline


def test_repair_escapes_bare_backslash_typo():
    """The LLM typo'd ``</div>`` as ``</div\\`` in the actual
    Old Cartographers body — a literal backslash not followed by a
    JSON escape char. Repair must escape the bare backslash so the
    reparsed JSON keeps it as a single ``\\`` character.
    """
    body_with_typo = "<div>oops</div\\>"
    candidate = (
        '{"name": "create_site", "arguments": '
        '{"reasoning": "r", "name": "old", "title": "T", "body": "'
        + body_with_typo
        + '"}}'
    )
    obj = _safe_parse_tool_call_candidate(candidate)
    assert obj is not None
    assert obj["arguments"]["body"] == body_with_typo


def test_repair_returns_none_when_no_body_field():
    """Without a ``body`` field, the repair pass can't help. We
    expect ``None`` so the caller falls back to skip-and-continue.
    """
    candidate = (
        '{"name": "web_search", "arguments": {"reasoning": "r", "query": "Mat Dickie"}}'
    )
    # Clean web_search parses fine — repair returns None (no-op).
    obj = _safe_parse_tool_call_candidate(candidate)
    assert obj is not None
    assert obj["name"] == "web_search"
    assert _repair_unescaped_html_quotes(candidate) is None


def test_repair_trailing_garbage_tolerated():
    """The LLM appended a hallucinated ``<parameter ...>`` tag after
    the JSON close. raw_decode should still extract the tool call.
    """
    candidate = (
        '{"name": "create_site", "arguments": '
        '{"reasoning": "r", "name": "old", "title": "T", "body": "<p>hi</p>"}}'
        '\n<parameter name="encoding">text</parameter>'
    )
    obj = _safe_parse_tool_call_candidate(candidate)
    assert obj is not None
    assert obj["name"] == "create_site"
    assert obj["arguments"]["body"] == "<p>hi</p>"


def test_repair_preserves_keys_after_body():
    """``body`` is not always the last key in ``arguments``.

    The terminator scan used to look only for an unescaped ``"``
    followed by ``}}`` — true only when ``body`` is last. With a key
    after it, the scan ran past the real terminator to the end of the
    object and swallowed every trailing key into the body string, so
    ``title`` silently vanished and the body was corrupted.
    """
    body = '<a href="https://x.example/" target="_blank">link</a>'
    candidate = (
        '{"name": "create_site", "arguments": '
        '{"body": "' + body + '", "title": "T", "name": "old"}}'
    )
    obj = _safe_parse_tool_call_candidate(candidate)
    assert obj is not None
    args = obj["arguments"]
    assert args["body"] == body
    assert args["title"] == "T", "key after body must survive the repair"
    assert args["name"] == "old"


def test_repair_handles_quote_comma_inside_body():
    """A body containing a literal ``",`` produces a false terminator
    candidate. The repair must reject it (it does not reparse) and keep
    scanning for the real one.
    """
    body = '<p>He said "hello", then left</p>'
    candidate = (
        '{"name": "create_site", "arguments": '
        '{"body": "' + body + '", "title": "T"}}'
    )
    obj = _safe_parse_tool_call_candidate(candidate)
    assert obj is not None
    assert obj["arguments"]["body"] == body
    assert obj["arguments"]["title"] == "T"


def test_streaming_feed_applies_unescaped_quote_repair():
    """The live path regression.

    ``_CustomToolCallBuffer.feed`` used bare ``json.loads`` to validate a
    balanced candidate, so the repair pass above — reachable only from a
    dead method — never ran on real streamed output. A ``create_site``
    with ``href="..."`` in the body was treated as a false-positive
    opener and shipped to the channel as raw visible text while the tool
    never ran. Parsing the candidate in isolation passed, which is why
    the unit tests above did not catch it; this one drives ``feed``.
    """
    from providers import _CustomToolCallBuffer

    body = '<a href="https://x.example/" target="_blank">link</a>'
    payload = (
        'Sure! {"name": "create_site", "arguments": '
        '{"body": "' + body + '", "title": "T"}}'
    )
    buf = _CustomToolCallBuffer()
    buf.feed(payload)
    buf.drain()

    assert len(buf.completed) == 1, "tool call must be extracted, not leaked as text"
    call = buf.completed[0]
    assert call["function"]["name"] == "create_site"
    args = json.loads(call["function"]["arguments"])
    assert args["body"] == body
    assert args["title"] == "T"
    # The malformed JSON must NOT reach the channel as visible text.
    visible = "".join(buf.text_parts)
    assert visible == "Sure! "
    assert "create_site" not in visible


def test_streaming_feed_repair_across_chunk_boundaries():
    """Same as above but split mid-attribute, since deltas arrive in
    arbitrary chunks and the opener/close can straddle them.
    """
    from providers import _CustomToolCallBuffer

    chunks = [
        'Sure! {"name": "create_s',
        'ite", "arguments": {"body": "<a href=',
        '"x">hi</a>", "title": "T"}}',
    ]
    buf = _CustomToolCallBuffer()
    for chunk in chunks:
        buf.feed(chunk)
    buf.drain()

    assert len(buf.completed) == 1
    args = json.loads(buf.completed[0]["function"]["arguments"])
    assert args["body"] == '<a href="x">hi</a>'
    assert args["title"] == "T"


def test_streaming_feed_leaves_plain_text_alone():
    """Regression guard: routing feed through the repair path must not
    change behaviour for ordinary prose, including text with braces.
    """
    from providers import _CustomToolCallBuffer

    for text in (
        "just a normal reply, no json here",
        "here is some css: {color: red} ok",
    ):
        buf = _CustomToolCallBuffer()
        buf.feed(text)
        buf.drain()
        assert buf.completed == []
        assert "".join(buf.text_parts) == text


def test_repair_full_z3ki_cartographer_repro():
    """The actual 14 KB LLM output from the 2026-08-02 #boing
    cartographer page. This is the canonical regression — if this
    fails, the user is back to seeing broken JSON in chat instead of
    a working site.
    """
    # Read the saved user paste of the LLM output
    import os
    user_path = "/root/.hermes/cache/documents/doc_383cff7b29f9_message.txt"
    if not os.path.exists(user_path):
        return  # Skipped when fixture not present
    text = open(user_path).read()
    # The LLM output starts with prose then {"name": "create_site", ...
    start = text.find('{"name": "create_site"')
    candidate = text[start:]
    obj = _safe_parse_tool_call_candidate(candidate)
    assert obj is not None, "the canonical LLM output failed to repair"
    assert obj["name"] == "create_site"
    body = obj["arguments"]["body"]
    # Body should be a real HTML document
    assert body.startswith("<!DOCTYPE html>")
    assert "</html>" in body
    # And contain the cartographers mentioned
    assert "Mercator" in body
    assert "Nolli" in body
