"""Regressions from the 2026-08-19 multi-agent code review."""

import asyncio
import json
from types import SimpleNamespace

from bot import TelegramMessageAdapter, strip_tool_payload_leaks
from captcha_solver import _BaseSolver, CaptchaSolveError
from rem import RemStore
from tool_schemas import TOOL_PARAMETERS, build_openai_tools


class _FakeSession:
    def post(self, *args, **kwargs):
        raise AssertionError("should not send")


def test_telegram_adapter_does_not_reuse_chat_id_as_message_id():
    adapter = TelegramMessageAdapter(
        _FakeSession(), "https://api.telegram.org/botx", 999888777, None
    )
    assert adapter.id is None
    assert adapter.chat_id == 999888777


def test_native_calls_from_string_does_not_consume_stash():
    from bot import MaxwellBot

    consumed = {"n": 0}

    def consume():
        consumed["n"] += 1
        return [{"id": "stale"}]

    bot = SimpleNamespace(_consume_native_tool_calls=consume)
    assert MaxwellBot._native_calls_from(bot, "quota exceeded") == []
    assert consumed["n"] == 0


def test_wait_and_personality_schemas_are_declared():
    assert "seconds" in TOOL_PARAMETERS["wait"]["properties"]
    assert "text" in TOOL_PARAMETERS["update_base_personality"]["properties"]
    assert "files" in TOOL_PARAMETERS["shell"]["properties"]
    tools = build_openai_tools(
        {
            "wait": SimpleNamespace(get_description=lambda: "wait"),
            "sleep": SimpleNamespace(get_description=lambda: "sleep"),
        }
    )
    names = {t["function"]["name"] for t in tools}
    assert "wait" in names
    wait = next(t for t in tools if t["function"]["name"] == "wait")
    assert "seconds" in wait["function"]["parameters"]["properties"]


def test_2captcha_ready_status_is_accepted():
    async def run():
        solver = _BaseSolver("key")

        async def get_result():
            return {"status": 1, "request": "token-abc"}

        data = await solver._poll(get_result, timeout=1)
        assert data["request"] == "token-abc"

    asyncio.run(run())


def test_2captcha_not_ready_then_error():
    async def run():
        solver = _BaseSolver("key")
        n = {"i": 0}

        async def get_result():
            n["i"] += 1
            if n["i"] == 1:
                return {"status": 0, "request": "CAPCHA_NOT_READY"}
            return {"status": 0, "request": "ERROR_CAPTCHA_UNSOLVABLE"}

        try:
            await solver._poll(get_result, timeout=5)
            raise AssertionError("expected failure")
        except CaptchaSolveError:
            pass

    asyncio.run(run())


def test_rem_patch_state_does_not_wipe_corrupt_file(tmp_path):
    store = RemStore(str(tmp_path))
    store.state_file.write_text("{ broken", encoding="utf-8")

    async def run():
        out = await store.patch_state({"running": False})
        assert out == {}
        assert store.state_file.read_text(encoding="utf-8") == "{ broken"

    asyncio.run(run())


def test_strip_keeps_plain_chat_without_tool_tags():
    assert strip_tool_payload_leaks("hello there") == "hello there"
