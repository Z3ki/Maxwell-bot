"""Public /u/<handle>/ hosting and signed /u/_s/ downloads."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from api.auth import _auth_middleware_unless_login
from api.user_content import (
    SANDBOX_CSP,
    inject_shim,
    open_user_storage,
    register_user_content,
)
from message_quota import MessageQuota

UID = "111111111111111111"
FREE = "222222222222222222"


@asynccontextmanager
async def client_for(tmp_path, monkeypatch):
    monkeypatch.setenv("MAXWELL_ADMIN_USER", "operator")
    monkeypatch.setenv("MAXWELL_ADMIN_PASSWORD", "operator-secret")
    MessageQuota(tmp_path / "message_quota.sqlite3").configure(UID, plan="premium")
    storage = open_user_storage(tmp_path)
    storage.set_handle(UID, "7up")
    storage.set_handle(FREE, "freebie")
    storage.write(
        UID,
        "public/cat-3d/index.html",
        b"<!doctype html><html><head><title>c</title></head><body>meow</body></html>",
    )
    storage.write(UID, "public/cat-3d/game.js", b"console.log(1)")
    storage.write(UID, "private/build.mcaddon", b"PK\x03\x04zip")
    app = web.Application(middlewares=[_auth_middleware_unless_login])
    register_user_content(app, lambda: tmp_path)

    async def operator(request):
        return web.json_response({"private": True})

    app.router.add_get("/api/control", operator)
    async with TestClient(TestServer(app)) as client:
        yield client, storage


def run(coro):
    asyncio.run(coro)


def test_public_folder_serves_index_with_sandbox(tmp_path, monkeypatch):
    async def go():
        async with client_for(tmp_path, monkeypatch) as (client, _):
            response = await client.get("/u/7up/cat-3d/")
            assert response.status == 200
            body = await response.text()
            assert "meow" in body and "localStorage" in body
            assert body.startswith("<!doctype html><html><head><script>")
            assert response.headers["Content-Security-Policy"] == SANDBOX_CSP
            assert (
                "allow-same-origin" not in response.headers["Content-Security-Policy"]
            )
            assert response.headers["X-Content-Type-Options"] == "nosniff"
            js = await client.get("/u/7up/cat-3d/game.js")
            assert js.status == 200
            assert js.headers["Content-Type"].startswith("text/javascript")
            assert js.headers["Access-Control-Allow-Origin"] == "*"
            assert await js.text() == "console.log(1)"
            # Operator routes stay behind admin auth.
            assert (await client.get("/api/control")).status == 401

    run(go())


def test_folder_redirects_and_misses(tmp_path, monkeypatch):
    async def go():
        async with client_for(tmp_path, monkeypatch) as (client, _):
            response = await client.get("/u/7up/cat-3d", allow_redirects=False)
            assert response.status == 302
            assert response.headers["Location"] == "cat-3d/"
            response = await client.get("/u/7up", allow_redirects=False)
            assert response.status == 302 and response.headers["Location"] == "7up/"
            assert (await client.get("/u/7up/")).status == 404  # no index, no listing
            assert (await client.get("/u/7up/nope.html")).status == 404
            assert (await client.get("/u/7up/../private/build.mcaddon")).status == 404
            assert (
                await client.get("/u/7up/%2e%2e/private/build.mcaddon")
            ).status == 404
            assert (await client.get("/u/nobody/x")).status == 404
            # A path through an existing file is a miss, not a server error.
            assert (await client.get("/u/7up/cat-3d/index.html/x")).status == 404
            assert (await client.get("/u/7up/cat-3d/game.js/extra")).status == 404

    run(go())


def test_private_files_are_not_public_and_free_users_are_not_hosted(
    tmp_path, monkeypatch
):
    async def go():
        async with client_for(tmp_path, monkeypatch) as (client, storage):
            assert (await client.get("/u/7up/private/build.mcaddon")).status == 404
            # A plan lapse keeps files but stops hosting them.
            target = storage.user_dir(FREE) / "public" / "index.html"
            target.parent.mkdir(parents=True)
            target.write_text("hi")
            assert (await client.get("/u/freebie/")).status == 404

    run(go())


def test_symlinks_in_public_tree_are_refused(tmp_path, monkeypatch):
    async def go():
        async with client_for(tmp_path, monkeypatch) as (client, storage):
            secret = tmp_path / "secret.txt"
            secret.write_text("operator secret")
            os.symlink(secret, storage.user_dir(UID) / "public" / "leak.txt")
            os.symlink(
                storage.user_dir(UID) / "private",
                storage.user_dir(UID) / "public" / "dir",
            )
            assert (await client.get("/u/7up/leak.txt")).status == 404
            assert (await client.get("/u/7up/dir/build.mcaddon")).status == 404

    run(go())


def test_signed_links_expire_and_bind_path(tmp_path, monkeypatch):
    async def go():
        async with client_for(tmp_path, monkeypatch) as (client, storage):
            sig, expires = storage.sign(UID, "private/build.mcaddon", ttl=3600)
            url = f"/u/_s/{UID}/{expires}/{sig}/private/build.mcaddon"
            response = await client.get(url)
            assert response.status == 200
            assert await response.read() == b"PK\x03\x04zip"
            assert response.headers["Content-Disposition"].startswith("attachment;")
            assert response.headers["Content-Security-Policy"] == SANDBOX_CSP
            assert (
                await client.get(f"/u/_s/{UID}/{expires}/{sig}/public/cat-3d/game.js")
            ).status == 404
            assert (
                await client.get(f"/u/_s/{FREE}/{expires}/{sig}/private/build.mcaddon")
            ).status == 404
            assert (
                await client.get(
                    f"/u/_s/{UID}/{expires + 1}/{sig}/private/build.mcaddon"
                )
            ).status == 404
            old_sig, old = storage.sign(UID, "private/build.mcaddon", ttl=60)
            monkeypatch.setattr("time.time", lambda: old + 10)
            assert (
                await client.get(f"/u/_s/{UID}/{old}/{old_sig}/private/build.mcaddon")
            ).status == 404

    run(go())


def test_shim_lands_after_head_without_breaking_doctype():
    assert inject_shim(b"<!DOCTYPE html><p>x").startswith(b"<!DOCTYPE html><script>")
    assert inject_shim(b"<p>bare</p>").startswith(b"<script>")
    out = inject_shim(b'<html lang="en"><body>x')
    assert out.startswith(b'<html lang="en"><script>')
