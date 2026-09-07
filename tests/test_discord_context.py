"""Welcome/system events, presence, clips, and extra media types in context."""

from types import SimpleNamespace

from bot import MaxwellBot
from tool_schemas import RESULT_TOOL_NAMES, result_contract
from utils import (
    _attachment_annotation,
    _compact_attachments_note,
    _compact_embeds_note,
    message_has_visible_payload,
    message_is_discord_system_event,
    render_discord_context_text,
)

CDN_TXT = "https://cdn.discordapp.com/attachments/1/2/message.txt?ex=abc&is=def&hm=ghi"
CDN_PNG = "https://cdn.discordapp.com/attachments/1/2/shot.png?ex=aaa&is=bbb&hm=ccc"


def _ctx_message(**kwargs):
    base = {
        "type": "MessageType.default",
        "content": "",
        "author": SimpleNamespace(display_name="Alice", id=1),
        "attachments": [],
        "embeds": [],
        "stickers": [],
        "components": [],
        "poll": None,
        "mentions": [],
        "channel_mentions": [],
        "role_mentions": [],
        "created_at": None,
        "guild": None,
        "channel": SimpleNamespace(id=9),
    }
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_welcome_join_is_a_visible_system_event():
    msg = SimpleNamespace(
        type="MessageType.new_member",
        content="",
        system_content="Welcome, Alice. We hope you brought pizza.",
        author=SimpleNamespace(display_name="Alice", id=1),
        attachments=[],
        embeds=[],
        stickers=[],
        components=[],
        poll=None,
        mentions=[],
        channel_mentions=[],
        role_mentions=[],
        created_at=None,
        guild=None,
    )
    assert message_is_discord_system_event(msg) is True
    assert message_has_visible_payload(msg) is True
    text = render_discord_context_text(msg)
    assert "Welcome, Alice" in text
    assert "[system:" in text


def test_ordinary_replies_are_not_labeled_system():
    msg = SimpleNamespace(
        type="MessageType.reply",
        content="hey",
        author=SimpleNamespace(display_name="Bob", id=2),
        attachments=[],
        embeds=[],
        stickers=[],
        components=[],
        poll=None,
        mentions=[],
        channel_mentions=[],
        role_mentions=[],
        created_at=None,
        guild=None,
    )
    assert message_is_discord_system_event(msg) is False
    assert "[system:" not in render_discord_context_text(msg)


def test_clip_and_voice_attachments_are_annotated():
    clip = SimpleNamespace(
        filename="clip.mp4",
        content_type="video/mp4",
        duration=None,
        waveform=None,
        flags=SimpleNamespace(clip=True, spoiler=False),
        title="Hallway",
        description="",
        clip_created_at="2026-01-01",
        clip_participants=[SimpleNamespace(display_name="Z3ki")],
        application=SimpleNamespace(name="VALORANT"),
        width=1920,
        height=1080,
        is_spoiler=lambda: False,
    )
    voice = SimpleNamespace(
        filename="voice.ogg",
        content_type="audio/ogg",
        duration=4.2,
        waveform=b"xx",
        flags=SimpleNamespace(clip=False, spoiler=False),
        title=None,
        description=None,
        clip_created_at=None,
        clip_participants=None,
        application=None,
        width=None,
        height=None,
        is_spoiler=lambda: False,
        is_voice_message=lambda: True,
    )
    msg = SimpleNamespace(
        type="MessageType.default",
        content="look",
        author=SimpleNamespace(display_name="Alice", id=1),
        attachments=[clip, voice],
        embeds=[],
        stickers=[],
        components=[],
        poll=None,
        mentions=[],
        channel_mentions=[],
        role_mentions=[],
        created_at=None,
        guild=None,
    )
    text = render_discord_context_text(msg)
    assert "clip" in text
    assert "VALORANT" in text
    assert "Hallway" in text
    assert "voice message" in text


