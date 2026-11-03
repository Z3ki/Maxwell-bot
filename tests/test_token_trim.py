"""Tool catalog, short-turn and prompt behavior."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot import MaxwellBot, ToolCircuitBreaker


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
            "web_search",
            "fetch_url",
            "create_site",
            "list_sites",
            "shell",
            "inbox_list",
            "inbox_act",
            "send_meme",
            "search_messages",
            "lookup_user",
            "image_generator",
            "hd_image",
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


def _msg(content, *, mentions=None, watch_followup=False, dm=False):
    guild = None
    author = SimpleNamespace(id=2)
    if not dm:
        perms = SimpleNamespace(administrator=True)
        me = SimpleNamespace(id=1, guild_permissions=perms, roles=[])
        author = SimpleNamespace(id=2, guild_permissions=perms, roles=[])
        guild = SimpleNamespace(id=1, me=me)
        author.guild = guild
        guild.get_member = lambda user_id: author if int(user_id) == 2 else None
        msg_channel = SimpleNamespace(id=99, guild=guild)
        msg_channel.permissions_for = lambda member: (
            perms if getattr(member, "id", None) in {1, 2} else None
        )
    else:
        msg_channel = SimpleNamespace(id=99, guild=None)
    msg = SimpleNamespace(
        content=content,
        channel=msg_channel,
        mentions=list(mentions or []),
        guild=guild,
        author=author,
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


@pytest.mark.parametrize("operator", [False, True])
def test_native_turn_offers_small_core_and_authorized_catalog(operator):
    bot = _live_bot()
    bot._is_admin = lambda _uid: operator
    for content in ("wyd", "Can you run a debugger?", "so anyway " * 40):
        message = _msg(content, mentions=[bot.user])
        payload = MaxwellBot._build_openai_tools(bot, message=message, content=content)
        names = {item["function"]["name"] for item in payload}
        assert {"send_message", "web_search", "load_tools"} <= names
        assert not {"shell", "hd_image", "more_tools"} & names
        catalog = next(item["function"]["description"] for item in payload
                       if item["function"]["name"] == "load_tools")
        assert "shell:" in catalog and "hd_image:" in catalog
        assert ("inbox_list:" in catalog) is operator


def test_lean_chat_turn_is_gone():
    bot = _live_bot()
    assert MaxwellBot._lean_chat_turn(bot, _msg("wyd"), "wyd") is False
    bot._control["lean_chat_tools"] = True
    assert MaxwellBot._lean_chat_turn(bot, _msg("wyd"), "wyd") is False


def test_native_tool_prompt_points_to_catalog():
    bot = _live_bot()
    chat = MaxwellBot._tool_system_prompt(
        bot, "discord", message=_msg("wyd"), content="wyd"
    )
    assert "load_tools" in chat


@pytest.mark.parametrize("native", [False, True])
def test_tool_prompt_and_native_payload_order_survive_registry_reload(native):
    import json

    bot = _live_bot()
    bot._control.update(native_tool_calls=native, disabled_tools=["shell"])
    message = _msg("hi")
    first = MaxwellBot._tool_system_prompt(bot, message=message, content="hi")
    first_tools = MaxwellBot._build_openai_tools(bot, message=message, content="hi")
    bot.tools = dict(reversed(list(bot.tools.items())))
    second = MaxwellBot._tool_system_prompt(bot, message=message, content="hi")
    second_tools = MaxwellBot._build_openai_tools(bot, message=message, content="hi")
    assert first == second
    assert json.dumps(first_tools) == json.dumps(second_tools)
    assert "shell" not in first.split("## Tool contract")[0]


def test_disabled_tools_still_hidden():
    bot = _live_bot()
    bot._control["disabled_tools"] = ["shell", "inbox_list"]
    content = "run a shell command"
    names = _tool_names(bot, _msg(content), content)
    assert "shell" not in names
    assert "inbox_list" not in names
    assert "send_message" in names
    prompt = MaxwellBot._tool_system_prompt(
        bot, "discord", message=_msg(content), content=content
    )
    catalog = prompt.split("## Tool contract")[0]
    assert "shell" not in catalog
    assert "inbox_list" not in catalog


def test_catalog_respects_dm_and_guild_permissions():
    bot = _live_bot(extra_tools={
        "kick_member": FakeTool(), "ban_member": FakeTool(),
        "create_channel": FakeTool(), "site_server": FakeTool(),
    })
    def catalog(message):
        payload = MaxwellBot._build_openai_tools(bot, message=message)
        return next(item["function"]["description"] for item in payload
                    if item["function"]["name"] == "load_tools")
    dm = catalog(_msg("hi", dm=True))
    assert "kick_member:" not in dm and "ban_member:" not in dm
    assert "shell:" in dm and "create_site:" in dm
    guild = catalog(_msg("hi"))
    assert "kick_member:" in guild and "shell:" in guild


def test_load_tools_is_turn_local_and_rechecks_authorization(monkeypatch):
    import json
    from bot_parts.tool_calls import _loaded_native_tools

    bot = _live_bot()
    bot._control["disabled_tools"] = ["inbox_list"]
    message = _msg("run a command", dm=True)
    message.id = 7
    monkeypatch.setattr(MaxwellBot, "_request_state", AsyncMock(return_value={}))

    async def turn():
        token = _loaded_native_tools.set(set())
        try:
            call = {"id": "load", "type": "function", "function": {
                "name": "load_tools",
                "arguments": json.dumps({"names": ["shell", "inbox_list"]}),
            }}
            _, results = await MaxwellBot._process_native_tool_calls(bot, message, "", [call])
            assert "Loaded shell" in results[0]
            assert "unavailable: inbox_list" in results[0]
            names = _tool_names(bot, message, message.content)
            assert "shell" in names and "inbox_list" not in names
            bot._control["disabled_tools"].append("shell")
            assert "shell" not in _tool_names(bot, message, message.content)
        finally:
            _loaded_native_tools.reset(token)

    asyncio.run(turn())
    bot._control["disabled_tools"].remove("shell")
    assert "shell" not in _tool_names(bot, message, message.content)


def test_unloaded_direct_call_is_allowed_but_disabled_call_is_denied(monkeypatch):
    bot = _live_bot()
    bot.config = SimpleNamespace(DISABLE_TAINT_GATE=False)
    bot.is_message_tainted = lambda message: False
    invoke = AsyncMock(return_value="ok")
    monkeypatch.setattr(MaxwellBot, "_invoke_request_tool", invoke)
    message = _msg("run a command", dm=True)
    message.id = 7

    async def run():
        allowed = await MaxwellBot._execute_tool_by_name(
            bot, message, "shell", {}, disabled=set(), compatible=set(bot.tools)
        )
        assert allowed == "Tool shell: ok"
        bot._control["disabled_tools"] = ["shell"]
        denied = await MaxwellBot._execute_tool_by_name(
            bot, message, "shell", {}, disabled={"shell"}, compatible=set(bot.tools)
        )
        assert "disabled" in denied
        assert invoke.await_count == 1

    asyncio.run(run())


def test_loaded_schema_prefix_is_stable_and_workers_keep_full_catalog():
    from bot_parts.tool_calls import _loaded_native_tools
    from plugins.maxwell_extras.task_workers import _inside_worker

    bot = _live_bot()
    message = _msg("hi")
    token = _loaded_native_tools.set(set())
    try:
        before = MaxwellBot._build_openai_tools(bot, message=message)
        _loaded_native_tools.get().add("shell")
        after = MaxwellBot._build_openai_tools(bot, message=message)
        assert after[:len(before)] == before
        assert after[-1]["function"]["name"] == "shell"
        worker_token = _inside_worker.set(True)
        try:
            worker = MaxwellBot._build_openai_tools(bot, message=message)
            names = {item["function"]["name"] for item in worker}
            assert "shell" in names and "load_tools" not in names
        finally:
            _inside_worker.reset(worker_token)
    finally:
        _loaded_native_tools.reset(token)


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



def test_lazy_tool_loading_can_be_disabled():
    bot = _live_bot()
    bot.config = SimpleNamespace(LAZY_TOOL_LOADING=False)
    message = _msg("wyd", mentions=[bot.user])
    names = _tool_names(bot, message, "wyd")
    assert "load_tools" not in names
    assert {"send_message", "web_search", "shell"} <= names
    chat = MaxwellBot._tool_system_prompt(bot, "discord", message=message, content="wyd")
    assert "load_tools" not in chat
