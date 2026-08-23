"""Media posted as a bare link, not an upload.

Discord unfurls only some links into embeds and never audio ones, so these
references used to reach the model as plain text — it could read the URL but
never see or hear what was behind it.
"""

import asyncio

import pytest

from bot import MaxwellBot, _owner_audio_input_enabled


refs = MaxwellBot._media_link_refs


def test_picks_up_image_and_audio_links():
    assert refs("look at https://example.com/cat.png") == [
        ("https://example.com/cat.png", ".png")
    ]
    assert refs("listen https://cdn.site/clip.mp3 please") == [
        ("https://cdn.site/clip.mp3", ".mp3")
    ]


@pytest.mark.parametrize(
    "ext",
    [
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".mp3",
        ".wav",
        ".ogg",
        ".m4a",
        ".flac",
        ".aac",
        ".mp4",
        ".webm",
        ".mov",
    ],
)
def test_every_supported_extension_is_recognised(ext):
    assert refs(f"https://e.com/file{ext}") == [(f"https://e.com/file{ext}", ext)]


def test_ignores_non_media_but_recognizes_video_links():
    assert refs("https://e.com/v.mp4 https://e.com/doc.pdf https://e.com/page") == [
        ("https://e.com/v.mp4", ".mp4")
    ]


def test_youtube_is_not_stolen_by_generic_video_path():
    assert refs("https://www.youtube.com/video.mp4 https://youtu.be/watch.mp4") == []


def test_tenor_and_giphy_pages_count_as_gifs():
    tenor = "https://tenor.com/view/cat-dancing-gif-12345678"
    giphy = "https://giphy.com/gifs/funny-cat-abcXYZ"
    assert refs(f"lol {tenor}") == [(tenor, ".gif")]
    assert refs(giphy) == [(giphy, ".gif")]
    assert refs("https://i.imgur.com/abc123.gifv") == [
        ("https://i.imgur.com/abc123.gifv", ".gif")
    ]


def test_plain_website_still_ignored():
    assert refs("https://tenor.com") == []
    assert refs("https://imgur.com/gallery/cats") == []
    assert refs("https://example.com/blog/post") == []


def test_strips_trailing_punctuation_and_angle_brackets():
    assert refs("see https://example.com/a.jpg.") == [
        ("https://example.com/a.jpg", ".jpg")
    ]
    assert refs("<https://example.com/b.webp>") == [
        ("https://example.com/b.webp", ".webp")
    ]


def test_keeps_query_strings_intact():
    # Discord CDN links carry ?ex=&is=&hm= signatures; dropping them 404s.
    url = "https://cdn.discordapp.com/x/y.png?ex=abc&is=def"
    assert refs(f"pic {url}") == [(url, ".png")]


def test_collapses_duplicate_urls():
    assert refs("https://e.com/a.png and again https://e.com/a.png") == [
        ("https://e.com/a.png", ".png")
    ]


def test_preserves_order_across_kinds():
    assert refs("https://e.com/a.png then https://e.com/b.wav") == [
        ("https://e.com/a.png", ".png"),
        ("https://e.com/b.wav", ".wav"),
    ]


def test_empty_and_missing_content():
    assert refs("") == []
    assert refs(None) == []


class _FakeMessage:
    def __init__(self, content):
        self.content = content
        self.id = 4242


def _harness(control):
    """A stand-in `self` for _extract_linked_media: no network, no bot."""
    import types

    calls = []

    async def _download(url, filename, max_size, message_id):
        calls.append((url, filename))
        mime = "video/mp4" if url.endswith((".mp4", ".webm", ".mov")) else "image/png"
        return {"b64": "AAA=", "mime_type": mime, "filename": filename, "url": url}

    fake = types.SimpleNamespace(
        _control=control,
        config=types.SimpleNamespace(ENABLE_VIDEO_INPUT=True),
        _max_media_bytes=lambda: 10 * 1024 * 1024,
        _download_embed_media=_download,
        _media_link_refs=MaxwellBot._media_link_refs,
        _LINK_IMAGE_EXTS=MaxwellBot._LINK_IMAGE_EXTS,
        _LINK_AUDIO_EXTS=MaxwellBot._LINK_AUDIO_EXTS,
        _LINK_VIDEO_EXTS=MaxwellBot._LINK_VIDEO_EXTS,
    )
    return fake, calls


def _run(control, content, **kwargs):
    fake, calls = _harness(control)
    media = asyncio.run(
        MaxwellBot._extract_linked_media(fake, _FakeMessage(content), **kwargs)
    )
    return media, [u for u, _ in calls]


