"""Focused regressions for transport output and lifecycle review findings."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from maxwell_core.transport.output_safety import (
    _sanitize_visible_reply,
    strip_tool_payload_leaks,
)
from tool_progress import ToolProgress
from typing_status import ChannelTyping


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("wrapper", [None, "function", "tool_calls"])
def test_tool_json_is_removed_with_nested_arguments_in_either_order(reverse, wrapper):
    fields = [
        ("name", "shell"),
        ("arguments", {"nested": {"value": 'SECRET_PAYLOAD } { "quoted"'}}),
    ]
    if reverse:
        fields.reverse()
    payload = dict(fields)
    if wrapper == "function":
        payload = {"function": payload}
    elif wrapper == "tool_calls":
        payload = {"tool_calls": [{"id": "call", "function": payload}]}
    raw = "Before " + json.dumps(payload) + " After"
    assert strip_tool_payload_leaks(raw) == "Before  After"
    assert _sanitize_visible_reply(raw, scrub_repeats=False) == "Before  After"


@pytest.mark.parametrize("key", ["reasoning", "PLAN", "internal_monologue", "action_plan"])
def test_nested_reasoning_json_is_removed_as_a_complete_object(key):
    raw = json.dumps({key: {"step": {"first": "private"}, "second": "SECRET_PAYLOAD"}})
    assert strip_tool_payload_leaks(raw + "\nPublic answer.") == "Public answer."


def test_adjacent_reasoning_objects_do_not_leave_private_suffixes():
    raw = '{"reasoning":{"step":"private"}} {"plan":{"step":"SECRET_PAYLOAD"}} Public answer.'
    assert strip_tool_payload_leaks(raw) == "Public answer."


def test_truncated_think_block_does_not_become_visible():
    assert strip_tool_payload_leaks("Public answer. <think>SECRET_PAYLOAD") == "Public answer."
    assert strip_tool_payload_leaks("<think>SECRET_PAYLOAD") == ""


def test_json_examples_in_code_keep_literal_reasoning_and_tool_fields():
    example = '````python\n{"reasoning":{"step":"example"},"name":"shell","arguments":{}}\n````'
    assert strip_tool_payload_leaks(example) == example
    assert _sanitize_visible_reply(example) == example


def test_plain_json_is_not_mistaken_for_a_tool_or_reasoning_object():
    text = '{"result":{"nested":{"value":"a } brace"}},"status":"ok"} Public answer.'
    assert strip_tool_payload_leaks(text) == text


def _assert_no_mentions(call):
    mentions = call.kwargs["allowed_mentions"]
    assert mentions.to_dict()["parse"] == []
    assert mentions.replied_user is False


def test_progress_post_and_reply_fallback_cannot_ping_mentions():
    async def run():
        posted = SimpleNamespace()
        channel = SimpleNamespace(send=AsyncMock(return_value=posted))
        message = SimpleNamespace(channel=channel, reply=AsyncMock(return_value=posted))
        progress = ToolProgress(message)
        text = "@everyone @here <@123> <@&456>"
        assert await progress._post_reply(text) is posted
        _assert_no_mentions(channel.send.await_args)
        channel.send.side_effect = RuntimeError("unavailable channel")
        assert await progress._post_reply(text) is posted
        _assert_no_mentions(message.reply.await_args)

    asyncio.run(run())


def test_progress_edits_and_final_fallback_cannot_ping_mentions():
    async def run():
        posted = SimpleNamespace(edit=AsyncMock(), delete=AsyncMock())
        message = SimpleNamespace(reply=AsyncMock(side_effect=RuntimeError("send failed")))
        progress = ToolProgress(message)
        progress._posted = posted
        await progress._flush("@everyone <@&456>")
        _assert_no_mentions(posted.edit.await_args)
        await progress._background_transition(posted, "@everyone <@&456>")
        assert posted.edit.await_count == 2
        _assert_no_mentions(posted.edit.await_args)
        posted.delete.assert_not_awaited()

    asyncio.run(run())


def test_non_discord_progress_keeps_adapter_call_signature():
    async def run():
        async def send(content):
            return content

        message = SimpleNamespace(tool_platform="adapter", channel=SimpleNamespace(send=send))
        assert await ToolProgress(message)._post_reply("Inspecting files.") == "Inspecting files."

    asyncio.run(run())


def test_cancelled_typing_suspend_still_drains_the_detached_loop():
    async def run():
        started = asyncio.Event()
        exiting = asyncio.Event()
        allow_exit = asyncio.Event()
        finished = asyncio.Event()

        class Typing:
            async def __aenter__(self):
                async def loop():
                    started.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        finished.set()

                self.task = asyncio.create_task(loop())
                await started.wait()
                return self

            async def __aexit__(self, *_args):
                exiting.set()
                await allow_exit.wait()

        cm = Typing()
        status = ChannelTyping(SimpleNamespace(typing=lambda: cm))
        handle = await status.acquire()
        suspension = asyncio.create_task(status.suspend())
        await exiting.wait()
        suspension.cancel()
        await asyncio.sleep(0)
        allow_exit.set()
        try:
            with pytest.raises(asyncio.CancelledError):
                await suspension
            assert cm.task.done()
            assert finished.is_set()
            assert not status.active
            await handle.release()
            assert status.holders == 0
        finally:
            if not cm.task.done():
                cm.task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await cm.task

    asyncio.run(run())
