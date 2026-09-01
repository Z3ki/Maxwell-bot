"""site_test: console/network probe, asset checks, ownership, URL jail."""

import asyncio
import base64
from types import SimpleNamespace

import pytest

import site_test
from bot_tools import CreateSiteTool, SiteTestTool


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def bot(tmp_path):
    site_dir = tmp_path / "public" / "bot"
    data_dir = tmp_path / "data"
    site_dir.mkdir(parents=True)
    data_dir.mkdir()
    control = {"create_site_quota_per_user": 50}
    return SimpleNamespace(
        config=SimpleNamespace(
            MAXWELL_SITE_DIR=str(site_dir),
            MAXWELL_PUBLIC_BASE_URL="https://maxwell.example.com",
            DATA_DIR=str(data_dir),
        ),
        _sites={},
        _load_sites=lambda quiet=True: None,
        _is_admin=lambda _uid: False,
        _control=control,
        control=control,
        tools={},
    )


def _msg(uid=42, name="tester"):
    return SimpleNamespace(author=SimpleNamespace(id=uid, display_name=name))


PAGE = (
    "<!DOCTYPE html><html><head><title>Broken</title>"
    '<link rel="stylesheet" href="missing.css">'
    "</head><body><h1>hi</h1>"
    "<script src='gone.js'></script>"
    "<script>console.error('boom')</script>"
    "</body></html>"
)


def test_extract_assets_keeps_same_origin_and_skips_cdn():
    html = (
        '<link href="style.css"><script src="https://cdn.example/x.js"></script>'
        '<img src="https://maxwell.example.com/bot/demo/logo.png">'
        '<a href="mailto:x@y.z">'
    )
    urls = site_test.extract_assets(html, "https://maxwell.example.com/bot/demo/")
    assert "https://maxwell.example.com/bot/demo/style.css" in urls
    assert "https://maxwell.example.com/bot/demo/logo.png" in urls
    assert not any("cdn.example" in u for u in urls)


def test_missing_local_assets_finds_broken_relative_links(tmp_path):
    root = tmp_path / "site"
    root.mkdir()
    (root / "index.html").write_text(
        '<link href="style.css"><script src="app.js">', encoding="utf-8"
    )
    (root / "style.css").write_text("body{}", encoding="utf-8")
    missing = site_test.missing_local_assets(
        (root / "index.html").read_text(), str(root), slug="demo"
    )
    assert missing == ["app.js"]


def test_page_url_stays_inside_the_site():
    base = "https://maxwell.example.com/bot"
    assert site_test.page_url(base, "demo") == "https://maxwell.example.com/bot/demo/"
    assert (
        site_test.page_url(base, "demo", "about/")
        == "https://maxwell.example.com/bot/demo/about/"
    )
    assert (
        site_test.page_url(base, "demo", "https://maxwell.example.com/bot/demo/x")
        == "https://maxwell.example.com/bot/demo/x"
    )
    with pytest.raises(ValueError):
        site_test.page_url(base, "demo", "https://evil.test/")
    with pytest.raises(ValueError):
        site_test.page_url(base, "demo", "https://maxwell.example.com/bot/other/")
    with pytest.raises(ValueError):
        site_test.page_url(base, "demo", "../secret")
    with pytest.raises(ValueError):
        site_test.page_url(base, "demo", "/etc/passwd")


def test_format_report_lists_console_errors_and_attaches_a_screenshot():
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
    text = site_test.format_report(
        {
            "url": "https://maxwell.example.com/bot/demo/",
            "title": "Broken",
            "http_status": 200,
            "browser": "chromium-browser",
            "console_errors": ["Uncaught TypeError: foo"],
            "page_errors": [],
            "failed_requests": ["404 https://maxwell.example.com/bot/demo/x.js"],
            "asset_errors": ["missing on disk: x.js"],
            "screenshot_png": png,
        }
    )
    assert "Console errors:" in text
    assert "Uncaught TypeError: foo" in text
    assert "404" in text
    assert "RESULT:" in text and "problem" in text
    assert "__IMAGE_B64__" in text
    raw = text.split("__IMAGE_B64__")[1].split("__END_IMAGE_B64__")[0]
    assert base64.b64decode(raw) == png


def test_format_report_clean_page():
    text = site_test.format_report(
        {
            "url": "https://maxwell.example.com/bot/demo/",
            "http_status": 200,
            "browser": "chromium",
            "console_errors": [],
        }
    )
    assert "Console errors: none" in text
    assert "no console errors" in text


