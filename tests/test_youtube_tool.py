import asyncio
import json

from bot_tools import YouTubeTool


def test_url_kind_channel_videos_page():
    assert (
        YouTubeTool._url_kind("https://www.youtube.com/@xehanortvp/videos")
        == "channel"
    )
    assert YouTubeTool._url_kind("https://www.youtube.com/@xehanortvp") == "channel"
    assert (
        YouTubeTool._url_kind("https://www.youtube.com/channel/UC1234567890123456789012")
        == "channel"
    )


def test_url_kind_video_and_playlist():
    assert YouTubeTool._url_kind("https://youtu.be/dQw4w9WgXcQ") == "video"
    assert (
        YouTubeTool._url_kind("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "video"
    )
    assert (
        YouTubeTool._url_kind("https://www.youtube.com/shorts/dQw4w9WgXcQ") == "video"
    )
    assert (
        YouTubeTool._url_kind(
            "https://www.youtube.com/playlist?list=PLabcdefghijklmnopqrstuvwx"
        )
        == "playlist"
    )


def test_extract_handle_and_channel_id():
    assert YouTubeTool._extract_youtube_url("@xehanortvp") == (
        "https://www.youtube.com/@xehanortvp/videos"
    )
    assert YouTubeTool._extract_youtube_url("@xehanortvp/shorts") == (
        "https://www.youtube.com/@xehanortvp/shorts"
    )
    cid = "UC1234567890123456789012"
    assert YouTubeTool._extract_youtube_url(cid) == (
        f"https://www.youtube.com/channel/{cid}/videos"
    )


def test_normalize_list_url_adds_videos_tab():
    out = YouTubeTool._normalize_list_url(
        "https://www.youtube.com/@xehanortvp", "channel"
    )
    assert out.endswith("/@xehanortvp/videos")
    already = YouTubeTool._normalize_list_url(
        "https://www.youtube.com/@xehanortvp/shorts", "channel"
    )
    assert already.endswith("/@xehanortvp/shorts")


def test_format_catalog_lists_watch_urls():
    text = YouTubeTool._format_catalog(
        "channel",
        {
            "title": "Xehanort - Videos",
            "channel": "Xehanort",
            "entries": [
                {"id": "mnLCbYNBZps", "title": "Vogel im kafig", "duration": 233},
                {"id": "irhYHv05EIo", "title": "MidiPlayer Showcase"},
            ],
        },
        15,
    )
    assert "Type: channel" in text
    assert "Vogel im kafig (3:53)" in text
    assert "https://www.youtube.com/watch?v=mnLCbYNBZps" in text
    assert "MidiPlayer Showcase" in text


def test_execute_lists_channel_instead_of_transcript():
    YouTubeTool._result_cache.clear()
    tool = YouTubeTool(bot=None)
    payload = {
        "title": "Xehanort - Videos",
        "channel": "Xehanort",
        "entries": [
            {"id": "mnLCbYNBZps", "title": "Vogel im kafig", "duration": 233},
        ],
    }

    async def fake_dump(url, limit):
        assert "/videos" in url
        assert limit == 5
        return payload

    tool._dump_playlist = fake_dump  # type: ignore[method-assign]

    async def run():
        return await tool.execute(
            message=None,  # type: ignore[arg-type]
            url="https://www.youtube.com/@xehanortvp/videos",
            limit=5,
        )

    out = asyncio.run(run())
    assert "Type: channel" in out
    assert "Vogel im kafig" in out
    assert "Transcript:" not in out


def test_execute_search_query_uses_ytsearch():
    YouTubeTool._result_cache.clear()
    tool = YouTubeTool(bot=None)
    seen = {}

    async def fake_dump(url, limit):
        seen["url"] = url
        seen["limit"] = limit
        return {
            "title": "ytsearch",
            "entries": [{"id": "abc123xyz", "title": "hit"}],
        }

    tool._dump_playlist = fake_dump  # type: ignore[method-assign]
    out = asyncio.run(tool.execute(message=None, query="piano gardens"))  # type: ignore[arg-type]
    assert seen["url"].startswith("ytsearch")
    assert "piano gardens" in seen["url"]
    assert "hit" in out
    assert "Type: search" in out
