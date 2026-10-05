"""Shared-file token mutations across the bot, OAuth API, and worker threads.

All credentials are synthetic and every file lives under pytest's tmp_path.
Events coordinate the stale-snapshot race without making live OAuth requests.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
import hashlib
import json
import multiprocessing
from pathlib import Path
import threading

import pytest

import plugins.github_projects.impl as github
from utils import FileLock, FileLockTimeout


def _paused_oauth_write(path, ready, release):
    """Hold write's real file lock after it has read the old snapshot."""
    original = github._json_atomic

    def paused_replace(target, data):
        ready.set()
        if not release.wait(10):
            raise TimeoutError("test writer was not released")
        original(target, data)

    github._json_atomic = paused_replace
    github.TokenStore(Path(path)).write("new-user", "synthetic-oauth-token")


def _concurrent_writer(path, prefix, barrier, asynchronous):
    store = github.TokenStore(Path(path))
    barrier.wait(timeout=10)

    async def write_async():
        for index in range(5):
            await store.set(f"{prefix}-{index}", "synthetic-worker-token")

    if asynchronous:
        asyncio.run(write_async())
    else:
        for index in range(5):
            store.write(f"{prefix}-{index}", "synthetic-worker-token")


def _oauth_write_signaling_lock_attempt(path, requested):
    class ObservedFileLock(FileLock):
        def __enter__(self):
            requested.set()
            return super().__enter__()

    github.FileLock = ObservedFileLock
    github.TokenStore(Path(path)).write("new-user", "synthetic-oauth-token")


@contextmanager
def _running_process(context, target, args):
    process = context.Process(target=target, args=args)
    process.start()
    try:
        yield process
    finally:
        process.join(timeout=10)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        assert process.exitcode == 0, "token test worker did not complete cleanly"


def _digest(path):
    return hashlib.sha256(path.read_bytes()).digest()


def _row_digest(row):
    # Assertion diagnostics should compare integrity without displaying tokens.
    return hashlib.sha256(json.dumps(row, sort_keys=True).encode()).digest()


@pytest.mark.parametrize("logout_uid, expected", [("logout-user", True), ("absent", False)])
def test_oauth_write_cannot_resurrect_logout_or_lose_new_user(
    tmp_path, monkeypatch, logout_uid, expected
):
    path = tmp_path / "tokens.json"
    store = github.TokenStore(path)
    store.write("logout-user", "synthetic-logout-token")
    store.write("other-user", "synthetic-unrelated-token", scopes=["repo"])
    unrelated = _row_digest(store.row("other-user"))
    context = multiprocessing.get_context("spawn")
    snapshot_ready, release_writer = context.Event(), context.Event()
    lock_requested = threading.Event()

    class ObservedFileLock(FileLock):
        def __enter__(self):
            assert threading.current_thread() is not threading.main_thread(), (
                "async logout waited for a file lock on the event loop"
            )
            lock_requested.set()
            return super().__enter__()

    async def logout_while_oauth_is_paused():
        with _running_process(
            context, _paused_oauth_write, (str(path), snapshot_ready, release_writer)
        ):
            assert await asyncio.to_thread(snapshot_ready.wait, 10), (
                "OAuth writer did not reach its stale snapshot"
            )
            monkeypatch.setattr(github, "FileLock", ObservedFileLock)
            logout = asyncio.create_task(store.clear(logout_uid))
            try:
                assert await asyncio.to_thread(lock_requested.wait, 5), (
                    "logout did not request the shared token-file lock"
                )
                assert not logout.done(), "logout ran while OAuth held its old snapshot"
            finally:
                release_writer.set()
            assert await asyncio.wait_for(logout, 10) is expected

    asyncio.run(logout_while_oauth_is_paused())
    if expected:
        assert not store.peek("logout-user")
    else:
        assert bool(store.peek("logout-user"))
    assert bool(store.peek("new-user")), "logout discarded the completed OAuth write"
    assert _row_digest(store.row("other-user")) == unrelated


def test_bot_and_api_writers_preserve_every_unrelated_user(tmp_path):
    path = tmp_path / "tokens.json"
    store = github.TokenStore(path)
    store.write("existing", "synthetic-existing-token")
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(3)
    with _running_process(
        context, _concurrent_writer, (str(path), "api", barrier, False)
    ), _running_process(
        context, _concurrent_writer, (str(path), "bot", barrier, True)
    ):
        barrier.wait(timeout=10)

    assert set(store._read()) == {
        "existing",
        *(f"api-{index}" for index in range(5)),
        *(f"bot-{index}" for index in range(5)),
    }
    assert all(bool(store.peek(uid)) for uid in store._read())


def test_logout_snapshot_cannot_overwrite_an_oauth_write(tmp_path, monkeypatch):
    path = tmp_path / "tokens.json"
    store = github.TokenStore(path)
    store.write("logout-user", "synthetic-logout-token")
    store.write("other-user", "synthetic-unrelated-token")
    unrelated = _row_digest(store.row("other-user"))
    snapshot_ready, release_logout = threading.Event(), threading.Event()
    context = multiprocessing.get_context("spawn")
    oauth_requested = context.Event()
    original_replace = github._json_atomic

    def paused_logout_replace(target, data):
        snapshot_ready.set()
        if not release_logout.wait(10):
            raise TimeoutError("test logout was not released")
        original_replace(target, data)

    monkeypatch.setattr(github, "_json_atomic", paused_logout_replace)

    async def run():
        logout = asyncio.create_task(store.clear("logout-user"))
        try:
            assert await asyncio.to_thread(snapshot_ready.wait, 5)
            # The delete must still hold its lock when publishing the snapshot.
            # This rejects the old unlocked clear deterministically.
            with pytest.raises(FileLockTimeout), FileLock(path, timeout=0.01):
                pass
            with _running_process(
                context,
                _oauth_write_signaling_lock_attempt,
                (str(path), oauth_requested),
            ):
                try:
                    assert await asyncio.to_thread(oauth_requested.wait, 5)
                finally:
                    release_logout.set()
                assert await asyncio.wait_for(logout, 5) is True
        finally:
            release_logout.set()

    asyncio.run(run())
    assert not store.peek("logout-user")
    assert bool(store.peek("new-user")), "stale logout snapshot lost the OAuth write"
    assert _row_digest(store.row("other-user")) == unrelated


