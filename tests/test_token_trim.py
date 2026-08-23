"""Same-function token cuts: live tool packs, short turns, emoji grid, embeds."""

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


def test_live_turn_offers_every_registered_tool():
    bot = _live_bot()
    content = "Can you run a debugger on YOUR machine?"
    names = _tool_names(bot, _msg(content, mentions=[bot.user]), content)
    assert names == set(bot.tools)
    assert "shell" in names
    assert "youtube" in names
    assert "web_search" in names
    assert "sub_agent" not in names  # not registered on this stub


def test_short_watch_line_still_offers_full_catalog():
    bot = _live_bot()
    names = _tool_names(bot, _msg("wyd"), "wyd")
    assert names == set(bot.tools)
    assert "shell" in names
    assert "youtube" in names


def test_tool_prompt_lists_full_catalog_on_live_turn():
    bot = _live_bot()
    msg = _msg("wyd")
    prompt = MaxwellBot._tool_system_prompt(bot, "discord", message=msg, content="wyd")
    assert "send_message" in prompt
    assert "youtube" in prompt
    assert "shell" in prompt
    full = MaxwellBot._tool_system_prompt(bot, "discord")
    assert "youtube" in full
    assert "shell" in full


def test_disabled_tools_still_hidden():
    bot = _live_bot()
    bot._control["disabled_tools"] = ["shell", "youtube"]
    names = _tool_names(bot, _msg("wyd"), "wyd")
    assert "shell" not in names
    assert "youtube" not in names
    assert "send_message" in names
    prompt = MaxwellBot._tool_system_prompt(bot, "discord", message=_msg("wyd"), content="wyd")
    # Catalog must not list disabled tools. TOOL_PROTOCOL may still mention
    # shell as a send_file delivery method (`files=`) — that is not offering
    # the tool.
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
