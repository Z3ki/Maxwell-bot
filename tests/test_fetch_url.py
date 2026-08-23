"""fetch_url: SSRF-safe redirects, private URL refusal, usable page text."""

import asyncio
from types import SimpleNamespace

from bot_tools import FetchUrlTool, _fetch_public_url, _is_safe_url


class _Content:
    def __init__(self, data: bytes):
        self._data = data

    async def iter_chunked(self, _n):
        yield self._data


class FakeResp:
    def __init__(self, status, *, headers=None, body=b""):
        self.status = status
        self.headers = headers or {}
        self.content = _Content(body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class FakeSession:
    def __init__(self, by_url: dict[str, FakeResp]):
        self.by_url = by_url
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        resp = self.by_url.get(url)
        if resp is None:
            return FakeResp(404)
        return resp


def _run(coro):
    return asyncio.run(coro)


def test_is_safe_url_blocks_private_and_allows_public():
    assert _is_safe_url("https://example.com/page") is True
    assert _is_safe_url("http://127.0.0.1/") is False
    assert _is_safe_url("http://localhost/admin") is False
    assert _is_safe_url("http://10.0.0.5/x") is False
    assert _is_safe_url("http://169.254.169.254/latest") is False
    assert _is_safe_url("file:///etc/passwd") is False


def test_fetch_url_refuses_private_without_network():
    tool = FetchUrlTool(SimpleNamespace())
    msg = SimpleNamespace(id=1, channel=SimpleNamespace(id=2))
    result = _run(tool.execute(msg, url="http://127.0.0.1/secret"))
    assert result == "Error: Cannot fetch from private/internal URLs"


def test_fetch_public_url_follows_redirects(monkeypatch):
    session = FakeSession(
        {
            "https://ex.com/old": FakeResp(
                301, headers={"Location": "https://ex.com/new"}
            ),
            "https://ex.com/new": FakeResp(
                200,
                headers={"Content-Type": "text/plain"},
                body=b"hello page",
            ),
        }
    )

    async def _session():
        return session

    monkeypatch.setattr("bot_tools._get_shared_session", _session)
    final, ctype, raw = _run(
        _fetch_public_url("https://ex.com/old", max_bytes=1024)
    )
    assert final == "https://ex.com/new"
    assert "text/plain" in ctype
    assert raw == b"hello page"
    assert session.calls[0][1].get("allow_redirects") is False
    assert "User-Agent" in session.calls[0][1].get("headers", {})


def test_fetch_public_url_refuses_redirect_to_private(monkeypatch):
    session = FakeSession(
        {
            "https://ex.com/jump": FakeResp(
                302, headers={"Location": "http://127.0.0.1/meta"}
            ),
        }
    )

    async def _session():
        return session

    monkeypatch.setattr("bot_tools._get_shared_session", _session)
    try:
        _run(_fetch_public_url("https://ex.com/jump", max_bytes=1024))
        raise AssertionError("expected private-redirect refusal")
    except ValueError as e:
        assert "private/internal" in str(e)


def test_fetch_url_returns_page_text_after_redirect(monkeypatch):
    session = FakeSession(
        {
            "https://ex.com/a": FakeResp(
                302, headers={"Location": "/b"}
            ),
            "https://ex.com/b": FakeResp(
                200,
                headers={"Content-Type": "text/html; charset=utf-8"},
                body=b"<html><body><p>readable article</p></body></html>",
            ),
        }
    )

    async def _session():
        return session

    monkeypatch.setattr("bot_tools._get_shared_session", _session)
    bot = SimpleNamespace(mark_message_tainted=lambda *_a, **_k: None)
    tool = FetchUrlTool(bot)
    msg = SimpleNamespace(id=1, channel=SimpleNamespace(id=2), guild=None)
    result = _run(tool.execute(msg, url="https://ex.com/a"))
    assert "readable article" in result
    assert not result.startswith("Error")


def test_fetch_url_taints_on_untrusted_page(monkeypatch):
    tainted = {}
    session = FakeSession(
        {
            "https://ex.com/doc": FakeResp(
                200,
                headers={"Content-Type": "text/plain"},
                body=b"untrusted",
            ),
        }
    )

    async def _session():
        return session

    monkeypatch.setattr("bot_tools._get_shared_session", _session)
    bot = SimpleNamespace(
        mark_message_tainted=lambda *_a, **_k: tainted.setdefault("ok", True)
    )
    result = _run(
        FetchUrlTool(bot).execute(
            SimpleNamespace(id=1, channel=SimpleNamespace(id=2)),
            url="https://ex.com/doc",
        )
    )
    assert result == "untrusted"
    assert tainted.get("ok") is True
