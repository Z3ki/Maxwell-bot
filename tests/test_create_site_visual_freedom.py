"""Generated sites preserve supplied HTML and enforce only configured CSP."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

from bot_tools import CreateSiteTool


def _make_tool(tmp_path: Path) -> CreateSiteTool:
    site_dir = tmp_path / "public" / "bot"
    data_dir = tmp_path / "data"
    site_dir.mkdir(parents=True)
    data_dir.mkdir()
    bot = SimpleNamespace(
        config=SimpleNamespace(
            MAXWELL_SITE_DIR=str(site_dir),
            MAXWELL_PUBLIC_BASE_URL="https://maxwell.example.com",
            DATA_DIR=str(data_dir),
        ),
        _sites={},
        _load_sites=lambda quiet=True: None,
        _is_admin=lambda _uid: False,
        _control={},
        control={},
        tools={},
    )
    return CreateSiteTool(bot)


def _author_message():
    return SimpleNamespace(
        author=SimpleNamespace(id=42, display_name="tester"),
    )


_LIGHT_NEWSPAPER = """<!DOCTYPE html>
<html><head><title>Broadsheet</title>
<style>
body { background: #f4ecd8; color: #1a1208; font-family: 'Georgia', serif;
  column-count: 3; max-width: 1100px; margin: 2rem auto; }
h1 { font-family: 'Old English Text MT', serif; font-size: 64px; column-span: all; }
</style></head>
<body><h1>The Evening Post</h1><p>classifieds and shipping news</p></body></html>
"""

_CANVAS_TOY = """<!DOCTYPE html>
<html><head><title>dots</title></head>
<body style="margin:0;background:#fff8f0">
<canvas id="c" width="400" height="300"></canvas>
<script>
const c = document.getElementById('c').getContext('2d');
c.fillStyle = '#c45c26';
c.fillRect(10, 10, 80, 80);
</script>
</body></html>
"""


def test_create_site_writes_html_as_is_without_house_skin(tmp_path):
    tool = _make_tool(tmp_path)
    message = _author_message()

    async def run():
        r1 = await tool.execute(
            message, name="broadsheet", title="Evening Post", body=_LIGHT_NEWSPAPER
        )
        r2 = await tool.execute(
            message, name="dots", title="dots", body=_CANVAS_TOY
        )
        return r1, r2

    r1, r2 = asyncio.run(run())
    assert r1.startswith("Site created:")
    assert r2.startswith("Site created:")

    paper = (tmp_path / "public" / "bot" / "broadsheet" / "index.html").read_text(
        encoding="utf-8"
    )
    dots = (tmp_path / "public" / "bot" / "dots" / "index.html").read_text(
        encoding="utf-8"
    )

    assert "The Evening Post" in paper
    assert "column-count: 3" in paper
    assert "Georgia" in paper
    assert "c45c26" in dots
    assert "getElementById('c')" in dots
    assert "The Evening Post" not in dots

    # Nothing is injected by default: what the model wrote is what is served.
    assert paper.startswith("<!DOCTYPE html>")
    assert dots.startswith("<!DOCTYPE html>")
    assert "Content-Security-Policy" not in paper
    assert "Content-Security-Policy" not in dots
    for blob in (paper, dots):
        low = blob.lower()
        assert "fonts.googleapis.com" not in low
        assert "font-family: inter" not in low
        assert "glassmorphism" not in low
        # Must not wrap the model's page in extra chrome.
        assert "maxwell-site-shell" not in low
        assert 'id="maxwell-root"' not in low


def test_site_inject_csp_is_opt_in(tmp_path):
    """Operators without a CSP at the host layer can still get the meta tag."""
    tool = _make_tool(tmp_path)
    tool.bot._control["site_inject_csp"] = True
    tool.bot.control["site_inject_csp"] = True

    async def run():
        return await tool.execute(
            _author_message(), name="guarded", title="guarded", body=_CANVAS_TOY
        )

    assert asyncio.run(run()).startswith("Site created:")
    page = (tmp_path / "public" / "bot" / "guarded" / "index.html").read_text(
        encoding="utf-8"
    )
    assert "Content-Security-Policy" in page
    # Still the model's page, just with a policy in front of it.
    assert "c45c26" in page