def test_heic_and_opus_links_are_annotated():
    msg = SimpleNamespace(
        type="MessageType.default",
        content="https://cdn.example/shot.heic https://cdn.example/note.opus",
        author=SimpleNamespace(display_name="Alice", id=1),
        attachments=[],
        embeds=[],
        stickers=[],
        components=[],
        poll=None,
        mentions=[],
        channel_mentions=[],
        role_mentions=[],
        created_at=None,
        guild=None,
    )
    text = render_discord_context_text(msg)
    assert "shot.heic" in text
    assert "note.opus" in text


def test_presence_includes_game_and_voice():
    author = SimpleNamespace(
        id=1,
        display_name="Alice",
        status="online",
        activities=[
            SimpleNamespace(
                type=SimpleNamespace(name="playing"),
                name="Minecraft",
                state="Survival",
                details="Overworld",
                title=None,
                artists=None,
                url=None,
                emoji=None,
            )
        ],
        voice=SimpleNamespace(
            channel=SimpleNamespace(name="general", id=9),
            self_mute=True,
            mute=False,
            self_deaf=False,
            deaf=False,
            self_stream=False,
            self_video=False,
        ),
        timed_out_until=None,
    )
    msg = SimpleNamespace(author=author, content="hey")
    text = MaxwellBot._get_music_context(
        SimpleNamespace(_format_presence_activity=MaxwellBot._format_presence_activity),
        msg,
    )
    assert "online" in text
    assert "playing Minecraft" in text
    assert "in voice #general" in text
    assert "muted" in text


def test_silent_tools_do_not_tell_the_model_to_send_a_placeholder():
    text = result_contract("react").lower()
    assert "send_message" not in text
    assert "same batch" not in text


def test_create_thread_is_a_result_tool():
    assert "create_thread" in RESULT_TOOL_NAMES
    assert "thread_control" in RESULT_TOOL_NAMES
    assert "guide" not in RESULT_TOOL_NAMES
    assert "returns output" in result_contract("create_thread")
    assert "returns output" in result_contract("thread_control")


def test_attachment_note_keeps_mime_charset_and_cdn_query_string():
    att = SimpleNamespace(
        filename="message.txt",
        content_type="text/plain; charset=utf-8",
        url=CDN_TXT,
        proxy_url="https://media.discordapp.net/attachments/1/2/message.txt",
        size=12,
    )
    msg = _ctx_message(content="here", attachments=[att])
    note = _compact_attachments_note(msg)
    assert note == (
        "[attachments: message.txt (text/plain; charset=utf-8) " + CDN_TXT + "]"
    )
    assert "charset=utf-8" in note
    assert "?ex=abc&is=def&hm=ghi" in note
    assert "media.discordapp.net" not in note
    live = render_discord_context_text(msg)
    assert note in live
    assert CDN_TXT in _attachment_annotation(att)
    assert f"[file: message.txt {CDN_TXT}]" in live


def test_attachment_note_falls_back_to_size_without_mime():
    att = SimpleNamespace(
        filename="blob.bin",
        content_type=None,
        size=1234,
        url="https://cdn.discordapp.com/attachments/1/2/blob.bin?ex=1&is=2&hm=3",
    )
    msg = _ctx_message(attachments=[att])
    note = _compact_attachments_note(msg)
    assert note == (
        "[attachments: blob.bin (1234 bytes) "
        "https://cdn.discordapp.com/attachments/1/2/blob.bin?ex=1&is=2&hm=3]"
    )


def test_embed_note_includes_type_quoted_title_and_media_urls():
    embed = SimpleNamespace(
        type=SimpleNamespace(name="video"),
        title='Watch "this" clip',
        description="  lots   of   space  ",
        url="https://youtube.com/watch?v=abc",
        image=None,
        video=SimpleNamespace(url="https://cdn.example/v.mp4"),
        thumbnail=SimpleNamespace(url="https://i.ytimg.com/vi/abc/hq.jpg"),
        author=None,
        provider=None,
        fields=[],
        footer=None,
    )
    msg = _ctx_message(content="link", embeds=[embed])
    note = _compact_embeds_note(msg)
    assert note.startswith("[embeds: video \"Watch 'this' clip\" ")
    assert "https://youtube.com/watch?v=abc" in note
    assert "https://cdn.example/v.mp4" in note
    assert "i.ytimg.com" not in note
    live = render_discord_context_text(msg)
    assert note in live
    assert "[embed: video Watch 'this' clip" in live or "[embed: video Watch" in live


