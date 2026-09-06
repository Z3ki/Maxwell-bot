"""create_site must treat a backend as optional, not mandatory.

A static page is a valid site. Prompts and schemas must not herd the model
into backend=true, and execute must still default the flag to False.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from bot import TOOL_PROTOCOL
from bot_tools import CreateSiteTool
from tool_schemas import TOOL_PARAMETERS

_BANNED_BACKEND_PHRASES = (
    "always true",
    "must use backend=true",
    "client-only sites are forbidden",
    "backend is mandatory",
)

_PAGE = (
    "<!DOCTYPE html><html><head><title>static</title></head>"
    "<body><h1>hello</h1><p>a finished static page</p></body></html>"
)


def _site_bot(tmp_path: Path | None = None) -> SimpleNamespace:
    if tmp_path is None:
        return SimpleNamespace(
            config=SimpleNamespace(
                MAXWELL_SITE_DIR="public/bot",
                MAXWELL_PUBLIC_BASE_URL="https://maxwell.example.com",
            )
        )
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


def _mentions_optional_backend(text: str) -> bool:
    low = text.lower()
    if "backend" in low and "not required" in low:
        return True
    start = 0
    while True:
        idx = low.find("backend", start)
        if idx < 0:
            return False
        window = low[max(0, idx - 48) : idx + 96]
        if "optional" in window or "not required" in window:
            return True
        start = idx + 1


def test_create_site_description_backend_is_optional():
    desc = CreateSiteTool(_site_bot()).get_description()
    low = desc.lower()
    for phrase in _BANNED_BACKEND_PHRASES:
        assert phrase not in low, f"create_site description still mandates {phrase!r}"
    assert _mentions_optional_backend(desc)
    assert len(desc) < 1024


def test_create_site_backend_schema_is_not_mandatory():
    desc = TOOL_PARAMETERS["create_site"]["properties"]["backend"]["description"]
    low = desc.lower()
    assert "always true" not in low
    assert "must use backend=true" not in low
    assert "forbidden" not in low
    assert "client-only sites are forbidden" not in low


def test_tool_protocol_backend_is_optional():
    text = TOOL_PROTOCOL
    low = text.lower()
    assert "BACKEND IS MANDATORY" not in text
    assert "Client-only sites are forbidden" not in text
    assert "MUST use backend=true" not in text
    assert "backend is mandatory" not in low
    assert "must use backend=true" not in low
    assert "client-only sites are forbidden" not in low
    assert "z3ki authorization" not in low
    assert _mentions_optional_backend(text)


def test_create_site_execute_defaults_backend_to_false(tmp_path):
    bot = _site_bot(tmp_path)
    tool = CreateSiteTool(bot)
    message = SimpleNamespace(author=SimpleNamespace(id=42, display_name="tester"))

    async def run():
        return await tool.execute(
            message,
            name="static",
            title="Static",
            body=_PAGE,
            backend=None,
        )

    result = asyncio.run(run())
    assert result.startswith("Site created:")
    assert bot._sites["static"]["backend"] is False
