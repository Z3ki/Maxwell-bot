"""Regressions from the 2026-08-19 multi-agent code review."""

import asyncio
from types import SimpleNamespace

from bot import strip_tool_payload_leaks
from rem import RemStore
from tool_schemas import TOOL_PARAMETERS, build_openai_tools


def test_native_calls_from_string_does_not_consume_stash():
    from bot import MaxwellBot

    consumed = {"n": 0}

    def consume():
        consumed["n"] += 1
        return [{"id": "stale"}]

    bot = SimpleNamespace(_consume_native_tool_calls=consume)
    assert MaxwellBot._native_calls_from(bot, "quota exceeded") == []
    assert consumed["n"] == 0


def test_wait_schema_is_declared_and_prompt_edit_schema_is_retired():
    assert "seconds" in TOOL_PARAMETERS["wait"]["properties"]
    assert "update_base_personality" not in TOOL_PARAMETERS
    assert "update_server_prompt" not in TOOL_PARAMETERS
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
