"""Regression tests for the second-opinion review of the audit branch.

Each test pins a defect in (or left by) the first-pass audit fixes.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest


# --------------------------------------------------------------------------
# typing_status: releasing typing must not raise the cancelled typing task's
# CancelledError into the caller
# --------------------------------------------------------------------------
class _RealishTyping:
    """Mirrors discord.py's Typing: __aexit__ cancels a background task."""

    def __init__(self):
        self.task = None

    async def __aenter__(self):
        self.task = asyncio.get_running_loop().create_task(asyncio.sleep(3600))

    async def __aexit__(self, *exc):
        self.task.cancel()


class _Channel:
    def __init__(self):
        self.cms = []

    def typing(self):
        cm = _RealishTyping()
        self.cms.append(cm)
        return cm


def test_typing_release_returns_normally():
    from typing_status import ChannelTyping

    async def run():
        status = ChannelTyping(_Channel())
        await status.acquire()
        await status.release()  # must not raise CancelledError
        return status.active

    assert asyncio.run(run()) is False


def test_typing_release_still_propagates_caller_cancellation():
    from typing_status import ChannelTyping

    async def run():
        channel = _Channel()
        status = ChannelTyping(channel)
        await status.acquire()
        started = asyncio.Event()

        async def releaser():
            started.set()
            await status.release()
            return "finished"

        task = asyncio.ensure_future(releaser())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())


# --------------------------------------------------------------------------
# tools.results: a negation after the promise phrase is not a refusal
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        "lemme check why it's not loading",
        "I'll fix the thing that isn't working",
        "I can't make it but I'll check",
    ],
)
def test_promise_about_a_negated_problem_still_loops(text):
    from maxwell_core.tools.results import _promises_followup_work

    assert _promises_followup_work(f"Tool send_message: __MESSAGE_SENT__\n{text}")


@pytest.mark.parametrize(
    "text",
    [
        "I'm not going to create that site.",
        "i'm not gonna build that",
        "I really don't think I'll make it",
    ],
)
def test_negated_promise_phrase_is_a_refusal(text):
    from maxwell_core.tools.results import _promises_followup_work

    assert not _promises_followup_work(f"Tool send_message: __MESSAGE_SENT__\n{text}")


# --------------------------------------------------------------------------
# providers.streaming: a call cut off after a nested object closed is still
# a truncated tool call
# --------------------------------------------------------------------------
def test_truncated_call_after_nested_object_is_rejected():
    from maxwell_core.providers.custom_tools import _CustomToolCallBuffer

    buf = _CustomToolCallBuffer()
    buf.feed('sure {"name": "shell", "arguments": {"command": "ls"}')
    assert buf.truncated_tool_call is True

    done = _CustomToolCallBuffer()
    done.feed('{"name": "shell", "arguments": {"command": "ls"}}')
    assert done.truncated_tool_call is False

    prose = _CustomToolCallBuffer()
    prose.feed("a stray { brace in prose")
    assert prose.truncated_tool_call is False


# --------------------------------------------------------------------------
# runtime_lifecycle: every SHARD_ID out of range must not launch all shards
# --------------------------------------------------------------------------
def test_all_shard_ids_out_of_range_refuses_to_launch(monkeypatch):
    from runtime_lifecycle import shard_launch_kwargs

    monkeypatch.setenv("SHARD_COUNT", "2")
    monkeypatch.setenv("SHARD_IDS", "2,3")
    with pytest.raises(ValueError):
        shard_launch_kwargs()


# --------------------------------------------------------------------------
# recovery: a failed batched suppression falls back to the per-row sweep
# --------------------------------------------------------------------------
def test_failed_batch_suppression_falls_back_to_per_row():
    from bot_parts.recovery import RecoveryMixin

    updated = []

    class Journal:
        def suppress_stale(self, cutoff, *, reason):
            raise RuntimeError("database is locked")

        def pending(self, limit):
            return [
                {"message_id": "1", "created_at": 10.0},
                {"message_id": "2", "created_at": 500.0},
            ]

        def update(self, message_id, status, *, reason=""):
            updated.append((message_id, status, reason))

    bot = SimpleNamespace(_request_journal=Journal(), _process_started_at=100.0)
    dropped = RecoveryMixin._abandon_offline_inbound(bot)
    assert dropped == 1
    assert updated == [("1", "suppressed", "stale_offline")]


# --------------------------------------------------------------------------
# shell: an idle reaper with only busy past-idle tenants must yield
# --------------------------------------------------------------------------
def test_idle_reaper_yields_while_past_idle_tenant_is_busy(monkeypatch):
    from plugins.shell.impl import ShellTool

    name = "maxwell-shell-review-test"
    monkeypatch.setattr(
        ShellTool, "_last_used_by_tenant", {name: time.monotonic() - 10_000}
    )
    monkeypatch.setattr(ShellTool, "_active_tenants", {name})
    monkeypatch.setattr(ShellTool, "_tenants", {})
    monkeypatch.setattr(ShellTool, "_idle_reaper_task", None)

    async def run():
        reaper = asyncio.ensure_future(ShellTool._idle_reaper_loop())
        # A spinning reaper never yields, so this sleep would never return.
        await asyncio.wait_for(asyncio.sleep(0.05), timeout=2)
        assert not reaper.done()
        reaper.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reaper

    asyncio.run(run())


# --------------------------------------------------------------------------
# message_pipeline.Watermarks: a mark noted while save() is writing stays dirty
# --------------------------------------------------------------------------
def test_watermark_noted_during_save_is_not_lost(tmp_path, monkeypatch):
    import json

    import message_pipeline
    from message_pipeline import Watermarks

    marks = Watermarks(str(tmp_path / "wm.json"))
    marks.note("1", 100)
    real_dump = json.dump

    def dump_then_note(payload, handle):
        real_dump(payload, handle)
        marks.note("2", 200)  # the event loop notes a receipt mid-write

    monkeypatch.setattr(message_pipeline.json, "dump", dump_then_note)
    marks.save()
    monkeypatch.setattr(message_pipeline.json, "dump", real_dump)
    assert marks._dirty is True
    marks.save()
    saved = json.loads((tmp_path / "wm.json").read_text())
    assert saved["channels"] == {"1": "100", "2": "200"}