def test_can_test_a_site_you_did_not_create(bot, monkeypatch):
    run(CreateSiteTool(bot).execute(_msg(uid=1), name="mine", title="Mine", body=PAGE))

    async def fake_http(url, **kwargs):
        return 200, PAGE.encode(), ""

    async def fake_browser(url, **kwargs):
        return {"browser": "none", "http_status": 200, "console_errors": []}

    monkeypatch.setattr(site_test, "http_get", fake_http)
    monkeypatch.setattr(site_test, "probe_browser", fake_browser)
    out = run(SiteTestTool(bot).execute(_msg(uid=2), name="mine"))
    assert "belongs to someone else" not in out
    assert "mine" in out.lower()


def test_site_test_refuses_a_third_identical_probe(bot, monkeypatch):
    from bot_tools import SITE_READ_LOOP_MARKER

    msg = _msg()
    run(CreateSiteTool(bot).execute(msg, name="demo", title="Demo", body=PAGE))

    async def fake_http(url, **kwargs):
        return 200, PAGE.encode(), ""

    async def fake_browser(url, **kwargs):
        return {"browser": "none", "http_status": 200, "console_errors": []}

    monkeypatch.setattr(site_test, "http_get", fake_http)
    monkeypatch.setattr(site_test, "probe_browser", fake_browser)
    tool = SiteTestTool(bot)
    first = run(tool.execute(msg, name="demo"))
    second = run(tool.execute(msg, name="demo"))
    third = run(tool.execute(msg, name="demo"))
    assert SITE_READ_LOOP_MARKER not in first
    assert SITE_READ_LOOP_MARKER not in second
    assert SITE_READ_LOOP_MARKER in third


def test_unknown_site(bot):
    out = run(SiteTestTool(bot).execute(_msg(), name="ghost"))
    assert "no site named 'ghost'" in out


def test_path_cannot_leave_the_site(bot):
    run(CreateSiteTool(bot).execute(_msg(), name="jail", title="Jail", body=PAGE))
    out = run(
        SiteTestTool(bot).execute(
            _msg(), name="jail", path="https://evil.test/steal"
        )
    )
    assert out.startswith("Error:")
    assert "not this site" in out


def test_reports_console_errors_and_missing_files(bot, monkeypatch):
    run(CreateSiteTool(bot).execute(_msg(), name="demo", title="Demo", body=PAGE))

    async def fake_http(url, **kwargs):
        if url.endswith((".css", ".js")):
            return 404, b"", ""
        return 200, PAGE.encode(), ""

    async def fake_browser(url, **kwargs):
        return {
            "browser": "chromium-browser",
            "title": "Broken",
            "http_status": 200,
            "console_errors": ["boom"],
            "console_warnings": [],
            "page_errors": ["Error: exploded"],
            "failed_requests": [
                "404 https://maxwell.example.com/bot/demo/gone.js"
            ],
            "screenshot_png": b"\x89PNG\r\n\x1a\n" + b"\x00" * 12,
        }

    monkeypatch.setattr(site_test, "http_get", fake_http)
    monkeypatch.setattr(site_test, "probe_browser", fake_browser)
    out = run(SiteTestTool(bot).execute(_msg(), name="demo", screenshot=True, wait=0.2))
    assert "SITE TEST https://maxwell.example.com/bot/demo/" in out
    assert "boom" in out
    assert "exploded" in out
    assert "gone.js" in out or "missing.css" in out
    assert "Fix with edit_site" in out
    assert "__IMAGE_B64__" in out


@pytest.mark.skipif(not site_test.find_chrome(), reason="no chromium/chrome on PATH")
def test_real_browser_sees_console_error_and_404():
    html = (
        "<!doctype html><html><head><title>Probe</title></head>"
        "<body><script>console.error('site-test-probe');</script>"
        '<img src="/missing-asset.png"></body></html>'
    )

    async def serve_and_probe():
        from aiohttp import web

        async def handler(request):
            if request.path in {"/", "/index.html"}:
                return web.Response(text=html, content_type="text/html")
            return web.Response(status=404, text="nope")

        app = web.Application()
        app.router.add_get("/", handler)
        app.router.add_get("/index.html", handler)
        app.router.add_get("/missing-asset.png", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        server = site._server
        if server is None or not getattr(server, "sockets", None):
            await runner.cleanup()
            pytest.skip("could not bind a test HTTP server")
        port = server.sockets[0].getsockname()[1]
        try:
            return await site_test.probe_browser(
                f"http://127.0.0.1:{port}/", wait=1.0, screenshot=True
            )
        finally:
            await runner.cleanup()

    result = run(serve_and_probe())
    if result.get("browser_error"):
        pytest.skip(f"chromium could not probe: {result['browser_error']}")
    blob = " ".join(result.get("console_errors") or [])
    failed = " ".join(result.get("failed_requests") or [])
    assert "site-test-probe" in blob
    assert "missing-asset.png" in failed or "404" in failed
    assert isinstance(result.get("screenshot_png"), (bytes, bytearray))
    assert result["screenshot_png"][:8] == b"\x89PNG\r\n\x1a\n"
