"""Direct video-link handling and provider-safe audio/frame extraction."""

import asyncio
import base64
from types import SimpleNamespace

from bot_tools import FetchUrlTool, SeeVideoTool
from tool_schemas import RESULT_TOOL_NAMES, TOOL_PARAMETERS


def _message():
    return SimpleNamespace(id=7, channel=SimpleNamespace(id=8), guild=None)


def test_video_tool_is_registered_and_keeps_youtube_on_its_own_path():
    assert "see_video" in TOOL_PARAMETERS
    assert "see_video" in RESULT_TOOL_NAMES
    assert SeeVideoTool.looks_video("https://cdn.example/clip.mp4")
    assert not SeeVideoTool.looks_video(
        "https://www.youtube.com/watch.mp4?v=dQw4w9WgXcQ"
    )
    assert SeeVideoTool.looks_video("https://notyoutube.com/clip.mp4")
    assert not SeeVideoTool.looks_video("https://example.com/article")


def test_fetch_url_delegates_direct_video_without_decoding_it(monkeypatch):
    calls = []

    async def delegate(self, message, *, url=None, **kwargs):
        calls.append(url)
        return "video handled"

    monkeypatch.setattr("bot_tools.SeeVideoTool.execute", delegate)
    bot = SimpleNamespace(
        _control={"process_images": True, "process_audio": True},
        _download_embed_media=lambda *args, **kwargs: None,
        _authorize_tool_execution=lambda *_a, **_k: None,
    )
    bot.tools = {"see_video": SeeVideoTool(bot)}
    result = asyncio.run(
        FetchUrlTool(bot).execute(_message(), url="https://cdn.example/clip.mp4")
    )

    assert result == "video handled"
    assert calls == ["https://cdn.example/clip.mp4"]


def test_fetch_url_rejects_audio_payload_as_text(monkeypatch):
    async def fetch(_url, *, max_bytes, timeout):
        assert timeout == 12.0
        return "https://cdn.example/clip.mp3", "audio/mpeg", b"\xff\xfe\x00\x01"

    monkeypatch.setattr("bot_tools._fetch_public_url", fetch)
    off = SimpleNamespace(mark_message_tainted=lambda *_args: None)
    off_result = asyncio.run(
        FetchUrlTool(off).execute(_message(), url="https://cdn.example/clip.mp3")
    )
    assert off_result.startswith("Error:")
    assert "audio input is off" in off_result
    assert "Do not transcribe" in off_result
    assert "\ufffd" not in off_result

    on = SimpleNamespace(
        mark_message_tainted=lambda *_args: None,
        config=SimpleNamespace(ENABLE_AUDIO_INPUT=True),
        _control={"process_audio": True},
    )
    on_result = asyncio.run(
        FetchUrlTool(on).execute(_message(), url="https://cdn.example/clip.mp3")
    )
    assert on_result.startswith("Error:")
    assert "audio media" in on_result
    assert "\ufffd" not in on_result


def test_video_tool_returns_frames_and_audio_for_followup_input():
    derived = [
        {
            "b64": base64.b64encode(b"jpeg").decode(),
            "mime_type": "image/jpeg",
            "filename": "frame.jpg",
            "is_image": True,
        },
        {
            "b64": base64.b64encode(b"wav").decode(),
            "mime_type": "audio/wav",
            "filename": "audio.wav",
            "is_image": False,
        },
    ]
    cached = []

    async def extract(*args, **kwargs):
        return derived

    bot = SimpleNamespace(
        _control={"process_images": True, "process_audio": True},
        config=SimpleNamespace(ENABLE_VIDEO_INPUT=True),
        _max_media_bytes=lambda: 1024,
        _extract_video_derivatives=extract,
        _cache_media_context=lambda channel_id, media: cached.append(
            (channel_id, media)
        ),
    )
    result = asyncio.run(
        SeeVideoTool(bot).result_from_blob(
            b"video", "video/mp4", "https://cdn.example/clip.mp4", _message()
        )
    )

    assert "__IMAGE_B64__" in result
    assert "__AUDIO_B64__" in result
    assert "audio track was extracted" in result
    assert cached and cached[0][0] == "8"
    assert len(cached[0][1]) == 1
