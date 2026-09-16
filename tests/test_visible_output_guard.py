from __future__ import annotations

import asyncio
import sys
from types import MethodType, SimpleNamespace

from plugins.maxwell_extras.visible_output_guard import (
    install_visible_output_guard,
    tool_batch_has_visible_output,
    tool_result_is_visible,
)


def _turn_sent_message(tool_results):
    return any("__MESSAGE_SENT__" in str(item or "") for item in (tool_results or []))


async def _fake_handle_message(self, message):
    return None


async def _fake_dispatch_tool_calls(
    self,
    message,
    response,
    native_tool_calls=None,
    include_images=False,
):
    results = ["Tool send_rich_message: Sent rich embed message."]
    if include_images:
        return response, results, []
    return response, results


def _fake_bot():
    bot = SimpleNamespace()
    bot._handle_message = MethodType(_fake_handle_message, bot)
    bot._dispatch_tool_calls = MethodType(_fake_dispatch_tool_calls, bot)
    return bot


def test_visible_output_detection_covers_delivery_tools():
    assert tool_result_is_visible("Tool send_file: __FILE_SENT__ Sent file: song.mp3")
    assert tool_result_is_visible("Tool send_rich_message: Sent rich embed message.")
    assert tool_result_is_visible("Tool create_poll: Poll created")
    assert not tool_result_is_visible("Tool send_file: Error - upload failed")
    assert not tool_result_is_visible("Tool web_search: 3 results")
    assert tool_batch_has_visible_output(
        ["Tool web_search: 3 results", "Tool tts: __TTS_SENT__"]
    )


def test_native_tool_turn_drops_coemitted_here_text(monkeypatch):
    module = sys.modules[__name__]
    original_turn_sent = _turn_sent_message
    monkeypatch.setattr(module, "_turn_sent_message", original_turn_sent)

    bot = _fake_bot()
    install_visible_output_guard(bot)

    async def run():
        return await bot._dispatch_tool_calls(
            object(),
            "Here's an interactive question embed with buttons:",
            native_tool_calls=[{"id": "call_1"}],
        )

    response, results = asyncio.run(run())
    assert response == ""
    assert results == ["Tool send_rich_message: Sent rich embed message."]


def test_plain_reply_without_tool_calls_is_preserved(monkeypatch):
    module = sys.modules[__name__]
    original_turn_sent = _turn_sent_message
    monkeypatch.setattr(module, "_turn_sent_message", original_turn_sent)

    bot = _fake_bot()
    install_visible_output_guard(bot)

    async def run():
        return await bot._dispatch_tool_calls(
            object(),
            "This is the actual answer.",
            native_tool_calls=None,
        )

    response, _results = asyncio.run(run())
    assert response == "This is the actual answer."


def test_existing_followup_guard_sees_file_and_rich_delivery(monkeypatch):
    module = sys.modules[__name__]
    original_turn_sent = _turn_sent_message
    monkeypatch.setattr(module, "_turn_sent_message", original_turn_sent)

    bot = _fake_bot()
    install_visible_output_guard(bot)

    assert _turn_sent_message(["Tool send_file: __FILE_SENT__ Sent file: song.mp3"])
    assert _turn_sent_message(["Tool send_rich_message: Sent rich embed message."])
    assert not _turn_sent_message(["Tool web_search: 3 results"])
