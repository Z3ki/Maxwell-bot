"""Expression delivery, scoping, live refresh and terminal reply regressions."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import discord
import pytest

from bot import MaxwellBot, _sanitize_visible_reply, _tool_results_need_followup
from discord_expressions import (
    available_emojis,
    available_stickers,
    cannot_send,
    extract_stickers,
    refresh_global_expressions,
    start_expression_refresh,
    sync_guild,
)
from plugins.discord_messages import setup
from plugins.discord_messages.impl import (
    ReactTool,
    SendGifTool,
    SendMessageTool,
    NoResponseTool,
    ListExpressionsTool,
    _download_gif,
)
from plugins.maxwell_extras.visible_output_guard import tool_result_is_visible
from tool_schemas import build_openai_tools
from tool_progress import ToolProgress


class Emoji:
    def __init__(self, name, eid=123456789012345678, animated=False, available=True):
        self.name, self.id, self.animated, self.available = (
            name,
            eid,
            animated,
            available,
        )

    def __str__(self):
        return f"<{'a' if self.animated else ''}:{self.name}:{self.id}>"


def fixture(**permissions):
    perms = SimpleNamespace(
        **{
            "view_channel": True,
            "read_message_history": True,
            "add_reactions": True,
            "send_messages": True,
            "send_messages_in_threads": True,
            "embed_links": True,
        }
        | permissions
    )
    guild = SimpleNamespace(id=1, me=SimpleNamespace(id=3), emojis=[], stickers=[])
    channel = SimpleNamespace(
        id=2,
        guild=guild,
        permissions_for=lambda _: perms,
        send=AsyncMock(return_value=SimpleNamespace(id=10)),
    )
    message = SimpleNamespace(
        id=4,
        guild=guild,
        channel=channel,
        add_reaction=AsyncMock(),
        reactions=[],
        author=SimpleNamespace(id=5),
    )
    bot = SimpleNamespace(
        _guild_emojis={},
        _guild_stickers={},
        _control={},
        _application_emojis={},
        _standard_stickers={},
        config=SimpleNamespace(GIPHY_API_KEY="test-secret"),
        _record_delivery=AsyncMock(),
        _record_request_outcome=AsyncMock(),
        _directly_addressed=lambda _: True,
        _progress_enabled=lambda _: False,
    )
    return bot, message, perms


def test_reaction_is_a_confirmed_complete_response():
    bot, msg, _ = fixture()
    result = asyncio.run(ReactTool(bot).execute(msg, emoji="👍"))
    assert result.startswith("__REACTION_SENT__")
    msg.add_reaction.assert_awaited_once_with("👍")
    bot._record_request_outcome.assert_awaited_once_with(
        msg, "delivered", reason="reaction_only"
    )
    assert not _tool_results_need_followup(["Tool react: " + result])
    assert tool_result_is_visible("Tool react: " + result)


def test_nonterminal_reaction_still_allows_substantive_work():
    bot, msg, _ = fixture()
    result = asyncio.run(ReactTool(bot).execute(msg, emoji="👍", finish=False))
    assert _tool_results_need_followup(["Tool react: " + result])
    bot._record_request_outcome.assert_not_awaited()


@pytest.mark.parametrize(
    "field", ["view_channel", "read_message_history", "add_reactions"]
)
def test_reaction_permission_denials_do_not_hit_discord(field):
    bot, msg, _ = fixture(**{field: False})
    result = asyncio.run(ReactTool(bot).execute(msg, emoji="👍"))
    assert result.startswith("Error:")
    msg.add_reaction.assert_not_awaited()
    bot._record_request_outcome.assert_not_awaited()


def test_existing_reaction_can_be_joined_without_add_reactions():
    bot, msg, _ = fixture(add_reactions=False)
    msg.reactions = [SimpleNamespace(emoji="👍")]
    assert asyncio.run(ReactTool(bot).execute(msg, emoji="👍")).startswith(
        "__REACTION_SENT__"
    )


def test_application_emoji_can_react_without_external_emoji_permission():
    bot, msg, _ = fixture(use_external_emojis=False)
    em = Emoji("wave", animated=True)
    bot._application_emojis = {"wave": str(em)}
    assert asyncio.run(ReactTool(bot).execute(msg, emoji="wave")).startswith(
        "__REACTION_SENT__"
    )
    msg.add_reaction.assert_awaited_once_with(str(em))


@pytest.mark.parametrize("source", ["guild", "application"])
@pytest.mark.parametrize(
    "value",
    [
        "KingMe", ":kingme:", "123456789012345678",
        "<:KingMe:123456789012345678>",
        "<a:old_name:123456789012345678>",
        "old_name:123456789012345678",
    ],
)
def test_animated_reactions_resolve_live_ids_and_names(source, value):
    bot, msg, _ = fixture(use_external_emojis=False)
    em = Emoji("KingMe", animated=True)
    if source == "guild":
        msg.guild.emojis = [em]
    else:
        bot._application_emojis = {"kingme": str(em)}
    assert asyncio.run(ReactTool(bot).execute(msg, emoji=value)).startswith(
        "__REACTION_SENT__"
    )
    msg.add_reaction.assert_awaited_once_with(str(em))


def test_existing_custom_reaction_is_matched_by_id_without_add_reactions():
    bot, msg, _ = fixture(add_reactions=False)
    em = Emoji("KingMe", animated=True)
    msg.guild.emojis = [em]
    msg.reactions = [
        SimpleNamespace(emoji=discord.PartialEmoji(name="old_name", id=em.id))
    ]
    result = asyncio.run(ReactTool(bot).execute(msg, emoji=":KingMe:"))
    assert result.startswith("__REACTION_SENT__")
    msg.add_reaction.assert_awaited_once_with(str(em))


def test_application_emoji_remains_selectable_when_local_name_collides():
    bot, msg, _ = fixture()
    app = Emoji("KingMe", eid=123456789012345678, animated=True)
    local = Emoji("KingMe", eid=234567890123456789)
    bot._application_emojis = {"kingme": str(app)}
    msg.guild.emojis = [local]
    assert asyncio.run(ReactTool(bot).execute(msg, emoji=str(app))).startswith(
        "__REACTION_SENT__"
    )
    msg.add_reaction.assert_awaited_once_with(str(app))
    assert MaxwellBot._render_custom_emojis(bot, str(app), msg.guild) == str(app)
    assert MaxwellBot._render_custom_emojis(bot, ":KingMe:", msg.guild) == str(local)
    catalog = asyncio.run(ListExpressionsTool(bot).execute(msg, query="kingme"))
    assert str(app) in catalog and str(local) in catalog


@pytest.mark.parametrize("value", ["234567890123456789", "<a:KingMe:234567890123456789>"])
def test_unknown_id_cannot_impersonate_a_local_animated_emoji(value):
    bot, msg, _ = fixture()
    local = Emoji("KingMe", animated=True)
    msg.guild.emojis = [local]
    result = asyncio.run(ReactTool(bot).execute(msg, emoji=value))
    assert result.startswith("Error:")
    msg.add_reaction.assert_not_awaited()


@pytest.mark.parametrize("available", [False, True])
def test_guild_emoji_availability_and_role_restrictions_still_apply(available):
    bot, msg, _ = fixture()
    em = Emoji("KingMe", animated=True, available=available)
    em.is_usable = lambda: False
    msg.guild.emojis = [em]
    result = asyncio.run(ReactTool(bot).execute(msg, emoji=str(em)))
    assert result.startswith("Error:")
    msg.add_reaction.assert_not_awaited()


def test_animated_grid_uses_webp_for_webp_and_avif_upload_compatibility():
    bot, msg, _ = fixture()
    msg.guild.emojis = [Emoji("KingMe", animated=True)]
    bot._GRID_MAX_EMOJIS, bot._GRID_MAX_STICKERS = 48, 12
    assert MaxwellBot._grid_entries(bot, msg.guild) == [
        ("kingme", "https://cdn.discordapp.com/emojis/123456789012345678.webp")
    ]


def test_other_server_emoji_is_rejected_locally():
    bot, msg, _ = fixture()
    bot._guild_emojis["unrelated"] = {"other": str(Emoji("other"))}
    result = asyncio.run(ReactTool(bot).execute(msg, emoji=str(Emoji("other"))))
    assert result.startswith("Error:")
    msg.add_reaction.assert_not_awaited()
    rendered = MaxwellBot._render_custom_emojis(bot, str(Emoji("other")), msg.guild)
    assert "<" not in rendered


def test_reaction_target_stays_in_current_channel():
    bot, msg, _ = fixture()
    target = SimpleNamespace(add_reaction=AsyncMock(), reactions=[])
    msg.channel.fetch_message = AsyncMock(return_value=target)
    asyncio.run(ReactTool(bot).execute(msg, emoji="🔥", message_id="9"))
    msg.channel.fetch_message.assert_awaited_once_with(9)
    target.add_reaction.assert_awaited_once_with("🔥")


@pytest.mark.parametrize("thread", [False, True])
def test_read_only_ping_ends_without_model_or_post(monkeypatch, thread):
    bot, msg, _ = fixture(
        **{"send_messages_in_threads" if thread else "send_messages": False}
    )
    if thread:
        msg.channel.type = discord.ChannelType.public_thread
    bot.tools = {"react": ReactTool(bot)}
    bot._begin_inflight_context = AsyncMock(
        side_effect=AssertionError("started a model turn")
    )
    bot._check_sleep_gate = AsyncMock(
        side_effect=AssertionError("posted a sleep notice")
    )
    msg.content = "hello"

    async def invoke(owner, inbound, name, handler, **args):
        return await handler.execute(inbound, **args)

    monkeypatch.setattr(MaxwellBot, "_invoke_request_tool", invoke)
    asyncio.run(MaxwellBot._handle_message(bot, msg))
    msg.add_reaction.assert_awaited_once_with("🔇")
    msg.channel.send.assert_not_awaited()


def test_read_only_ping_without_reactions_is_suppressed(monkeypatch):
    bot, msg, _ = fixture(send_messages=False, add_reactions=False)
    bot.tools = {"react": ReactTool(bot)}

    async def invoke(owner, inbound, name, handler, **args):
        return await handler.execute(inbound, **args)

    monkeypatch.setattr(MaxwellBot, "_invoke_request_tool", invoke)
    monkeypatch.setattr(
        MaxwellBot, "_record_request_outcome", bot._record_request_outcome
    )
    assert asyncio.run(MaxwellBot._acknowledge_read_only_channel(bot, msg))
    msg.add_reaction.assert_not_awaited()
    assert bot._record_request_outcome.await_args.args[2] == "suppressed"


def test_read_only_direct_request_can_end_silently_and_thread_permissions_work():
    bot, msg, _ = fixture(send_messages=False)
    assert asyncio.run(NoResponseTool(bot).execute(msg)) == "__NO_RESPONSE__"
    msg.channel.type = discord.ChannelType.public_thread
    assert not cannot_send(msg)
    msg.user_install = True
    msg.channel.permissions_for = lambda _: SimpleNamespace(
        view_channel=False, send_messages=False
    )
    assert not cannot_send(msg)


@pytest.mark.parametrize("terminal", ["react", "send_gif"])
def test_social_response_stops_following_calls_and_prose(monkeypatch, terminal):
    bot, msg, _ = fixture()
    bot.tools = {}
    marker = "__REACTION_SENT__" if terminal == "react" else "__GIF_SENT__"
    calls = []

    async def execute(owner, inbound, name, *_args, **_kwargs):
        calls.append(name)
        return f"Tool {name}: {marker}"

    monkeypatch.setattr(MaxwellBot, "_execute_tool_by_name", execute)
    monkeypatch.setattr(MaxwellBot, "_remember_tool_call", AsyncMock())
    native = [
        {
            "id": str(i),
            "type": "function",
            "function": {"name": name, "arguments": "{}"},
        }
        for i, name in enumerate([terminal, "send_message"])
    ]
    text, results = asyncio.run(
        MaxwellBot._dispatch_tool_calls(
            bot, msg, "extra prose", native_tool_calls=native
        )
    )
    assert calls == [terminal]
    assert text == ""
    assert not _tool_results_need_followup(results)


def test_guild_updates_and_removal_replace_catalogs():
    bot, msg, _ = fixture()
    em = Emoji("new", animated=True)
    sticker = SimpleNamespace(
        name="Animated hello",
        id=99,
        format=discord.StickerFormatType.apng,
        available=True,
    )
    bot._dev_scope_allows = lambda *a, **kw: True
    sync_guild(bot, msg.guild, emojis=[em], stickers=[sticker])
    assert bot._guild_emojis["1"] == {"new": str(em)}
    assert bot._guild_stickers["1"] == {"animated hello": "Animated hello"}
    sync_guild(bot, msg.guild, emojis=[], stickers=[])
    assert bot._guild_emojis["1"] == bot._guild_stickers["1"] == {}
    bot._dispatch_plugin_event = lambda *a: None
    asyncio.run(MaxwellBot.on_guild_remove(bot, msg.guild))
    assert "1" not in bot._guild_emojis


def test_global_refresh_replaces_additions_renames_and_deletions_and_keeps_failures():
    bot, msg, _ = fixture()
    sticker = SimpleNamespace(
        name="Wumpus hello", id=99, format=discord.StickerFormatType.lottie
    )
    em = Emoji("appwave", animated=True)
    bot.fetch_application_emojis = AsyncMock(return_value=[em])
    bot.fetch_premium_sticker_packs = AsyncMock(
        return_value=[SimpleNamespace(stickers=[sticker])]
    )
    asyncio.run(refresh_global_expressions(bot))
    assert available_emojis(bot, None) == {"appwave": str(em)}
    assert available_stickers(bot, None) == {"wumpus hello": sticker}
    assert MaxwellBot._render_custom_emojis(bot, ":appwave:", None) == str(em)
    bot.fetch_application_emojis = AsyncMock(return_value=[])
    bot.fetch_premium_sticker_packs = AsyncMock(side_effect=TimeoutError())
    asyncio.run(refresh_global_expressions(bot))
    assert bot._application_emojis == {}
    assert bot._standard_stickers == {"wumpus hello": sticker}
    bot.fetch_premium_sticker_packs = AsyncMock(return_value=[])
    asyncio.run(refresh_global_expressions(bot))
    assert bot._standard_stickers == {}


def test_refresh_task_is_single_tracked_and_cancellable():
    async def run():
        bot, _, _ = fixture()
        bot.is_closed = lambda: False
        bot.fetch_application_emojis = AsyncMock(return_value=[])
        bot.fetch_premium_sticker_packs = AsyncMock(return_value=[])
        tracked = []
        bot._track_task = lambda t: tracked.append(t) or t
        start_expression_refresh(bot)
        start_expression_refresh(bot)
        await asyncio.sleep(0)
        assert len(tracked) == 1
        tracked[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await tracked[0]

    asyncio.run(run())


def test_official_and_current_server_stickers_require_explicit_markers():
    bot, msg, _ = fixture()
    official = SimpleNamespace(
        name="Wumpus hello", id=99, format=discord.StickerFormatType.lottie
    )
    local = SimpleNamespace(name="hello", id=100, available=True)
    msg.guild.stickers = [local]
    bot._standard_stickers = {"wumpus hello": official}
    text, stickers = extract_stickers(
        bot, "hello there [STICKER (Wumpus hello)]", msg.guild
    )
    assert text == "hello there"
    assert stickers == [official]
    assert extract_stickers(bot, "hello there", msg.guild) == ("hello there", [])
    assert extract_stickers(bot, "[STICKER (hello)]", None)[1] == []


class Response:
    def __init__(self, status=200, payload=None):
        self.status = status
        self.payload = payload or {
            "data": [
                {
                    "images": {
                        "original": {
                            "url": "https://media.giphy.com/media/test/giphy.gif"
                        }
                    }
                }
            ]
        }

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def json(self):
        return self.payload


class Session:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def test_giphy_is_optional_and_has_a_native_schema():
    bot, _, _ = fixture()
    assert "send_gif" in {t.name for t in setup(bot)}
    bot.config.GIPHY_API_KEY = ""
    assert "send_gif" in {t.name for t in setup(bot)}
    schema = build_openai_tools({"send_gif": SendGifTool(bot)})[0]["function"]
    assert schema["parameters"]["required"] == ["query"]


def _gif_bytes():
    async def download(session, url):
        assert url.startswith("https://")
        return b"GIF89a-fake"

    return download


def test_giphy_sends_only_the_gif_file(monkeypatch):
    bot, msg, _ = fixture()
    session = Session(Response())
    monkeypatch.setattr(
        "plugins.discord_messages.impl._get_shared_session",
        AsyncMock(return_value=session),
    )
    monkeypatch.setattr("plugins.discord_messages.impl._download_gif", _gif_bytes())
    result = asyncio.run(SendGifTool(bot).execute(msg, query="happy cat"))
    assert result == "__GIF_SENT__"
    msg.channel.send.assert_awaited_once()
    args = msg.channel.send.await_args.kwargs
    assert "embed" not in args and "content" not in args
    assert args["file"].filename == "gif.gif"
    assert args["file"].fp.getvalue().startswith(b"GIF89a")
    assert "Powered By" not in str(args)
    assert args["allowed_mentions"].to_dict() == {"parse": []}
    assert session.calls[0][0] == "https://api.giphy.com/v1/gifs/search"
    assert session.calls[0][1]["params"]["q"] == "happy cat"
    assert session.calls[0][1]["params"]["rating"] == "pg-13"
    bot._record_delivery.assert_awaited_once()
    assert not _tool_results_need_followup(["Tool send_gif: " + result])


def test_reaction_gif_uses_keyless_catalog_and_sends_only_the_file(monkeypatch):
    bot, msg, _ = fixture()
    bot.config.GIPHY_API_KEY = ""
    session = Session(
        Response(
            payload={
                "results": [
                    {"url": "https://nekos.best/api/v2/hug/abc.gif"},
                ]
            }
        )
    )
    monkeypatch.setattr(
        "plugins.discord_messages.impl._get_shared_session",
        AsyncMock(return_value=session),
    )
    monkeypatch.setattr("plugins.discord_messages.impl._download_gif", _gif_bytes())
    result = asyncio.run(SendGifTool(bot).execute(msg, query="hug"))
    assert result == "__GIF_SENT__"
    args = msg.channel.send.await_args.kwargs
    assert args["file"].filename == "gif.gif"
    assert "content" not in args and "embed" not in args
    assert session.calls[0][0] == "https://nekos.best/api/v2/hug"
    assert "api_key" not in session.calls[0][1].get("params", {})
    assert "test-secret" not in str(session.calls)
    assert session.calls[0][1]["headers"]["User-Agent"].startswith("Maxwell (")


def test_unmatched_phrase_without_giphy_key_does_not_send(monkeypatch):
    bot, msg, _ = fixture()
    bot.config.GIPHY_API_KEY = ""
    session = Session(Response())
    monkeypatch.setattr(
        "plugins.discord_messages.impl._get_shared_session",
        AsyncMock(return_value=session),
    )
    result = asyncio.run(SendGifTool(bot).execute(msg, query="happy cat"))
    assert result.startswith("Error:")
    assert "test-secret" not in result
    msg.channel.send.assert_not_awaited()
    assert session.calls == []


def test_download_gif_rejects_non_gif_and_other_hosts():
    class Body:
        def __init__(self, data):
            self.data = data

        def iter_chunked(self, _size):
            data = self.data

            async def chunks():
                yield data

            return chunks()

    class DownloadResponse:
        def __init__(self, data, url="https://nekos.best/a.gif"):
            self.status = 200
            self.url = url
            self.content = Body(data)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class OneShot:
        def __init__(self, response):
            self.response = response

        def get(self, _url, **_kwargs):
            return self.response

    rejected = asyncio.run(
        _download_gif(
            OneShot(DownloadResponse(b"not-a-gif")),
            "https://nekos.best/a.gif",
        )
    )
    foreign = asyncio.run(
        _download_gif(
            OneShot(DownloadResponse(b"GIF89aok")),
            "https://evil.test/a.gif",
        )
    )
    accepted = asyncio.run(
        _download_gif(
            OneShot(DownloadResponse(b"GIF89aok")),
            "https://nekos.best/a.gif",
        )
    )
    assert rejected == b"" and foreign == b"" and accepted.startswith(b"GIF89a")


def test_attach_denied_sends_the_url_alone(monkeypatch):
    bot, msg, _ = fixture(attach_files=False)
    session = Session(Response())
    monkeypatch.setattr(
        "plugins.discord_messages.impl._get_shared_session",
        AsyncMock(return_value=session),
    )
    result = asyncio.run(SendGifTool(bot).execute(msg, query="happy cat"))
    assert result == "__GIF_SENT__"
    args = msg.channel.send.await_args.kwargs
    assert args["content"] == "https://media.giphy.com/media/test/giphy.gif"
    assert "file" not in args and "embed" not in args
    assert "Powered By" not in args["content"]


@pytest.mark.parametrize(
    "status,payload",
    [
        (403, None),
        (429, None),
        (200, {"data": []}),
        (
            200,
            {
                "data": [
                    {"images": {"original": {"url": "https://malicious.test/x.gif"}}}
                ]
            },
        ),
    ],
)
def test_giphy_bad_results_do_not_send_or_expose_key(monkeypatch, status, payload):
    bot, msg, _ = fixture()
    session = Session(Response(status, payload))
    monkeypatch.setattr(
        "plugins.discord_messages.impl._get_shared_session",
        AsyncMock(return_value=session),
    )
    result = asyncio.run(SendGifTool(bot).execute(msg, query="cat"))
    assert result.startswith("Error:")
    assert "test-secret" not in result
    msg.channel.send.assert_not_awaited()


def test_giphy_exception_keeps_api_key_private(monkeypatch):
    bot, msg, _ = fixture()
    monkeypatch.setattr(
        "plugins.discord_messages.impl._get_shared_session",
        AsyncMock(side_effect=aiohttp.ClientError("test-secret")),
    )
    result = asyncio.run(SendGifTool(bot).execute(msg, query="cat"))
    assert result.startswith("Error:") and "test-secret" not in result


def test_send_denial_on_real_slotted_message_can_end_silently():
    bot, msg, _ = fixture()

    class SlottedMessage:
        __slots__ = ("id", "channel", "guild")

    real = SlottedMessage()
    real.id, real.channel, real.guild = msg.id, msg.channel, msg.guild
    msg.channel.send.side_effect = discord.Forbidden(
        SimpleNamespace(status=403, reason="Denied"),
        {"code": 50013, "message": "denied"},
    )
    result = asyncio.run(SendMessageTool(bot).execute(real, content="hi", reply=False))
    assert "react or no_response" in result
    assert asyncio.run(NoResponseTool(bot).execute(real)) == "__NO_RESPONSE__"


@pytest.mark.parametrize("source", ["guild", "official"])
@pytest.mark.parametrize("fmt", list(discord.StickerFormatType))
@pytest.mark.parametrize("selector", ["King Me", "99"])
def test_all_sticker_formats_are_dispatched_natively(source, fmt, selector):
    bot, msg, _ = fixture()
    sticker = SimpleNamespace(
        name="King Me", id=99, format=fmt, available=True
    )
    if source == "guild":
        msg.guild.stickers = [sticker]
    else:
        bot._standard_stickers = {"king me": sticker}
    bot._extract_stickers_from_text = lambda text, guild: extract_stickers(
        bot, text, guild
    )
    result = asyncio.run(
        SendMessageTool(bot).execute(
            msg, content=f"[STICKER ({selector})]", reply=False
        )
    )
    assert result.startswith("__MESSAGE_SENT__")
    assert msg.channel.send.await_args.kwargs["stickers"] == [sticker]
    bot._record_delivery.assert_awaited_once()
    listing = asyncio.run(ListExpressionsTool(bot).execute(msg, query="King Me"))
    assert f"format={fmt.name}" in listing


def test_sticker_id_cannot_select_another_server_or_unavailable_sticker():
    bot, msg, _ = fixture()
    msg.guild.stickers = [SimpleNamespace(name="King Me", id=99, available=False)]
    for selector in ("99", "100", "King Me"):
        marker = f"[STICKER ({selector})]"
        assert extract_stickers(bot, marker, msg.guild) == (marker, [])


def test_invalid_native_sticker_is_not_retried_in_same_request():
    bot, msg, _ = fixture()
    sticker = SimpleNamespace(
        name="Wumpus hello", id=99, format=discord.StickerFormatType.lottie
    )
    bot._standard_stickers = {"wumpus hello": sticker}
    bot._extract_stickers_from_text = lambda text, guild: extract_stickers(
        bot, text, guild
    )
    msg.channel.send.side_effect = discord.HTTPException(
        SimpleNamespace(status=400, reason="Invalid"),
        {"code": 50081, "message": "Invalid sticker sent"},
    )

    async def run():
        first = await SendMessageTool(bot).execute(
            msg, content="[STICKER (Wumpus hello)]", reply=False
        )
        second = await SendMessageTool(bot).execute(
            msg, content="[STICKER (Wumpus hello)]", reply=False
        )
        return first, second

    first, second = asyncio.run(run())
    assert "Discord refused" in first
    assert "already refused" in second
    assert msg.channel.send.await_count == 1
    bot._record_delivery.assert_not_awaited()


def test_webhook_stickers_are_not_silently_discarded():
    bot, msg, _ = fixture()
    sticker = SimpleNamespace(
        name="Wumpus hello", id=99, format=discord.StickerFormatType.lottie
    )
    bot._standard_stickers = {"wumpus hello": sticker}
    bot._extract_stickers_from_text = lambda text, guild: extract_stickers(
        bot, text, guild
    )
    msg.channel._session = object()
    result = asyncio.run(
        SendMessageTool(bot).execute(
            msg, content="[STICKER (Wumpus hello)]", reply=False
        )
    )
    assert result.startswith("Error:") and "webhooks" in result
    msg.channel.send.assert_not_awaited()


def test_emoji_and_sticker_update_events_replace_catalogs():
    bot, msg, _ = fixture()
    bot.config.MAXWELL_DEV_MODE = False
    emoji = Emoji("changed", animated=True)
    sticker = SimpleNamespace(name="changed sticker", available=True)
    msg.guild.emojis = [emoji]
    msg.guild.stickers = [sticker]
    asyncio.run(MaxwellBot.on_guild_emojis_update(bot, msg.guild, [], [emoji]))
    asyncio.run(MaxwellBot.on_guild_stickers_update(bot, msg.guild, [], [sticker]))
    assert bot._guild_emojis["1"] == {"changed": str(emoji)}
    assert bot._guild_stickers["1"] == {"changed sticker": "changed sticker"}
    msg.guild.emojis = []
    msg.guild.stickers = []
    asyncio.run(MaxwellBot.on_guild_emojis_update(bot, msg.guild, [emoji], []))
    asyncio.run(MaxwellBot.on_guild_stickers_update(bot, msg.guild, [sticker], []))
    assert bot._guild_emojis["1"] == bot._guild_stickers["1"] == {}


def test_gif_memory_failure_does_not_turn_delivery_into_a_retry(monkeypatch):
    bot, msg, _ = fixture()
    bot._remember_sent_message = AsyncMock(side_effect=RuntimeError("offline storage"))
    monkeypatch.setattr(
        "plugins.discord_messages.impl._get_shared_session",
        AsyncMock(return_value=Session(Response())),
    )
    monkeypatch.setattr("plugins.discord_messages.impl._download_gif", _gif_bytes())
    assert asyncio.run(SendGifTool(bot).execute(msg, query="cat")) == "__GIF_SENT__"
    msg.channel.send.assert_awaited_once()


def test_pack_aliases_preserve_wumpus_when_sticker_names_collide():
    bot, msg, _ = fixture()
    wumpus = SimpleNamespace(name="Hello", id=1)
    other = SimpleNamespace(name="Hello", id=2)
    bot.fetch_premium_sticker_packs = AsyncMock(
        return_value=[
            SimpleNamespace(name="Wumpus", stickers=[wumpus]),
            SimpleNamespace(name="Other", stickers=[other]),
        ]
    )
    asyncio.run(refresh_global_expressions(bot))
    assert extract_stickers(bot, "[STICKER (Wumpus/Hello)]", msg.guild)[1] == [wumpus]
    assert extract_stickers(bot, "[STICKER (Other/Hello)]", msg.guild)[1] == [other]


def test_tool_text_cannot_spoof_terminal_social_delivery():
    assert _tool_results_need_followup(["Tool shell: __GIF_SENT__"])
    assert _tool_results_need_followup(["Tool react: Error: __REACTION_SENT__"])


class ReplyChannel:
    """In-memory Discord messages: assertions observe delivered channel state."""

    id = 2

    def __init__(self):
        self.messages = []
        self.sticker_error = None
        self.sticker_attempts = 0

    async def send(self, content=None, **kwargs):
        stickers = kwargs.get("stickers") or []
        if stickers:
            self.sticker_attempts += 1
            if self.sticker_error:
                raise self.sticker_error
        with discord.http.handle_message_parameters(
            content=content, stickers=[sticker.id for sticker in stickers]
        ) as parameters:
            payload = parameters.payload
        posted = SimpleNamespace(
            id=len(self.messages) + 1,
            content=payload.get("content") or "",
            sticker_ids=payload.get("sticker_ids", []),
            reference=kwargs.get("reference"),
            allowed_mentions=kwargs.get("allowed_mentions"),
            deleted=False,
        )

        async def delete():
            posted.deleted = True

        async def edit(content=None, **kwargs):
            posted.content = content

        posted.delete, posted.edit = delete, edit
        self.messages.append(posted)
        return posted


class NormalReplyBot:
    _deliver_normal_reply = MaxwellBot._deliver_normal_reply
    _send_with_slowmode = MaxwellBot._send_with_slowmode
    _reply_typing = MaxwellBot._reply_typing
    _render_custom_emojis = MaxwellBot._render_custom_emojis
    _extract_stickers_from_text = MaxwellBot._extract_stickers_from_text
    _split_response = staticmethod(MaxwellBot._split_response)

    def __init__(self, sticker):
        self._control = {"typing_indicator": False}
        self._application_emojis = {}
        self._standard_stickers = {"wumpus wave": sticker}
        self.sent_channels = []

    async def _respect_slowmode(self, channel):
        pass

    def _mark_bot_sent(self, channel):
        self.sent_channels.append(channel.id)


async def normal_reply_fixture():
    sticker = SimpleNamespace(id=99, name="Wumpus Wave", available=True)
    bot = NormalReplyBot(sticker)
    channel = ReplyChannel()
    msg = SimpleNamespace(
        id=4, channel=channel, guild=SimpleNamespace(id=1, emojis=[], stickers=[])
    )

    async def reply(content=None, **kwargs):
        return await channel.send(content, reference=msg.id, **kwargs)

    msg.reply = reply
    progress = ToolProgress(msg)
    await progress.start()
    assert progress.posted is not None
    return bot, msg, progress, sticker


@pytest.mark.parametrize("text", ["found it!", "first " * 500, ""], ids=["single-chunk", "multiple-chunks", "sticker-only"])
def test_normal_sticker_reply_after_progress_delivers_once_and_retires_progress(text):
    async def run():
        bot, msg, progress, sticker = await normal_reply_fixture()
        placeholder = progress.posted
        response, delivered = await bot._deliver_normal_reply(
            msg, text + " [STICKER (wumpus wave)]", [progress]
        )
        for _ in range(50):
            await asyncio.sleep(0)
        visible = [m for m in msg.channel.messages if not m.deleted]
        assert delivered
        assert placeholder.deleted
        assert " ".join("".join(m.content for m in visible).split()) == " ".join(text.split())
        assert visible[0].sticker_ids == [sticker.id]
        assert visible[0].reference == msg.id
        assert all(not m.sticker_ids for m in visible[1:])
        assert msg.channel.sticker_attempts == 1
        # Outer-turn cleanup must not retire the actual reply or send it again.
        await progress.stop()
        assert all(not m.deleted for m in visible)

    asyncio.run(run())


def test_numeric_sticker_survives_cleanup_and_progress_as_native_payload():
    async def run():
        bot, msg, progress, sticker = await normal_reply_fixture()
        sticker.id = 796140620111544330
        raw = f"@Alice(123456789012345678) found it! [STICKER ({sticker.id})]"
        _, delivered = await bot._deliver_normal_reply(
            msg, _sanitize_visible_reply(raw), [progress]
        )
        for _ in range(50):
            await asyncio.sleep(0)
        visible = [m for m in msg.channel.messages if not m.deleted]
        assert delivered
        assert [(m.content, m.sticker_ids) for m in visible] == [
            ("<@123456789012345678> found it!", [sticker.id])
        ]

    asyncio.run(run())


def test_normal_text_reply_keeps_progress_transition_delivery():
    async def run():
        bot, msg, progress, _ = await normal_reply_fixture()
        placeholder = progress.posted
        _, delivered = await bot._deliver_normal_reply(msg, "found it!", [progress])
        for _ in range(50):
            await asyncio.sleep(0)
        visible = [m for m in msg.channel.messages if not m.deleted]
        assert delivered
        assert placeholder.deleted
        assert [m.content for m in visible] == ["found it!"]
        assert visible[0].reference == msg.id
        assert not visible[0].sticker_ids

    asyncio.run(run())


def test_normal_sticker_failure_after_progress_reaches_reply_error_handler():
    async def run():
        bot, msg, progress, _ = await normal_reply_fixture()
        placeholder = progress.posted
        failure = discord.HTTPException(
            SimpleNamespace(status=400, reason="Invalid"),
            {"code": 50081, "message": "Invalid sticker sent"},
        )
        msg.channel.sticker_error = failure
        with pytest.raises(discord.HTTPException) as caught:
            await bot._deliver_normal_reply(
                msg, "found it! [STICKER (wumpus wave)]", [progress]
            )
        assert caught.value is failure
        for _ in range(50):
            await asyncio.sleep(0)
        assert placeholder.deleted
        assert not [m for m in msg.channel.messages if not m.deleted]
        assert msg.channel.sticker_attempts == 1

    asyncio.run(run())
