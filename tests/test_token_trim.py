"""Same-function token cuts: live tool packs, short turns, emoji grid, embeds.

The tool pack is per-turn: plain conversation carries CHAT_CORE_TOOL_NAMES,
anything that asks for an action carries the whole catalog, and more_tools is
the way back up when a chat turn turns out to need something.
"""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot import MaxwellBot, ToolCircuitBreaker
from tool_schemas import CHAT_CORE_TOOL_NAMES
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


def test_action_turn_offers_every_registered_tool():
    bot = _live_bot()
    content = "Can you run a debugger on YOUR machine?"
    names = _tool_names(bot, _msg(content, mentions=[bot.user]), content)
    assert names == set(bot.tools)
    assert "shell" in names
    assert "youtube" in names
    assert "web_search" in names


def test_plain_chat_turn_carries_only_the_conversational_set():
    bot = _live_bot()
    names = _tool_names(bot, _msg("wyd"), "wyd")
    assert names <= CHAT_CORE_TOOL_NAMES
    # It can still talk, react, and look something up.
    assert {"send_message", "no_response", "web_search", "more_tools"} <= names
    # The operator surface is not along for the ride.
    assert "shell" not in names
    assert "email_send" not in names
    assert "join_vc" not in names
    assert "search_messages" not in names


def test_asking_for_something_leaves_lean_mode():
    bot = _live_bot()
    for ask in (
        "make me a website about frogs",
        "ban that guy",
        "run this script for me",
        "send me the file",
        "can you change your avatar",
    ):
        names = _tool_names(bot, _msg(ask), ask)
        assert names == set(bot.tools), ask


def test_more_tools_reopens_the_full_catalog():
    bot = _live_bot()
    msg = _msg("wyd")
    assert _tool_names(bot, msg, "wyd") <= CHAT_CORE_TOOL_NAMES
    msg._tools_expanded = True  # what MoreToolsTool sets
    assert _tool_names(bot, msg, "wyd") == set(bot.tools)


def test_lean_chat_tools_can_be_turned_off():
    bot = _live_bot()
    bot._control["lean_chat_tools"] = False
    assert _tool_names(bot, _msg("wyd"), "wyd") == set(bot.tools)


def test_tool_prompt_lists_full_catalog_on_action_turn():
    bot = _live_bot()
    content = "run the deploy script"
    prompt = MaxwellBot._tool_system_prompt(
        bot, "discord", message=_msg(content), content=content
    )
    assert "send_message" in prompt
    assert "youtube" in prompt
    assert "shell" in prompt
    full = MaxwellBot._tool_system_prompt(bot, "discord")
    assert "youtube" in full
    assert "shell" in full


def test_tool_prompt_on_chat_turn_stays_short():
    bot = _live_bot()
    chat = MaxwellBot._tool_system_prompt(bot, "discord", message=_msg("wyd"), content="wyd")
    full = MaxwellBot._tool_system_prompt(bot, "discord")
    assert "more_tools" in chat
    assert "shell" not in chat.split("## Tool contract")[0]
    assert len(chat) < len(full)


def test_disabled_tools_still_hidden():
    bot = _live_bot()
    bot._control["disabled_tools"] = ["shell", "youtube"]
    content = "run a shell command"  # action turn: full catalog minus disabled
    names = _tool_names(bot, _msg(content), content)
    assert "shell" not in names
    assert "youtube" not in names
    assert "send_message" in names
    prompt = MaxwellBot._tool_system_prompt(
        bot, "discord", message=_msg(content), content=content
    )
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


def test_naming_a_tool_asks_for_it_but_a_substring_does_not():
    bot = _live_bot()
    named = "can you tts that"
    assert _tool_names(bot, _msg(named), named) == set(bot.tools)
    # "whatts" contains "tts"; word boundaries keep the turn lean.
    chat = "whatts up"
    assert _tool_names(bot, _msg(chat), chat) <= CHAT_CORE_TOOL_NAMES


def test_a_long_message_is_treated_as_a_request():
    bot = _live_bot()
    rant = "so anyway " * 40  # >300 chars, no action verb
    assert len(rant) > 300
    assert _tool_names(bot, _msg(rant), rant) == set(bot.tools)


def test_a_non_media_attachment_leaves_lean_mode():
    bot = _live_bot()
    msg = _msg("look")
    assert _tool_names(bot, msg, "look") <= CHAT_CORE_TOOL_NAMES
    msg.attachments = [SimpleNamespace(content_type="application/pdf")]
    assert _tool_names(bot, msg, "look") == set(bot.tools)
    msg.attachments = [SimpleNamespace(content_type="image/png")]
    assert _tool_names(bot, msg, "look") <= CHAT_CORE_TOOL_NAMES
