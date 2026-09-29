"""Consumer-visible site placeholder detection and provider schema bounds."""

from types import SimpleNamespace

from bot_tools import CreateSiteTool, _site_placeholder_warnings


def _create_site_desc():
    bot = SimpleNamespace(
        config=SimpleNamespace(
            MAXWELL_SITE_DIR="public/bot",
            MAXWELL_PUBLIC_BASE_URL="https://maxwell.example.com",
        )
    )
    return CreateSiteTool(bot).get_description()


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