@pytest.mark.parametrize("operation", ["set", "clear"])
def test_async_token_mutation_keeps_event_loop_responsive(
    tmp_path, monkeypatch, operation
):
    path = tmp_path / "tokens.json"
    store = github.TokenStore(path)
    store.write("target", "synthetic-token")
    lock_requested = threading.Event()

    class ObservedFileLock(FileLock):
        def __enter__(self):
            assert threading.current_thread() is not threading.main_thread(), (
                "blocking token-file transaction ran on the event loop"
            )
            lock_requested.set()
            return super().__enter__()

    monkeypatch.setattr(github, "FileLock", ObservedFileLock)

    async def run():
        held_lock = FileLock(path, timeout=1)
        held_lock.__enter__()
        try:
            mutation = asyncio.create_task(
                store.set("target", "synthetic-replacement")
                if operation == "set"
                else store.clear("target")
            )
            assert await asyncio.to_thread(lock_requested.wait, 5)
            heartbeat = asyncio.Event()
            asyncio.get_running_loop().call_soon(heartbeat.set)
            await asyncio.wait_for(heartbeat.wait(), 1)
            assert not mutation.done()
        finally:
            held_lock.__exit__(None, None, None)
        await asyncio.wait_for(mutation, 5)

    asyncio.run(run())


@pytest.mark.parametrize("operation", ["write", "set", "clear"])
def test_token_mutation_lock_timeout_fails_without_reading_or_replacing(
    tmp_path, monkeypatch, operation
):
    path = tmp_path / "tokens.json"
    store = github.TokenStore(path)
    store.write("target", "synthetic-token")
    before = _digest(path)

    class ShortFileLock(FileLock):
        def __init__(self, target, timeout):
            assert timeout == 5.0
            super().__init__(target, timeout=0.01)

    def forbidden_read():
        raise AssertionError("timed-out mutation read a credential snapshot")

    monkeypatch.setattr(github, "FileLock", ShortFileLock)
    monkeypatch.setattr(store, "_read", forbidden_read)
    with FileLock(path, timeout=1), pytest.raises(FileLockTimeout):
        if operation == "write":
            store.write("new-user", "synthetic-token")
        elif operation == "set":
            asyncio.run(store.set("new-user", "synthetic-token"))
        else:
            asyncio.run(store.clear("target"))
    assert _digest(path) == before


@pytest.mark.parametrize("operation", ["write", "set", "clear"])
def test_failed_atomic_replace_releases_token_lock(tmp_path, monkeypatch, operation):
    path = tmp_path / "tokens.json"
    store = github.TokenStore(path)
    store.write("target", "synthetic-token")
    before = _digest(path)

    def failed_replace(*args, **kwargs):
        raise OSError("synthetic disk failure")

    with monkeypatch.context() as patch:
        patch.setattr(github, "_json_atomic", failed_replace)
        with pytest.raises(OSError, match="synthetic disk failure"):
            if operation == "write":
                store.write("new-user", "synthetic-token")
            elif operation == "set":
                asyncio.run(store.set("new-user", "synthetic-token"))
            else:
                asyncio.run(store.clear("target"))
    assert _digest(path) == before
    asyncio.run(github.TokenStore(path).set("other-user", "synthetic-token"))
    assert bool(store.peek("other-user")), "failed mutation kept the shared lock"
    assert bool(store.peek("target"))


def test_concurrent_logout_returns_true_once_and_preserves_other_users(tmp_path):
    path = tmp_path / "tokens.json"
    initial = github.TokenStore(path)
    initial.write("target", "synthetic-token")
    initial.write("other-user", "synthetic-token", scopes=["repo", "workflow"])
    unrelated = _row_digest(initial.row("other-user"))

    async def run():
        stores = [github.TokenStore(path) for _ in range(8)]
        return await asyncio.gather(*(store.clear("target") for store in stores))

    results = asyncio.run(run())
    assert results.count(True) == 1
    assert results.count(False) == 7
    assert not initial.peek("target")
    assert _row_digest(initial.row("other-user")) == unrelated


def test_logout_is_idempotent_without_rewriting_unrelated_tokens(tmp_path):
    path = tmp_path / "tokens.json"
    store = github.TokenStore(path)
    store.write("target", "synthetic-token")
    store.write("other-user", "synthetic-token", scopes=["repo"])
    unrelated = _row_digest(store.row("other-user"))
    assert asyncio.run(store.clear("target")) is True
    after_logout = path.stat()
    assert asyncio.run(store.clear("target")) is False
    after_repeat = path.stat()
    assert (after_repeat.st_ino, after_repeat.st_mtime_ns) == (
        after_logout.st_ino,
        after_logout.st_mtime_ns,
    )
    assert _row_digest(store.row("other-user")) == unrelated


def test_logout_without_token_file_does_not_create_a_credential_snapshot(tmp_path):
    path = tmp_path / "nested" / "tokens.json"
    store = github.TokenStore(path)
    assert asyncio.run(store.clear("absent")) is False
    assert not path.exists()
    assert not store.peek("absent")
