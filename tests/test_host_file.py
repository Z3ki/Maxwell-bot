"""host_file publishes a curlable/local file at a stable public URL."""

import asyncio
from types import SimpleNamespace

import pytest

from bot_tools import CreateSiteTool, HostFileTool, _blob_looks_like_html


def _bot(tmp_path):
    site_dir = tmp_path / "public" / "bot"
    data_dir = tmp_path / "data"
    exports = data_dir / "exports"
    site_dir.mkdir(parents=True)
    data_dir.mkdir()
    exports.mkdir()
    control = {"create_site_quota_per_user": 50}
    bot = SimpleNamespace(
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
    return bot, site_dir, exports


def _msg(uid=42, name="tester"):
    return SimpleNamespace(author=SimpleNamespace(id=uid, display_name=name))


def run(coro):
    return asyncio.run(coro)


PAGE = "<!DOCTYPE html><html><head><title>hi</title></head><body><h1>yo</h1></body></html>"


def test_host_inline_html_becomes_a_page(tmp_path):
    bot, site_dir, _exports = _bot(tmp_path)
    out = run(
        HostFileTool(bot).execute(
            _msg(), filename="index.html", content=PAGE, name="demo"
        )
    )
    assert out.startswith("Hosted page: https://maxwell.example.com/bot/_files/demo/")
    written = site_dir / "_files" / "demo" / "index.html"
    assert written.read_text() == PAGE
    assert "send_message this URL" in out


def test_host_inline_non_html_keeps_filename(tmp_path):
    bot, site_dir, _exports = _bot(tmp_path)
    out = run(
        HostFileTool(bot).execute(
            _msg(), filename="notes.txt", content="hello", name="notes"
        )
    )
    assert "Hosted file:" in out
    assert out.split("\n")[0].endswith("/bot/_files/notes/notes.txt")
    assert (site_dir / "_files" / "notes" / "notes.txt").read_text() == "hello"


def test_host_from_path(tmp_path):
    bot, site_dir, exports = _bot(tmp_path)
    src = exports / "clip.json"
    src.write_text('{"ok":true}', encoding="utf-8")
    out = run(HostFileTool(bot).execute(_msg(), path=str(src), name="clip"))
    assert "Hosted file:" in out
    assert (site_dir / "_files" / "clip" / "clip.json").read_text() == '{"ok":true}'


def test_host_from_url(tmp_path, monkeypatch):
    bot, site_dir, _exports = _bot(tmp_path)

    async def fake_fetch(url, *, max_bytes, timeout=30.0):
        assert url == "https://ex.com/page/index.html"
        return url, "text/html; charset=utf-8", PAGE.encode("utf-8")

    monkeypatch.setattr("bot_tools._fetch_public_url", fake_fetch)
    out = run(
        HostFileTool(bot).execute(_msg(), url="https://ex.com/page/index.html", name="curled")
    )
    assert out.startswith("Hosted page:")
    assert (site_dir / "_files" / "curled" / "index.html").read_text() == PAGE


def test_host_refuses_private_url(tmp_path):
    bot, _site_dir, _exports = _bot(tmp_path)
    out = run(HostFileTool(bot).execute(_msg(), url="http://127.0.0.1/secret"))
    assert out.startswith("Error:")
    assert "private" in out.lower() or "internal" in out.lower()


def test_host_refuses_php(tmp_path):
    bot, _site_dir, _exports = _bot(tmp_path)
    out = run(
        HostFileTool(bot).execute(_msg(), filename="shell.php", content="<?php echo 1;")
    )
    assert out.startswith("Error:")
    assert "unsafe" in out.lower() or "unsupported" in out.lower()


def test_host_requires_a_source(tmp_path):
    bot, _site_dir, _exports = _bot(tmp_path)
    out = run(HostFileTool(bot).execute(_msg()))
    assert out.startswith("Error:")
    assert "url" in out.lower()


def test_create_site_from_url(tmp_path, monkeypatch):
    bot, site_dir, _exports = _bot(tmp_path)

    async def fake_fetch(url, *, max_bytes, timeout=30.0):
        return url, "text/html", PAGE.encode("utf-8")

    monkeypatch.setattr("bot_tools._fetch_public_url", fake_fetch)
    out = run(
        CreateSiteTool(bot).execute(
            _msg(), name="fromurl", url="https://ex.com/index.html"
        )
    )
    assert out.startswith("Site created:")
    assert (site_dir / "fromurl" / "index.html").read_text() == PAGE


def test_create_site_url_rejects_non_html(tmp_path, monkeypatch):
    bot, _site_dir, _exports = _bot(tmp_path)

    async def fake_fetch(url, *, max_bytes, timeout=30.0):
        return url, "application/pdf", b"%PDF-1.4"

    monkeypatch.setattr("bot_tools._fetch_public_url", fake_fetch)
    out = run(
        CreateSiteTool(bot).execute(
            _msg(), name="pdf", title="pdf", url="https://ex.com/doc.pdf"
        )
    )
    assert out.startswith("Error:")
    assert "host_file" in out


@pytest.mark.parametrize(
    "blob, ctype, url, expect",
    [
        (b"<!DOCTYPE html><html></html>", "", "", True),
        (b"<html lang=en>", "text/plain", "", True),
        (b"%PDF", "text/html", "", True),
        (b"%PDF", "application/pdf", "https://e.com/a.pdf", False),
        (b"hi", "text/plain", "https://e.com/index.html", True),
    ],
)
def test_html_sniff(blob, ctype, url, expect):
    assert _blob_looks_like_html(blob, ctype, url) is expect
