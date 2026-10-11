"""Drain timeout, shard launch arguments, and reply-queue pause."""

from __future__ import annotations

import asyncio

from maxwell_core.tools.spec import ToolSpec
from message_pipeline import ReplyQueue
from runtime_lifecycle import drain_timeout_seconds, shard_launch_kwargs


def test_drain_timeout_defaults_and_clamps(monkeypatch):
    monkeypatch.delenv("DRAIN_TIMEOUT_SECONDS", raising=False)
    assert drain_timeout_seconds() == 30.0
    monkeypatch.setenv("DRAIN_TIMEOUT_SECONDS", "nope")
    assert drain_timeout_seconds() == 30.0
    monkeypatch.setenv("DRAIN_TIMEOUT_SECONDS", "0")
    assert drain_timeout_seconds() == 0.0
    monkeypatch.setenv("DRAIN_TIMEOUT_SECONDS", "45")
    assert drain_timeout_seconds() == 45.0
    monkeypatch.setenv("DRAIN_TIMEOUT_SECONDS", "9999")
    assert drain_timeout_seconds() == 600.0
    monkeypatch.setenv("DRAIN_TIMEOUT_SECONDS", "-4")
    assert drain_timeout_seconds() == 0.0


def test_shard_kwargs_stay_empty_when_unset(monkeypatch):
    monkeypatch.delenv("SHARD_COUNT", raising=False)
    monkeypatch.delenv("SHARD_IDS", raising=False)
    assert shard_launch_kwargs() == {}


def test_shard_count_and_ids(monkeypatch):
    monkeypatch.delenv("SHARD_IDS", raising=False)
    monkeypatch.setenv("SHARD_COUNT", "4")
    assert shard_launch_kwargs() == {"shard_count": 4}
    monkeypatch.setenv("SHARD_IDS", "1, 3, -1, no")
    assert shard_launch_kwargs() == {"shard_count": 4, "shard_ids": [1, 3]}
    monkeypatch.delenv("SHARD_COUNT", raising=False)
    monkeypatch.setenv("SHARD_IDS", "0,2")
    assert shard_launch_kwargs() == {"shard_count": 3, "shard_ids": [0, 2]}


def test_user_install_can_use_discord_tools():
    spec = ToolSpec(name="music_control", plugin="music", tool=None, transports=("discord",))
    assert spec.available_on("user_install") is True
    assert spec.available_on("discord") is True
    assert spec.available_on("telegram") is False
    wide = ToolSpec(name="send_message", plugin="discord_messages", tool=None)
    assert wide.available_on("user_install") is True


def test_pause_new_does_not_start_the_next_turn():
    started: list[str] = []
    release = asyncio.Event()

    async def handler(message, _content):
        started.append(message)
        if message == "first":
            await release.wait()

    async def scenario():
        queue = ReplyQueue()
        queue.bind(handler)
        assert queue.submit("room", "first", "a", directed=True) == "started"
        for _ in range(50):
            if started:
                break
            await asyncio.sleep(0.01)
        assert queue.submit("room", "second", "b", directed=True) == "queued"
        queue.pause_new()
        assert queue.submit("room", "third", "c", directed=True) == "dropped"
        release.set()
        await asyncio.sleep(0.05)
        assert started == ["first"]
        await queue.close()

    asyncio.run(scenario())
