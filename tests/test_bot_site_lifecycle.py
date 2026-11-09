"""Expired sites stay tracked until their teardown finishes."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from bot import MaxwellBot
import bot_parts.settings as settings


@pytest.mark.parametrize("failure_stage", ["files", "backend"])
def test_failed_expiry_teardown_remains_tracked_for_next_cleanup(
    tmp_path, monkeypatch, failure_stage
):
    data_dir = tmp_path / "data"
    site_dir = tmp_path / "sites"
    data_dir.mkdir()
    site_dir.mkdir()
    entries = {
        "retry-site": {"created_at": 1, "ttl_hours": 1},
        "done-site": {"created_at": 1, "ttl_hours": 1},
    }
    for slug in entries:
        directory = site_dir / slug
        directory.mkdir()
        (directory / "index.html").write_text(slug, encoding="utf-8")
    registry = data_dir / "sites.json"
    registry.write_text(json.dumps(entries), encoding="utf-8")
    bot = object.__new__(MaxwellBot)
    bot.config = SimpleNamespace(DATA_DIR=str(data_dir), MAXWELL_SITE_DIR=str(site_dir))
    bot._control = {}
    bot._sites = {}
    bot._sites_mtime = 0.0
    real_rmtree = settings.shutil.rmtree
    failed = False

    def remove_tree(path, *args, **kwargs):
        nonlocal failed
        if failure_stage == "files" and path.name == "retry-site" and not failed:
            failed = True
            raise OSError("site files temporarily unavailable")
        return real_rmtree(path, *args, **kwargs)

    async def destroy_backend(_data_dir, slug):
        nonlocal failed
        if failure_stage == "backend" and slug == "retry-site" and not failed:
            failed = True
            raise OSError("backend teardown temporarily unavailable")

    monkeypatch.setattr(settings.shutil, "rmtree", remove_tree)
    monkeypatch.setattr(settings.site_backend, "destroy", Mock())
    backend = AsyncMock(side_effect=destroy_backend)
    monkeypatch.setattr(settings.site_server, "destroy", backend)

    async def run():
        await bot._cleanup_sites()
        assert failed
        assert json.loads(registry.read_text(encoding="utf-8")) == {
            "retry-site": entries["retry-site"]
        }
        assert bot._sites == {"retry-site": entries["retry-site"]}
        assert not (site_dir / "done-site").exists()
        if failure_stage == "files":
            assert (site_dir / "retry-site" / "index.html").read_text() == "retry-site"

        await bot._cleanup_sites()
        assert bot._sites == {}
        assert json.loads(registry.read_text(encoding="utf-8")) == {}
        assert not (site_dir / "retry-site").exists()
        expected_backend_attempts = 2 if failure_stage == "backend" else 1
        assert sum(call.args[1] == "retry-site" for call in backend.await_args_list) == expected_backend_attempts

    asyncio.run(run())
