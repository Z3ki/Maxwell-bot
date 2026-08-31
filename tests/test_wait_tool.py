"""Tests for WaitTool and the multi-terminal dispatch ordering.

These tests cover the 2026-08-08 change that lets Maxwell issue multiple
send_messages per turn, with optional `wait` calls between them, in
declared order. Previously the dispatch loop dropped any send_message
after the first as a 'duplicate terminal tool call'.
"""
import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot_tools import WaitTool  # noqa: E402

# Module-level stash used by _build_test_bot to record the original
# MaxwellBot._execute_tool_by_name so the autouse fixture below can
# restore it. Without this, tests that run AFTER our patch see the
# fake and break (test order pollution).
_SAVED: dict = {}


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _restore_execute_tool_by_name(request):
    """Autouse fixture that runs after each test in this file and
    restores MaxwellBot._execute_tool_by_name if _build_test_bot
    patched it. Without this, the patch leaks into tests that run
    AFTER the wait_tool tests."""
    yield
    if _SAVED.get("patched") and "real" in _SAVED:
        from bot import MaxwellBot
        MaxwellBot._execute_tool_by_name = _SAVED["real"]
        _SAVED["patched"] = False
        _SAVED.pop("real", None)


def _msg():
    return SimpleNamespace(id="t")


# ---------------------------------------------------------------------------
# WaitTool unit tests
# ---------------------------------------------------------------------------


def test_wait_tool_default_2_seconds():
    """Default 2.0s wait, capped, returns a short status string."""
    tool = WaitTool(bot=None)

    async def run():
        t0 = time.monotonic()
        result = await tool.execute(_msg())
        elapsed = time.monotonic() - t0
        return result, elapsed

    result, elapsed = asyncio.run(run())
    assert result == "Waited 2.0s"
    # Allow some slack but ensure it actually slept ~2s.
    assert 1.8 <= elapsed <= 3.0


def test_wait_tool_respects_custom_seconds():
    tool = WaitTool(bot=None)

    async def run():
        t0 = time.monotonic()
        result = await tool.execute(_msg(), seconds=0.5)
        elapsed = time.monotonic() - t0
        return result, elapsed

    result, elapsed = asyncio.run(run())
    assert result == "Waited 0.5s"
    assert 0.4 <= elapsed <= 1.5


def test_wait_tool_zero_is_instant():
    """seconds=0 should return immediately without sleeping."""
    tool = WaitTool(bot=None)

    async def run():
        t0 = time.monotonic()
        result = await tool.execute(_msg(), seconds=0)
        elapsed = time.monotonic() - t0
        return result, elapsed

    result, elapsed = asyncio.run(run())
    assert result == "Waited 0.0s"
    assert elapsed < 0.1


def test_wait_tool_caps_at_10_seconds():
    """11s request must be REJECTED (not silently capped). The model needs
    to see the error so it can use `sleep` instead of waiting forever."""
    tool = WaitTool(bot=None)

    async def run():
        return await tool.execute(_msg(), seconds=11)

    result = asyncio.run(run())
    assert result.startswith("Error:")
    assert "10 seconds" in result
    assert "sleep" in result.lower()


def test_wait_tool_negative_clamped_to_zero():
    """Negative values are nonsense — clamp to 0, don't crash."""
    tool = WaitTool(bot=None)

    async def run():
        return await tool.execute(_msg(), seconds=-5)

    result = asyncio.run(run())
    assert result == "Waited 0.0s"


def test_wait_tool_garbage_string_defaults_to_2():
    """Bad type -> fall back to default, don't crash the tool batch."""
    tool = WaitTool(bot=None)

    async def run():
        return await tool.execute(_msg(), seconds="not a number")

    result = asyncio.run(run())
    # Default path — should sleep ~2s.
    assert result == "Waited 2.0s"


def test_wait_tool_description_mentions_cap_and_sleep():
    """The description is the only thing the model reads to learn what
    the tool does. Cap + sleep distinction MUST be in there."""
    desc = WaitTool(bot=None).get_description()
    assert "10" in desc
    assert "wait" in desc.lower()
    assert "sleep" in desc.lower()


# ---------------------------------------------------------------------------
# Multi-terminal dispatch integration tests
# ---------------------------------------------------------------------------


