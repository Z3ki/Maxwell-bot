"""Welcome/system events, presence, clips, and extra media types in context."""

from types import SimpleNamespace

from bot import MaxwellBot
from tool_schemas import RESULT_TOOL_NAMES, result_contract
from utils import (
    message_has_visible_payload,
    message_is_discord_system_event,
    render_discord_context_text,
)


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
        SimpleNamespace(
            _format_presence_activity=MaxwellBot._format_presence_activity
        ),
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


def test_guide_is_a_result_tool():
    assert "guide" in RESULT_TOOL_NAMES
    assert "returns output" in result_contract("guide")
