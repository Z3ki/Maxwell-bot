"""Generated images go to model inspection; delivery requires an explicit tool."""

import asyncio
import base64
import json
import re
from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image

from bot_tools import HDImageGeneratorTool, ImageGeneratorTool
from bot_tools import SendFileTool
from generated_artifacts import begin_generated_files, reset_generated_files
from plugins.images.impl import _generated_image_result
from tooling.helpers import _public_files_target, _public_image_target


class _Response:
    status = 200

    def __init__(self, image_bytes, mime):
        self.image_bytes = image_bytes
        self.mime = mime
        self.headers = {"Content-Type": mime}
        self.content = SimpleNamespace(iter_chunked=self._chunks)

    async def _chunks(self, size):
        yield self.image_bytes

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        pass

    async def text(self):
        image = f"data:{self.mime};base64," + base64.b64encode(self.image_bytes).decode()
        return json.dumps({"choices": [{"message": {"content": image}}]})


def _generation(tmp_path, monkeypatch, hd, image_bytes, mime, *, attachments=None, image=None):
    response = _Response(image_bytes, mime)
    calls = []

    def get(*_args, **_kwargs):
        raise AssertionError("image generation must not GET an image host")

    def post(*args, **kwargs):
        calls.append(args[0] if args else kwargs.get("url"))
        return response

    async def session():
        return SimpleNamespace(get=get, post=post)

    monkeypatch.setattr("plugins.images.impl._get_shared_session", session)
    sends = []

    async def send(**kwargs):
        sends.append(kwargs)
        raise AssertionError("Generation must not upload to Discord")

    def record_delivery(*_args):
        raise AssertionError("Generation must not record a delivery")

    bot = SimpleNamespace(
        config=SimpleNamespace(
            AI_BASE_URL="https://example.invalid/v1",
            GEMINI_IMAGE_MODEL="gemini-3.1-flash-image",
            MAXWELL_SITE_DIR=str(tmp_path),
            MAXWELL_PUBLIC_BASE_URL="https://example.com",
        ),
        _record_delivery=record_delivery,
    )
    message = SimpleNamespace(
        channel=SimpleNamespace(id=42, send=send),
        attachments=attachments or [],
    )
    tool = HDImageGeneratorTool(bot) if hd else ImageGeneratorTool(bot)
    result = asyncio.run(tool.execute(message, prompt="a red fox", image=image))
    assert sends == []
    assert calls == ["https://example.invalid/v1/chat/completions"]
    assert "pollinations" not in result
    payload = re.search(r"__IMAGE_B64__data:([^;]+);base64,(.*?)__END_IMAGE_B64__", result)
    assert payload is not None, result
    persisted, = (tmp_path / "_images").iterdir()
    assert persisted.read_bytes() == image_bytes
    assert persisted.name.startswith("hd" if hd else "image")
    assert f"https://example.com/bot/_images/{persisted.name}" in result
    return payload.group(1), payload.group(2), persisted


@pytest.mark.parametrize("hd", [False, True], ids=["image", "hd"])
@pytest.mark.parametrize("format,mime,ext", [
    ("PNG", "image/png", ".png"),
    ("JPEG", "image/jpeg", ".jpg"),
    ("WEBP", "image/webp", ".webp"),
])
def test_generation_returns_model_image_without_discord_delivery(tmp_path, monkeypatch, hd, format, mime, ext):
    buffer = BytesIO()
    Image.new("RGB", (2, 2), "red").save(buffer, format=format)
    image_bytes = buffer.getvalue()
    returned_mime, payload, persisted = _generation(tmp_path, monkeypatch, hd, image_bytes, mime)
    assert returned_mime == mime
    assert base64.b64decode(payload) == image_bytes
    assert persisted.suffix == ext


@pytest.mark.parametrize("hd", [False, True], ids=["image", "hd"])
def test_oversized_generation_preserves_full_file_and_attaches_bounded_preview(tmp_path, monkeypatch, hd):
    buffer = BytesIO()
    Image.new("RGB", (1600, 1200), "red").save(buffer, format="PNG")
    original = buffer.getvalue()
    # PNG readers permit trailing data; exercise the exact inline size boundary.
    image_bytes = original + b"\0" * (3_750_000 - len(original))
    mime, payload, _persisted = _generation(tmp_path, monkeypatch, hd, image_bytes, "image/png")
    assert mime == "image/jpeg"
    assert len(payload) < 5_000_000
    with Image.open(BytesIO(base64.b64decode(payload))) as preview:
        assert preview.size == (1024, 768)


@pytest.mark.parametrize("tool_class", [ImageGeneratorTool, HDImageGeneratorTool])
def test_generation_requires_a_prompt(tool_class):
    tool = tool_class(SimpleNamespace())
    result = asyncio.run(tool.execute(SimpleNamespace()))
    assert result.startswith("Error:")


def test_image_generator_ignores_input_images(tmp_path, monkeypatch):
    buffer = BytesIO()
    Image.new("RGB", (2, 2), "red").save(buffer, format="PNG")
    attachment = SimpleNamespace(
        content_type="image/png",
        filename="photo.png",
        url="https://cdn.discordapp.com/attachments/1/2/photo.png",
    )
    _generation(
        tmp_path,
        monkeypatch,
        False,
        buffer.getvalue(),
        "image/png",
        attachments=[attachment],
        image="https://example.com/reference.png",
    )


def test_generated_image_can_be_delivered_without_public_hosting_or_shell(tmp_path):
    async def scenario():
        bot = SimpleNamespace(config=SimpleNamespace(
            MAXWELL_PUBLIC_BASE_URL="", MAXWELL_SITE_DIR=str(tmp_path), ENABLE_CREATE_SITE=False,
        ), tools={})
        image_bytes = b"\x89PNG\r\n\x1a\n" + b"generated-image-fixture"
        sent = []

        async def send(**kwargs):
            file = kwargs["file"]
            sent.append((file.filename, file.fp.read()))
            return SimpleNamespace(attachments=[])

        message = SimpleNamespace(channel=SimpleNamespace(id=1, send=send), reply=send)
        token = begin_generated_files()
        try:
            result = _generated_image_result(bot, image_bytes, prefix="img", summary="generated")
            assert "example.com" not in result
            assert "create_site" not in result
            path = re.search(r"Attachment path: (\S+)", result).group(1)
            delivered = await SendFileTool(bot).execute(message, path=path)
            assert delivered.startswith("__FILE_SENT__"), delivered
            assert sent == [("img.png", image_bytes)]
            # A separate turn cannot access the same generated attachment.
            other = begin_generated_files()
            try:
                assert (await SendFileTool(bot).execute(message, path=path)).startswith("Error:")
            finally:
                reset_generated_files(other)
        finally:
            reset_generated_files(token)
        assert (await SendFileTool(bot).execute(message, path=path)).startswith("Error:")
        assert _public_image_target(bot)[1] == _public_files_target(bot)[1] == ""

    asyncio.run(scenario())