def _build_test_bot(tools_dict):
    """Build a SimpleNamespace fake bot with the attributes
    _process_native_tool_calls / _execute_tool_by_name touch. We also
    monkey-patch MaxwellBot._execute_tool_by_name to invoke the fake
    tool's execute directly — we don't want the real send_message's
    emoji-rendering / Discord-posting code path in a unit test of
    dispatch ordering.

    The patch is restored at the end of the test via a pytest fixture
    pattern — see `_restore_execute_tool_by_name` autouse fixture
    below. Without the restore, tests that run AFTER our patch see the
    fake and break (test order pollution).
    """
    from bot import MaxwellBot

    bot = SimpleNamespace()
    bot.tools = tools_dict

    async def fake_execute_tool_by_name(self_obj, message, name, params, **kw):
        tool = self_obj.tools.get(name)
        if tool is None:
            return f"Error: tool {name!r} not registered"
        try:
            res = await tool.execute(message, **params)
            return res if isinstance(res, str) else str(res)
        except Exception as e:
            return f"Error: {e}"

    # Save the real method BEFORE patching. The fixture restores it.
    _SAVED["real"] = MaxwellBot._execute_tool_by_name
    MaxwellBot._execute_tool_by_name = fake_execute_tool_by_name
    _SAVED["patched"] = True

    bot._control = {
        "tools_enabled": True,
        "disabled_tools": [],
        "typing_indicator": False,
    }
    bot._compatible_tool_names = lambda platform: set(bot.tools)
    bot._message_tool_platform = lambda m: "discord"
    bot._last_native_followup_messages = []
    bot._native_tools_enabled = lambda: True
    bot._build_openai_tools = lambda platform="discord", **kwargs: []
    bot._progress_enabled = lambda *a, **k: False
    bot._current_progress_by_channel = {}
    bot._remember_tool_call = lambda *a, **k: asyncio.sleep(0)
    bot._tainted_messages = set()
    bot._trace_lock = asyncio.Lock()
    bot._record_llm_trace = lambda *a, **k: asyncio.sleep(0)
    bot._native_calls_from = lambda r: []
    bot._consume_native_tool_calls = list
    bot._usage_from = lambda r: {}
    bot._signal_streaming = lambda *a, **k: None
    bot._tool_results_need_followup = lambda results: False
    bot._tool_system_prompt = lambda platform="discord", **kwargs: ""
    bot.config = SimpleNamespace(DATA_DIR="/tmp", log_level="info")
    bot._extract_reasoning = lambda params: (
        str(params.get("reasoning", "") or ""),
        {k: v for k, v in params.items() if k != "reasoning"},
    )
    bot._tool_breaker = SimpleNamespace(
        is_open=lambda name: False,
        record_failure=lambda name: None,
        record_success=lambda name: None,
    )
    bot._is_admin = lambda *a, **k: True
    bot._discord_long_op_threshold = 5.0
    bot._progress_permitted = lambda *a, **k: False
    return bot


def test_send_message_then_wait_then_send_message_runs_in_order():
    """Three terminal calls (send → wait → send) must execute in the
    order the model emitted them. Previously the second send was dropped
    as a duplicate terminal tool call."""
    from bot import MaxwellBot

    events = []

    async def fake_send(message, **kwargs):
        content = kwargs.get("content") or kwargs.get("text") or ""
        events.append(("send", content))
        return "__MESSAGE_SENT__"

    async def fake_wait(message, **kwargs):
        seconds = kwargs.get("seconds", 2.0)
        events.append(("wait_start", seconds))
        await asyncio.sleep(0.01)
        events.append(("wait_end", seconds))
        return f"Waited {seconds}s"

    bot = _build_test_bot(
        {
            "send_message": SimpleNamespace(
                execute=fake_send, is_destructive=False, streams_output=False,
                name="send_message",
            ),
            "wait": SimpleNamespace(
                execute=fake_wait, is_destructive=False, streams_output=False,
                name="wait",
            ),
            "no_response": SimpleNamespace(
                execute=lambda *a, **k: asyncio.sleep(0, result="__NO_RESPONSE__"),
                is_destructive=False, streams_output=False, name="no_response",
            ),
        }
    )

    raw_tool_calls = [
        {
            "id": "call_a",
            "type": "function",
            "function": {
                "name": "send_message",
                "arguments": '{"reasoning": "first", "content": "first msg"}',
            },
        },
        {
            "id": "call_b",
            "type": "function",
            "function": {
                "name": "wait",
                "arguments": '{"reasoning": "pause", "seconds": 1.0}',
            },
        },
        {
            "id": "call_c",
            "type": "function",
            "function": {
                "name": "send_message",
                "arguments": '{"reasoning": "second", "content": "second msg"}',
            },
        },
    ]

    message = SimpleNamespace(
        id="m1",
        guild=SimpleNamespace(id="g1"),
        channel=SimpleNamespace(id="c1"),
        suppress_typing=True,
    )

    async def run():
        await MaxwellBot._process_native_tool_calls(
            bot, message, "", raw_tool_calls
        )

    asyncio.run(run())

    assert events == [
        ("send", "first msg"),
        ("wait_start", 1.0),
        ("wait_end", 1.0),
        ("send", "second msg"),
    ], f"Out-of-order or missing events: {events}"