def test_audio_link_skipped_when_audio_processing_off():
    media, fetched = _run(
        {"process_images": True, "process_audio": False},
        "https://e.com/a.png https://e.com/b.wav",
    )
    assert fetched == ["https://e.com/a.png"]
    assert len(media) == 1


def test_audio_link_still_fetched_when_images_off():
    # A linked clip should land even on a bot with image processing disabled.
    media, fetched = _run(
        {"process_images": False, "process_audio": True},
        "https://e.com/a.png https://e.com/b.wav",
    )
    assert fetched == ["https://e.com/b.wav"]
    assert len(media) == 1


def test_nothing_fetched_when_both_off():
    media, fetched = _run(
        {"process_images": False, "process_audio": False},
        "https://e.com/a.png https://e.com/b.wav",
    )
    assert fetched == []
    assert media == []


def test_skip_urls_prevents_double_download_of_unfurled_embed():
    media, fetched = _run(
        {"process_images": True, "process_audio": True},
        "https://e.com/a.png https://e.com/b.wav",
        skip_urls={"https://e.com/a.png"},
    )
    assert fetched == ["https://e.com/b.wav"]
    assert len(media) == 1


def test_caps_at_five_items():
    content = " ".join(f"https://e.com/{i}.png" for i in range(9))
    media, fetched = _run({"process_images": True, "process_audio": True}, content)
    assert len(fetched) == 5
    assert len(media) == 5


def test_audio_flag_prefers_dashboard_then_env():
    from types import SimpleNamespace

    assert (
        _owner_audio_input_enabled(
            SimpleNamespace(_control={"process_audio": False}, config=SimpleNamespace(ENABLE_AUDIO_INPUT=True))
        )
        is False
    )
    assert (
        _owner_audio_input_enabled(
            SimpleNamespace(_control={}, config=SimpleNamespace(ENABLE_AUDIO_INPUT=True))
        )
        is True
    )
    assert _owner_audio_input_enabled(SimpleNamespace(_control={}, config=None)) is False


def test_items_are_tagged_with_source_and_url():
    media, _ = _run(
        {"process_images": True, "process_audio": True}, "https://e.com/a.png"
    )
    assert media[0]["source"] == "link"
    assert media[0]["url"] == "https://e.com/a.png"
    assert media[0]["filename"] == "linked-media-1.png"


def test_video_link_is_downloaded_and_reduced_to_provider_safe_derivatives():
    fake, calls = _harness({"process_images": True, "process_audio": True})

    async def _derive(item, max_size, *, source_prefix):
        assert item["url"] == "https://e.com/clip.mp4"
        assert source_prefix == "link_video"
        return [
            {
                "b64": "ZnJhbWU=",
                "mime_type": "image/jpeg",
                "filename": "clip-frame.jpg",
                "is_image": True,
                "message_id": 4242,
            }
        ]

    fake._extract_video_item_derivatives = _derive
    media = asyncio.run(
        MaxwellBot._extract_linked_media(
            fake, _FakeMessage("https://e.com/clip.mp4")
        )
    )

    assert [url for url, _ in calls] == ["https://e.com/clip.mp4"]
    assert media and media[0]["is_image"] is True
    assert media[0]["url"] == "https://e.com/clip.mp4"


def test_video_link_can_keep_audio_when_images_are_disabled():
    fake, calls = _harness({"process_images": False, "process_audio": True})
    seen = {}

    async def _derive(item, max_size, *, source_prefix):
        seen["include"] = fake._control["process_images"]
        return [
            {
                "b64": "UkFXX0FVRElP",
                "mime_type": "audio/wav",
                "filename": "clip-audio.wav",
                "is_image": False,
                "message_id": 4242,
            }
        ]

    fake._extract_video_item_derivatives = _derive
    media = asyncio.run(
        MaxwellBot._extract_linked_media(
            fake, _FakeMessage("https://e.com/clip.mp4")
        )
    )

    assert [url for url, _ in calls] == ["https://e.com/clip.mp4"]
    assert seen["include"] is False
    assert media[0]["mime_type"] == "audio/wav"


def test_video_link_is_skipped_when_video_input_is_disabled():
    fake, calls = _harness({"process_images": True, "process_audio": True})
    fake.config.ENABLE_VIDEO_INPUT = False
    media = asyncio.run(
        MaxwellBot._extract_linked_media(
            fake, _FakeMessage("https://e.com/clip.mp4")
        )
    )

    assert calls == []
    assert media == []