def test_embed_note_uses_thumbnail_when_other_urls_are_missing():
    embed = SimpleNamespace(
        type="rich",
        title="card",
        description="",
        url=None,
        image=None,
        video=None,
        thumbnail=SimpleNamespace(url="https://pbs.twimg.com/media/x.jpg"),
    )
    note = _compact_embeds_note(_ctx_message(embeds=[embed]))
    assert note == '[embeds: rich "card" https://pbs.twimg.com/media/x.jpg]'


def test_message_memory_content_includes_attachment_and_embed_notes():
    att = SimpleNamespace(
        filename="message.txt",
        content_type="text/plain; charset=utf-8",
        url=CDN_TXT,
        proxy_url="https://media.discordapp.net/attachments/1/2/message.txt",
    )
    embed = SimpleNamespace(
        type="image",
        title="preview",
        description="updated embed description",
        url="https://example.com/post",
        image=SimpleNamespace(url=CDN_PNG),
        thumbnail=None,
        video=None,
        author=None,
        provider=None,
        fields=[],
        footer=None,
    )
    msg = _ctx_message(content="new text", attachments=[att], embeds=[embed])
    bot = SimpleNamespace(_recent_users={})
    text = MaxwellBot._message_memory_content(bot, msg)
    assert "new text" in text
    assert (
        "[attachments: message.txt (text/plain; charset=utf-8) " + CDN_TXT + "]"
    ) in text
    assert '[embeds: image "preview" https://example.com/post ' + CDN_PNG + "]" in text
    assert text.count("[attachments:") == 1
    assert text.count("[embeds:") == 1
    assert CDN_TXT in text
    assert CDN_PNG in text


def test_live_context_includes_curlable_attachment_url():
    att = SimpleNamespace(
        filename="shot.png",
        content_type="image/png",
        url=CDN_PNG,
        duration=None,
        waveform=None,
        flags=None,
        title=None,
        description=None,
        clip_created_at=None,
        clip_participants=None,
        application=None,
        width=64,
        height=64,
        is_spoiler=lambda: False,
    )
    msg = _ctx_message(content="look", attachments=[att])
    live = render_discord_context_text(msg, "look")
    assert CDN_PNG in live
    assert f"[image: shot.png {CDN_PNG}]" in live
    assert f"[attachments: shot.png (image/png) {CDN_PNG}]" in live


def test_compact_notes_are_not_repeated_when_content_already_has_them():
    att = SimpleNamespace(
        filename="a.png",
        content_type="image/png",
        url=CDN_PNG,
    )
    msg = _ctx_message(content="hi", attachments=[att])
    existing = _compact_attachments_note(msg)
    rendered = render_discord_context_text(msg, f"hi {existing}")
    assert rendered.count("[attachments:") == 1


def test_forwarded_snapshot_attachment_url_reaches_compact_note():
    snap = SimpleNamespace(
        content="peek",
        attachments=[
            SimpleNamespace(
                filename="secret.png",
                content_type="image/png",
                url=CDN_PNG,
            )
        ],
        embeds=[],
        stickers=[],
        components=[],
        poll=None,
    )
    msg = _ctx_message(
        content="",
        message_snapshots=[snap],
        flags=SimpleNamespace(forwarded=True),
        reference=SimpleNamespace(
            type=SimpleNamespace(name="forward"),
            channel_id=77,
            message_id=88,
            guild_id=66,
        ),
    )
    note = _compact_attachments_note(msg)
    assert "secret.png" in note
    assert CDN_PNG in note
    bot = SimpleNamespace(_recent_users={})
    text = MaxwellBot._message_memory_content(bot, msg)
    assert "secret.png" in text
    assert CDN_PNG in text
    assert "peek" in text
