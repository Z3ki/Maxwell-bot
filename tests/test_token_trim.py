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
    bot._LIVE_CORE_TOOLS = MaxwellBot._LIVE_CORE_TOOLS
    bot._compatible_tool_names = MaxwellBot._compatible_tool_names.__get__(bot)
    bot._live_tool_names = MaxwellBot._live_tool_names.__get__(bot)
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


def test_live_tool_names_core_only_for_wyd():
    bot = _live_bot()
    names = MaxwellBot._live_tool_names(bot, _msg("wyd"), "wyd", "discord")
    assert names == set(MaxwellBot._LIVE_CORE_TOOLS) & set(bot.tools)
    assert "youtube" not in names
    assert "web_search" not in names


def test_live_tool_names_adds_youtube_on_url():
    bot = _live_bot()
    content = "check https://youtu.be/dQw4w9WgXcQ"
    names = MaxwellBot._live_tool_names(bot, _msg(content), content, "discord")
    assert "youtube" in names
    assert "send_message" in names


def test_live_tool_names_hard_ping_long_line_gets_helpers():
    bot = _live_bot()
    me = bot.user
    content = "hey can you look around and tell me what this whole thing is about " * 2
    names = MaxwellBot._live_tool_names(
        bot, _msg(content, mentions=[me]), content, "discord"
    )
    assert "web_search" in names
    assert "youtube" in names


def test_tool_prompt_trims_to_live_core_on_short_line():
    bot = _live_bot()
    msg = _msg("wyd")
    prompt = MaxwellBot._tool_system_prompt(bot, "discord", message=msg, content="wyd")
    assert "send_message" in prompt
    assert "youtube" not in prompt
    full = MaxwellBot._tool_system_prompt(bot, "discord")
    assert "youtube" in full


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
