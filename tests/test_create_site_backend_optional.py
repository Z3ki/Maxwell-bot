"""Static pages are published without requiring a backend."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from bot_tools import CreateSiteTool


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
    control = {}
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


def test_create_site_has_no_per_user_site_count_cap(tmp_path):
    bot = _site_bot(tmp_path)
    # Old persisted settings may still carry this field; it is ignored.
    bot.control["create_site_quota_per_user"] = 1
    tool = CreateSiteTool(bot)
    message = SimpleNamespace(author=SimpleNamespace(id=42, display_name="tester"))

    async def run():
        return [
            await tool.execute(
                message,
                name=f"site-{index}",
                title=f"Site {index}",
                body=_PAGE,
                backend=False,
            )
            for index in range(11)
        ]

    results = asyncio.run(run())
    assert all(result.startswith("Site created:") for result in results)
    assert len(bot._sites) == 11
