"""see_image tool + GIF-page resolution.

Uploaded .gif files already become a JPEG contact sheet. Discord GIF-picker
links (tenor.com/view/..., giphy.com/gifs/...) have no file extension, so they
used to reach the model as plain text. fetch_url decoded the bytes as text.
"""

import asyncio
import base64
from types import SimpleNamespace

from bot import MaxwellBot
from bot_tools import FetchUrlTool, SeeImageTool
from tool_schemas import RESULT_TOOL_NAMES, TOOL_PARAMETERS
from utils import is_direct_image_url, is_gif_page_url


_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def test_schema_and_contract_are_wired():
    assert "see_image" in TOOL_PARAMETERS
    assert "url" in TOOL_PARAMETERS["see_image"]["properties"]
    assert "see_image" in RESULT_TOOL_NAMES


def test_og_image_extracted_from_either_attr_order():
    html = """
    <html><head>
      <meta property="og:image" content="https://media.tenor.com/x/tenor.gif">
      <meta content="https://media.tenor.com/x/clip.mp4" property="og:video">
    </head></html>
    """
    assert MaxwellBot._og_media_urls(html) == [
        "https://media.tenor.com/x/tenor.gif",
        "https://media.tenor.com/x/clip.mp4",
    ]
    html_rev = '<meta content="https://i.giphy.com/abc.gif" name="twitter:image">'
    assert MaxwellBot._og_media_urls(html_rev) == ["https://i.giphy.com/abc.gif"]


def test_gif_page_helpers():
    assert is_gif_page_url("https://tenor.com/view/cat-dancing-gif-123")
    assert is_gif_page_url("https://media1.tenor.com/m/abc/foo.gif")
    assert is_gif_page_url("https://giphy.com/gifs/funny-cat-abc")
    assert is_gif_page_url("https://i.imgur.com/abc.gifv")
    assert is_gif_page_url("https://gph.is/abc123")
    assert is_gif_page_url("https://klipy.com/gif/funny-cat")
    assert is_gif_page_url("https://cdn.klipy.com/x/clip.mp4")
    assert not is_gif_page_url("https://example.com/page")
    assert not is_gif_page_url("https://imgur.com/gallery/cats")
    assert is_direct_image_url("https://cdn.discordapp.com/x/y.png?ex=1")
    assert SeeImageTool.looks_visual("https://tenor.com/view/x-gif-1")
    assert SeeImageTool.looks_visual("https://e.com/pic.jpg")
    assert not SeeImageTool.looks_visual("https://example.com/article")


def test_see_image_rejects_bad_input():
    bot = SimpleNamespace(_control={"process_images": True})
    tool = SeeImageTool(bot)
    msg = SimpleNamespace(id=1, channel=SimpleNamespace(id=9))
    assert asyncio.run(tool.execute(msg, url=None)).startswith("Error:")
    assert asyncio.run(tool.execute(msg, url="http://127.0.0.1/x.gif")).startswith(
        "Error:"
    )
    bot._control["process_images"] = False
    assert "disabled" in asyncio.run(
        tool.execute(msg, url="https://e.com/x.png")
    ).lower()


def test_see_image_attaches_b64_from_download():
    cached = []

    async def _download(url, filename, max_size, message_id, **kwargs):
        return {
            "b64": base64.b64encode(_PNG_1X1).decode("ascii"),
            "mime_type": "image/png",
            "filename": "see-image.png",
            "is_image": True,
            "url": url,
        }

    bot = SimpleNamespace(
        _control={"process_images": True},
        _max_media_bytes=lambda: 1024 * 1024,
        _download_embed_media=_download,
        _cache_media_context=lambda channel_id, media: cached.append((channel_id, media)),
    )
    tool = SeeImageTool(bot)
    msg = SimpleNamespace(id=7, channel=SimpleNamespace(id=99))
    result = asyncio.run(tool.execute(msg, url="https://e.com/x.png"))
    assert "__IMAGE_B64__" in result
    assert "__END_IMAGE_B64__" in result
    assert "visual inspection" in result
    assert cached and cached[0][0] == "99"


def test_result_from_blob_skips_non_images():
    tool = SeeImageTool(SimpleNamespace(_control={"process_images": True}))
    result = asyncio.run(
        tool.result_from_blob(b"not-an-image", "application/pdf", "https://e.com/a.pdf")
    )
    assert result.startswith("Error:")


def test_fetch_url_defers_visual_links_to_see_image():
    async def _download(url, filename, max_size, message_id, **kwargs):
        return {
            "b64": "QUFB",
            "mime_type": "image/jpeg",
            "filename": "gif-sheet.jpg",
            "is_image": True,
            "url": url,
        }

    bot = SimpleNamespace(
        _control={"process_images": True},
        _max_media_bytes=lambda: 1024 * 1024,
        _download_embed_media=_download,
        _cache_media_context=lambda *a, **k: None,
        mark_message_tainted=lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("visual fetch must not taint")
        ),
    )
    tool = FetchUrlTool(bot)
    msg = SimpleNamespace(id=1, channel=SimpleNamespace(id=2))
    result = asyncio.run(
        tool.execute(msg, url="https://tenor.com/view/cat-dancing-gif-1")
    )
    assert "__IMAGE_B64__" in result
    assert "QUFB" in result


def test_reply_to_tenor_link_counts_as_media():
    parent = SimpleNamespace(
        id=55,
        attachments=None,
        stickers=None,
        embeds=None,
        content="https://tenor.com/view/cat-dancing-gif-123",
    )
    message = SimpleNamespace(reference=SimpleNamespace(resolved=parent))
    assert MaxwellBot._reply_media_message_id(message) == 55
