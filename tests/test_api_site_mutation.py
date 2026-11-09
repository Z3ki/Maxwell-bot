"""Operator deletion and site recreation share the full per-slug lifecycle lock."""

import asyncio
import json
from types import SimpleNamespace

import pytest

import api.api_server as api
from utils import FileLock, FileLockTimeout


def test_api_delete_blocks_reclaim_until_backend_cleanup_finishes(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "DATA_DIR", tmp_path)
    public = tmp_path / "public"
    public.mkdir()
    monkeypatch.setattr(api, "BASE_SITE_DIR", public)
    site = public / "demo"
    site.mkdir()
    (site / "index.html").write_text("old owner")
    metadata = tmp_path / "sites.json"
    metadata.write_text(json.dumps({"demo": {"user_id": "old-owner"}}))
    monkeypatch.setattr(api.site_backend, "destroy", lambda *args: None)

    async def run():
        entered, release = asyncio.Event(), asyncio.Event()

        async def destroy(*args):
            entered.set()
            await release.wait()

        monkeypatch.setattr(api.site_server, "destroy", destroy)
        deletion = asyncio.create_task(api.site_delete(SimpleNamespace(query={"slug": "demo"})))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            # Metadata is already gone, but a creator must not take this slug
            # until its old container and state have also been removed.
            assert "demo" not in json.loads(metadata.read_text())
            with pytest.raises(FileLockTimeout):
                with FileLock(tmp_path / "site_locks/demo.lock", timeout=0):
                    pass
        finally:
            release.set()
            response = await deletion
        assert response.status == 200
        with FileLock(tmp_path / "site_locks/demo.lock", timeout=0):
            site.mkdir()
            (site / "index.html").write_text("new owner")
            metadata.write_text(json.dumps({"demo": {"user_id": "new-owner"}}))
        assert (site / "index.html").read_text() == "new owner"
        assert json.loads(metadata.read_text())["demo"]["user_id"] == "new-owner"

    asyncio.run(run())
