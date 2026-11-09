"""Raw HTML answers can be saved even for attachment-only user requests."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot import MaxwellBot


@pytest.mark.parametrize("original_content,title", [
    ("", "untitled site"),
    (" \n\t", "untitled site"),
    ("  Make a page\nwith a gallery", "Make a page"),
    ("x" * 100, "x" * 80),
])
def test_html_reply_routes_empty_and_multiline_requests_to_real_tool(monkeypatch, original_content, title):
    tool = object()
    bot = SimpleNamespace(tools={"create_site": tool})
    message = SimpleNamespace(id=7)
    html = "<!doctype html><html><body>Attachment-inspired page</body></html>"
    invoke = AsyncMock(return_value="https://example.invalid/bot/site-123")
    monkeypatch.setattr(MaxwellBot, "_invoke_request_tool", invoke)

    result = asyncio.run(MaxwellBot._auto_route_html_to_site(
        bot, message, html, original_content
    ))

    invoke.assert_awaited_once()
    assert invoke.await_args.args == (bot, message, "create_site", tool)
    assert invoke.await_args.kwargs["title"] == title
    assert invoke.await_args.kwargs["body"] == html
    assert invoke.await_args.kwargs["encoding"] == "text"
    assert "https://example.invalid/bot/site-123" in result
