"""image_generator: Pollinations is the primary (and only) generator."""

import asyncio
from types import SimpleNamespace

from bot_tools import ImageGeneratorTool


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


class _Posted:
    def __init__(self):
        self.attachments = [SimpleNamespace(url="https://cdn.discordapp.com/gen.png")]


class _Channel:
    def __init__(self):
        self.id = 42
        self.files = []

    async def send(self, content=None, file=None, **kwargs):
        if file is not None:
            self.files.append(file)
        return _Posted()


class _Message:
    def __init__(self):
        self.channel = _Channel()
        self.author = SimpleNamespace(id=7)


class _Memory:
    async def add_to_channel_memory(self, *args, **kwargs):
        return None


def _tool(*, nvidia_key="nv-test"):
    bot = SimpleNamespace(
        config=SimpleNamespace(
            NVIDIA_API_KEY=nvidia_key,
            NVIDIA_IMAGE_URL="https://example.invalid/nvidia",
            POLLINATIONS_MODEL="flux",
        ),
        memory=_Memory(),
        _current_progress_by_channel={},
    )
    return ImageGeneratorTool(bot)


def test_execute_always_uses_pollinations(monkeypatch):
    """execute() calls Pollinations directly; NVIDIA is never attempted."""
    tool = _tool()
    message = _Message()
    calls = []

    async def nvidia_should_not_run(self, message, prompt):
        calls.append("nvidia")
        raise AssertionError("NVIDIA must never be called (route removed)")

    async def pollinations_ok(self, message, prompt):
        calls.append("pollinations")
        return "Image sent to chat: a red fox"

    # Even if a stale NVIDIA method were present, execute() must not call it.
    monkeypatch.setattr(
        ImageGeneratorTool, "_nvidia_generate", nvidia_should_not_run, raising=False
    )
    monkeypatch.setattr(ImageGeneratorTool, "_pollinations_generate", pollinations_ok)

    result = asyncio.run(tool.execute(message, prompt="a red fox"))
    assert calls == ["pollinations"]
    assert result.startswith("Image sent to chat")


def test_execute_does_not_require_nvidia_key(monkeypatch):
    """Pollinations is keyless; execute() works with no NVIDIA key."""
    tool = _tool(nvidia_key="")
    message = _Message()
    calls = []

    async def pollinations_ok(self, message, prompt):
        calls.append("pollinations")
        return "Image sent to chat: a cat"

    monkeypatch.setattr(ImageGeneratorTool, "_pollinations_generate", pollinations_ok)

    result = asyncio.run(tool.execute(message, prompt="a cat"))
    assert calls == ["pollinations"]
    assert "Image sent to chat" in result


def test_pollinations_posts_image_bytes(monkeypatch):
    tool = _tool()
    message = _Message()

    class _Resp:
        status = 200
        headers = {"Content-Type": "image/png"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return None

        async def text(self):
            return ""

    class _Session:
        def get(self, url, **kwargs):
            assert "image.pollinations.ai/prompt/" in url
            return _Resp()

    async def fake_session():
        return _Session()

    async def fake_limited(_resp, _max_bytes):
        return PNG

    monkeypatch.setattr("bot_tools._get_shared_session", fake_session)
    monkeypatch.setattr("bot_tools._read_response_limited", fake_limited)
    monkeypatch.setattr(
        "bot_tools._persist_public_image",
        lambda *_a, **_k: ("/tmp/x.png", "https://example.com/x.png"),
    )

    result = asyncio.run(tool._pollinations_generate(message, "a red fox"))
    assert message.channel.files
    assert "Image sent to chat" in result
    assert "https://cdn.discordapp.com/gen.png" in result
