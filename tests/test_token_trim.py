"""Same-function token cuts: live tool packs, short turns, emoji grid, embeds.

The full tool catalog ships on every turn. lean/gated catalogs hid tools
(like hd_image) behind more_tools and made photo requests look like a
different generator.
"""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot import MaxwellBot, ToolCircuitBreaker
from rag_memory import RAGMemoryManager


class FakeTool:
    def get_description(self):
        return "fake tool"


def _live_bot(extra_tools=None):
    tools = {
        name: FakeTool()
        for name in (
            "send_message",
            "no_response",
            "react",
            "send_file",
            "send_media",
            "wait",
            "typing",
            "youtube",
            "web_search",
            "fetch_url",
            "create_site",
            "list_sites",
            "shell",
            "email_send",
            "inbox_list",
            "inbox_act",
            "send_meme",
            "search_messages",
            "lookup_user",
            "tts",
            "image_generator",
            "hd_image",
            "join_vc",
            "more_tools",
        )
    }
    if extra_tools:
        tools.update(extra_tools)
    bot = SimpleNamespace(
        tools=tools,
        user=SimpleNamespace(id=1),
        _control={
            "tools_enabled": True,
            "disabled_tools": [],
            "native_tool_calls": True,
            "emoji_context_enabled": True,
        },
        _conversation_watch={},
        _emoji_grid_shown={},
        _tool_breaker=ToolCircuitBreaker(failure_threshold=999, recovery_seconds=0),
    )
    bot._compatible_tool_names = MaxwellBot._compatible_tool_names.__get__(bot)
    bot._native_tools_enabled = MaxwellBot._native_tools_enabled.__get__(bot)
    bot._directly_addressed = MaxwellBot._directly_addressed.__get__(bot)
    bot._conversation_watch_active = MaxwellBot._conversation_watch_active.__get__(bot)
    bot._is_short_live_turn = MaxwellBot._is_short_live_turn.__get__(bot)
    bot._lean_chat_turn = MaxwellBot._lean_chat_turn.__get__(bot)
    bot._turn_tool_names = MaxwellBot._turn_tool_names.__get__(bot)
    bot._tools_for_turn = MaxwellBot._tools_for_turn.__get__(bot)
    bot._tool_system_prompt = MaxwellBot._tool_system_prompt.__get__(bot)
    bot._build_openai_tools = MaxwellBot._build_openai_tools.__get__(bot)
    return bot


def _msg(content, *, mentions=None, watch_followup=False):
    msg = SimpleNamespace(
        content=content,
        channel=SimpleNamespace(id=99),
        mentions=list(mentions or []),
        guild=None,
        reference=None,
    )
    if watch_followup:
        msg._watch_followup = True
    return msg


def _tool_names(bot, message, content, platform="discord"):
    payload = MaxwellBot._build_openai_tools(
        bot, platform, message=message, content=content
    )
    return {item["function"]["name"] for item in payload}


def test_every_turn_offers_every_registered_tool():
    bot = _live_bot()
    for content in (
        "wyd",
        "Can you run a debugger on YOUR machine?",
        "look",
        "can you tts that",
        "whatts up",
        "so anyway " * 40,
    ):
        names = _tool_names(bot, _msg(content, mentions=[bot.user]), content)
        assert names == set(bot.tools) - {"more_tools"}, content
        assert "more_tools" not in names
        assert "shell" in names
        assert "hd_image" in names
        assert "image_generator" in names


def test_lean_chat_turn_is_gone():
    bot = _live_bot()
    assert MaxwellBot._lean_chat_turn(bot, _msg("wyd"), "wyd") is False
    bot._control["lean_chat_tools"] = True
    assert MaxwellBot._lean_chat_turn(bot, _msg("wyd"), "wyd") is False


def test_tool_prompt_lists_full_catalog_on_chat_turn():
    bot = _live_bot()
    chat = MaxwellBot._tool_system_prompt(
        bot, "discord", message=_msg("wyd"), content="wyd"
    )
    full = MaxwellBot._tool_system_prompt(bot, "discord")
    assert "youtube" in chat
    assert "shell" in chat
    assert "hd_image" in chat
    assert chat == full


def test_disabled_tools_still_hidden():
    bot = _live_bot()
    bot._control["disabled_tools"] = ["shell", "youtube"]
    content = "run a shell command"
    names = _tool_names(bot, _msg(content), content)
    assert "shell" not in names
    assert "youtube" not in names
    assert "send_message" in names
    prompt = MaxwellBot._tool_system_prompt(
        bot, "discord", message=_msg(content), content=content
    )
    catalog = prompt.split("## Tool contract")[0]
    assert "shell" not in catalog
    assert "youtube" not in catalog


def test_short_live_turn_for_watch_followup_not_hard_ping():
    bot = _live_bot()
    watch = _msg("dont be like him max", watch_followup=True)
    ping = _msg("wyd", mentions=[bot.user])
    assert MaxwellBot._is_short_live_turn(bot, watch, "dont be like him max") is True
    assert MaxwellBot._is_short_live_turn(bot, ping, "wyd") is False


def test_emoji_grid_skipped_unless_asked():
    bot = _live_bot()
    bot._emoji_grid_media = AsyncMock(return_value={"b64": "abcd" * 20})

    async def run():
        quiet = SimpleNamespace(guild=object(), content="wyd")
        assert await MaxwellBot._maybe_emoji_grid(bot, quiet, "1") is None
        assert bot._emoji_grid_media.await_count == 0
        asked = SimpleNamespace(guild=object(), content="what emoji can you use")
        item = await MaxwellBot._maybe_emoji_grid(bot, asked, "1")
        assert item is not None
        assert bot._emoji_grid_media.await_count == 1

    asyncio.run(run())


def test_spawn_skips_when_embed_endpoint_is_paused(tmp_path):
    mgr = RAGMemoryManager(str(tmp_path))
    mgr._embed_endpoint_down_until = time.monotonic() + 60
    ran = []

    async def work():
        ran.append(1)

    assert mgr._spawn(work()) is None
    assert ran == []