def test_no_response_blocks_later_send_message():
    """no_response must be exclusive — a later send_message in the same
    batch gets rejected with an error the model can see."""
    from bot import MaxwellBot

    sent = []

    async def fake_send(message, **kwargs):
        sent.append(kwargs.get("content") or kwargs.get("text"))
        return "__MESSAGE_SENT__"

    async def fake_no_response(message, **kwargs):
        sent.append("NO_RESPONSE")
        return "__NO_RESPONSE__"

    bot = _build_test_bot(
        {
            "send_message": SimpleNamespace(
                execute=fake_send, is_destructive=False, streams_output=False,
                name="send_message",
            ),
            "no_response": SimpleNamespace(
                execute=fake_no_response, is_destructive=False,
                streams_output=False, name="no_response",
            ),
            "wait": SimpleNamespace(
                execute=lambda *a, **k: asyncio.sleep(0, result="Waited 0s"),
                is_destructive=False, streams_output=False, name="wait",
            ),
        }
    )

    raw_tool_calls = [
        {
            "id": "call_nr",
            "type": "function",
            "function": {
                "name": "no_response",
                "arguments": '{"reasoning": "stay silent"}',
            },
        },
        {
            "id": "call_s",
            "type": "function",
            "function": {
                "name": "send_message",
                "arguments": '{"reasoning": "send", "content": "hi"}',
            },
        },
    ]

    message = SimpleNamespace(
        id="m2",
        guild=SimpleNamespace(id="g2"),
        channel=SimpleNamespace(id="c2"),
        suppress_typing=True,
    )

    async def run():
        await MaxwellBot._process_native_tool_calls(
            bot, message, "", raw_tool_calls
        )

    asyncio.run(run())

    # Only the no_response actually fired; the send_message was rejected
    assert sent == ["NO_RESPONSE"]


def test_two_send_messages_in_a_row_both_fire_in_order():
    """Two send_messages back-to-back (no wait) — both must fire, in order."""
    from bot import MaxwellBot

    sent = []

    async def fake_send(message, **kwargs):
        sent.append(kwargs.get("content") or kwargs.get("text"))
        return "__MESSAGE_SENT__"

    bot = _build_test_bot(
        {
            "send_message": SimpleNamespace(
                execute=fake_send, is_destructive=False, streams_output=False,
                name="send_message",
            ),
            "wait": SimpleNamespace(
                execute=lambda *a, **k: asyncio.sleep(0, result="Waited 0s"),
                is_destructive=False, streams_output=False, name="wait",
            ),
            "no_response": SimpleNamespace(
                execute=lambda *a, **k: asyncio.sleep(0, result="__NO_RESPONSE__"),
                is_destructive=False, streams_output=False, name="no_response",
            ),
        }
    )

    raw_tool_calls = [
        {
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "send_message",
                "arguments": '{"reasoning": "first", "content": "alpha"}',
            },
        },
        {
            "id": "call_2",
            "type": "function",
            "function": {
                "name": "send_message",
                "arguments": '{"reasoning": "second", "content": "beta"}',
            },
        },
    ]

    message = SimpleNamespace(
        id="m3",
        guild=SimpleNamespace(id="g3"),
        channel=SimpleNamespace(id="c3"),
        suppress_typing=True,
    )

    async def run():
        await MaxwellBot._process_native_tool_calls(
            bot, message, "", raw_tool_calls
        )

    asyncio.run(run())

    assert sent == ["alpha", "beta"], f"Out of order: {sent}"
