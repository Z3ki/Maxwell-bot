"""The prompts have to ask for the behaviour people complained about missing.

Production logs showed three shapes: bursts of consecutive short replies,
"I'll do X" with no tool call behind it, and sites announced as working that
served a "Loading…" shell. Each of these asserts the instruction that addresses
one of those, so a future prompt edit cannot quietly drop it.
"""

from types import SimpleNamespace

from bot import LEAN_TOOL_PROTOCOL, TOOL_PROTOCOL
from bot_tools import CreateSiteTool, _site_placeholder_warnings
from tool_schemas import TOOL_PARAMETERS


def _create_site_desc():
    bot = SimpleNamespace(
        config=SimpleNamespace(
            MAXWELL_SITE_DIR="public/bot",
            MAXWELL_PUBLIC_BASE_URL="https://maxwell.example.com",
        )
    )
    return CreateSiteTool(bot).get_description()


# --------------------------------------------------------------------------
# proactivity
# --------------------------------------------------------------------------


def test_full_protocol_asks_for_proactive_work():
    text = TOOL_PROTOCOL.lower()
    assert "be proactive" in text
    assert "do the whole job" in text
    assert "finishing is the job" in text


def test_full_protocol_discourages_needless_questions():
    text = TOOL_PROTOCOL.lower()
    assert "only ask a question when you genuinely cannot proceed" in text


def test_lean_protocol_also_asks_for_proactive_work():
    """Ordinary chat turns carry the lean block, so it needs this too."""
    assert "be proactive" in LEAN_TOOL_PROTOCOL.lower()


# --------------------------------------------------------------------------
# anti-spam
# --------------------------------------------------------------------------


def test_protocols_forbid_burst_replies():
    for text in (TOOL_PROTOCOL.lower(), LEAN_TOOL_PROTOCOL.lower()):
        assert "one send_message" in text
        assert "spam" in text


def test_protocols_offer_silence_as_the_alternative():
    for text in (TOOL_PROTOCOL.lower(), LEAN_TOOL_PROTOCOL.lower()):
        assert "no_response" in text


# --------------------------------------------------------------------------
# no unfulfilled promises
# --------------------------------------------------------------------------


def test_full_protocol_forbids_claiming_unverified_work():
    text = TOOL_PROTOCOL.lower()
    assert "never claim something is done" in text
    assert "unless a tool result" in text


def test_full_protocol_still_rejects_ack_only_turns():
    text = TOOL_PROTOCOL.lower()
    assert "announcing an action is not performing it" in text
    assert "on it" in text
    assert "do not pair send_message" in text
    assert "acknowledgement" not in text
    assert "put the helper tool" not in text


def test_protocols_do_not_ask_for_a_placeholder_send():
    for text in (TOOL_PROTOCOL.lower(), LEAN_TOOL_PROTOCOL.lower()):
        assert "same batch as the acknowledgement" not in text
        assert "content='on it" not in text
        assert "more_tools" not in text


def test_lean_protocol_forbids_claiming_unverified_work():
    assert "never say you have done something you have not" in (
        LEAN_TOOL_PROTOCOL.lower()
    )


def test_protocols_do_not_force_reasoning_on_every_call():
    for text in (TOOL_PROTOCOL, LEAN_TOOL_PROTOCOL):
        assert "needs `reasoning`" not in text
        assert "may include `reasoning`" in text


# --------------------------------------------------------------------------
# sites: work hard, no placeholders
# --------------------------------------------------------------------------


def test_protocol_bans_placeholders_in_sites():
    text = TOOL_PROTOCOL.lower()
    for banned in ("lorem ipsum", "todo", "coming soon", "placeholder"):
        assert banned in text, f"{banned!r} is not called out"


def test_protocol_calls_a_loading_shell_a_failure():
    text = TOOL_PROTOCOL.lower()
    assert "loading" in text
    assert "built nothing" in text


def test_protocol_requires_site_test_before_claiming_it_works():
    text = TOOL_PROTOCOL.lower()
    assert "do not tell anyone a site works before site_test" in text
    assert "not actually rendered" in text


def test_protocol_tells_it_to_write_as_much_code_as_needed():
    assert "write 900 lines" in TOOL_PROTOCOL.lower()


def test_create_site_description_bans_placeholders():
    text = _create_site_desc().lower()
    assert "no placeholders" in text
    assert "lorem ipsum" in text
    assert "shipped nothing" in text


def test_create_site_body_schema_bans_placeholders():
    body = TOOL_PARAMETERS["create_site"]["properties"]["body"]["description"].lower()
    assert "no placeholders" in body
    assert "lorem ipsum" in body
    assert "loading" in body


def test_create_site_description_stays_within_the_openai_limit():
    """Providers truncate long tool descriptions, which silently drops rules."""
    assert len(_create_site_desc()) < 1024


# --------------------------------------------------------------------------
# placeholder detection in what was actually written
# --------------------------------------------------------------------------


def test_a_finished_page_raises_no_warnings():
    html = (
        "<!doctype html><html><head><title>Real</title></head><body>"
        '<nav><a href="/about/">About</a><a href="/docs/">Docs</a></nav>'
        "<h1>Actual content</h1><p>Words that mean something.</p>"
        "</body></html>"
    )
    assert _site_placeholder_warnings(html, []) == []


def test_lorem_ipsum_is_reported():
    found = _site_placeholder_warnings("<p>Lorem ipsum dolor sit amet</p>", [])
    assert any("lorem ipsum" in item for item in found)


def test_todo_is_reported():
    found = _site_placeholder_warnings("<script>// TODO: finish</script>", [])
    assert any("TODO" in item for item in found)


def test_coming_soon_is_reported():
    found = _site_placeholder_warnings("<section>Coming soon!</section>", [])
    assert any("coming soon" in item for item in found)


def test_many_dead_links_are_reported():
    html = "".join(f'<a href="#">Item {i}</a>' for i in range(6))
    found = _site_placeholder_warnings(html, [])
    assert any("go nowhere" in item for item in found)


def test_one_dead_link_is_not_reported():
    """A single href="#" is a legitimate JS hook, not an unwired nav."""
    found = _site_placeholder_warnings('<a href="#" onclick="open()">Menu</a>', [])
    assert found == []


def test_extra_files_are_scanned_too():
    found = _site_placeholder_warnings(
        None, [{"path": "app.js", "bytes": b"// TODO wire this up\n"}]
    )
    assert any(item.startswith("app.js") for item in found)


def test_binary_and_huge_files_are_skipped_safely():
    entries = [
        {"path": "logo.png", "bytes": b"\x89PNG\r\n\x1a\n\xff\xfe"},
        {"path": "huge.js", "bytes": b"x" * 2_000_001},
    ]
    assert _site_placeholder_warnings(None, entries) == []


def test_warnings_are_bounded():
    html = "TODO FIXME lorem ipsum coming soon [insert here] not implemented TBD" * 5
    assert len(_site_placeholder_warnings(html, [])) <= 12


def test_no_sources_means_no_warnings():
    assert _site_placeholder_warnings(None, []) == []
    assert _site_placeholder_warnings("", []) == []


# --------------------------------------------------------------------------
# chess: he plays it himself
# --------------------------------------------------------------------------


def test_protocol_tells_him_to_pick_his_own_chess_moves():
    text = TOOL_PROTOCOL.lower()
    assert "you play your own moves" in text
    assert "nothing plays for you" in text
