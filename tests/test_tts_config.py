import asyncio
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

from bot_tools import (
    FISH_REFERENCE_DEFAULT,
    TtsTool,
    _fish_reference_id,
    _tts_language_key,
    _tts_riva_voice_config,
)


def test_fish_reference_id_resolves_named_voices(monkeypatch):
    monkeypatch.setenv("TTS_FISH_REFERENCE_ID_TIKTOK", "id-tiktok")
    monkeypatch.setenv("TTS_FISH_REFERENCE_ID_MOMMY", "id-mommy")
    monkeypatch.setenv("TTS_FISH_REFERENCE_ID_ESPANOL", "id-espanol")
    monkeypatch.setenv("TTS_FISH_REFERENCE_ID", "id-default")

    assert _fish_reference_id("tiktok") == "id-tiktok"
    assert _fish_reference_id("TikTok") == "id-tiktok"  # case-insensitive
    assert _fish_reference_id("mommy") == "id-mommy"
    assert _fish_reference_id("espanol") == "id-espanol"
    assert _fish_reference_id("español") == "id-espanol"
    assert _fish_reference_id("spanish") == "id-espanol"
    # Unknown/empty voice falls back to the legacy default.
    assert _fish_reference_id("britney") == "id-default"
    assert _fish_reference_id("") == "id-default"
    assert _fish_reference_id(None) == "id-default"


def test_fish_reference_id_falls_back_to_hardcoded_default(monkeypatch):
    monkeypatch.delenv("TTS_FISH_REFERENCE_ID_TIKTOK", raising=False)
    monkeypatch.delenv("TTS_FISH_REFERENCE_ID_MOMMY", raising=False)
    monkeypatch.delenv("TTS_FISH_REFERENCE_ID_ESPANOL", raising=False)
    monkeypatch.delenv("TTS_FISH_REFERENCE_ID", raising=False)

    assert _fish_reference_id("tiktok") == FISH_REFERENCE_DEFAULT
    assert _fish_reference_id(None) == FISH_REFERENCE_DEFAULT


def test_tts_language_key_accepts_spanish_aliases():
    assert _tts_language_key(language="spanish") == "spanish"
    assert _tts_language_key(lang="es") == "spanish"
    assert _tts_language_key(language="es-ES") == "spanish"


def test_tts_language_key_defaults_to_english():
    assert _tts_language_key() == "english"
    assert _tts_language_key(language="unknown") == "english"


def test_tts_spanish_riva_default_matches_available_nvidia_voice(monkeypatch):
    monkeypatch.delenv("TTS_RIVA_VOICE_ES", raising=False)
    monkeypatch.delenv("TTS_RIVA_LANGUAGE_ES", raising=False)

    assert _tts_riva_voice_config("spanish") == (
        "Magpie-Multilingual.ES-US.Jason.Angry",
        "es-US",
    )


def test_tts_english_riva_default_unchanged(monkeypatch):
    monkeypatch.delenv("TTS_RIVA_VOICE", raising=False)
    monkeypatch.delenv("TTS_RIVA_LANGUAGE", raising=False)

    assert _tts_riva_voice_config("english") == (
        "Magpie-Multilingual.EN-US.Jason.Angry",
        "en-US",
    )


def test_tts_spanish_falls_back_to_gtts_without_nvidia_key(monkeypatch, tmp_path):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    # Fish is now the highest-priority provider; unset its key too so the
    # test exercises the gTTS fallback (matches the test's intent).
    monkeypatch.delenv("FISH_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    calls = []

    gtts_module = ModuleType("gtts")

    class FakeGTTS:
        def __init__(self, text, lang):
            calls.append((text, lang))

        def save(self, filename):
            (tmp_path / filename).write_bytes(b"fake audio")

    gtts_module.gTTS = FakeGTTS
    monkeypatch.setitem(sys.modules, "gtts", gtts_module)

    class FakeProc:
        def __init__(self, returncode=0, stdout=b""):
            self.returncode = returncode
            self._stdout = stdout

        async def communicate(self):
            return self._stdout, b""

    async def fake_create_subprocess_exec(*args, **kwargs):
        if args[0] == "ffprobe":
            return FakeProc(stdout=b"1.0")
        if args[0] == "ffmpeg" and args[-1] == "pipe:1":
            return FakeProc(stdout=(1).to_bytes(2, "little", signed=True) * 512)
        if args[0] == "ffmpeg":
            (tmp_path / args[-1]).write_bytes(b"fake ogg")
            return FakeProc()
        raise AssertionError(f"unexpected command: {args}")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    sent = []

    async def send_voice_file(path):
        sent.append(path)

    message = SimpleNamespace(
        id=123,
        send_voice_file=send_voice_file,
    )

    async def run():
        result = await TtsTool(
            SimpleNamespace(config=SimpleNamespace(NVIDIA_API_KEY=""))
        ).execute(
            message,
            text="hola mundo",
            language="spanish",
        )
        assert result == "__TTS_SENT__"

    asyncio.run(run())

    assert calls == [("hola mundo", "es")]
    assert len(sent) == 1
    assert sent[0].startswith("tts_") and sent[0].endswith(".ogg")


def test_fish_tts_writes_audio_on_success(monkeypatch, tmp_path):
    """Fish TTS must POST to fish.audio/v1/tts with the right shape and write
    the response bytes to output_path."""
    from bot_tools import _synthesize_fish_tts

    captured = {}

    class FakeResponse:
        status = 200

        async def read(self):
            return b"\xff\xfb" + b"X" * 200  # fake mp3 magic + payload

        async def text(self):
            return ""

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class FakeSession:
        def post(self, url, json=None, headers=None, timeout=None):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return FakeResponse()

    async def fake_get_session():
        return FakeSession()

    monkeypatch.setattr("bot_tools._get_shared_session", fake_get_session)

    out = tmp_path / "fish.mp3"

    async def run():
        result = await _synthesize_fish_tts(
            "hello fish",
            str(out),
            api_key="sk-fish-test",
            model="s2.1-pro-free",
            reference_id="abc123",
            fmt="mp3",
        )
        assert result == str(out)

    asyncio.run(run())

    assert captured["url"] == "https://api.fish.audio/v1/tts"
    assert captured["headers"]["Authorization"] == "Bearer sk-fish-test"
    assert captured["headers"]["model"] == "s2.1-pro-free"
    assert captured["json"]["text"] == "hello fish"
    assert captured["json"]["reference_id"] == "abc123"
    assert captured["json"]["format"] == "mp3"
    assert out.read_bytes().startswith(b"\xff\xfb")
    assert len(out.read_bytes()) == 202


def test_fish_tts_returns_none_on_api_error(monkeypatch):
    """API non-200 must return None so the provider chain can fall through."""
    from bot_tools import _synthesize_fish_tts

    class FakeResponse:
        status = 401

        async def read(self):
            return b""

        async def text(self):
            return "unauthorized"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class FakeSession:
        def post(self, *args, **kwargs):
            return FakeResponse()

    async def fake_get_session():
        return FakeSession()

    monkeypatch.setattr("bot_tools._get_shared_session", fake_get_session)

    async def run():
        result = await _synthesize_fish_tts(
            "x",
            "/tmp/should_not_exist.mp3",
            api_key="bad",
            model="s2.1-pro-free",
            reference_id="",
        )
        assert result is None

    asyncio.run(run())


def test_fish_tts_returns_none_when_key_missing():
    """No API key -> return None immediately, no network call."""
    from bot_tools import _synthesize_fish_tts

    async def run():
        result = await _synthesize_fish_tts(
            "x", "/tmp/x.mp3", api_key="", model="s2.1-pro-free", reference_id=""
        )
        assert result is None

    asyncio.run(run())


def test_tts_tool_prefers_fish_over_riva(monkeypatch, tmp_path):
    """When FISH_API_KEY is set, TtsTool must call Fish first and skip Riva."""
    from bot_tools import TtsTool

    fish_calls = []
    riva_called = {"count": 0}

    async def fake_fish(text, output_path, *, api_key, model, reference_id, fmt):
        fish_calls.append((text, output_path, model, reference_id))
        # Simulate writing a real audio file.
        (tmp_path / output_path).write_bytes(b"\xff\xfb" + b"X" * 200)
        return output_path

    def fake_riva(*args, **kwargs):
        riva_called["count"] += 1
        raise AssertionError("Riva should NOT be called when Fish succeeds")

    monkeypatch.setenv("FISH_API_KEY", "sk-fish-test")
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    monkeypatch.setattr("bot_tools._synthesize_fish_tts", fake_fish)

    # Stub ffmpeg/ffprobe subprocess calls the downstream pipeline runs.
    class FakeProc:
        def __init__(self, returncode=0, stdout=b""):
            self.returncode = returncode
            self.stdout = stdout

        async def communicate(self):
            return self.stdout, b""

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class FakeOggProc:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def fake_create_subprocess_exec(*args, **kwargs):
        if args[0] == "ffprobe":
            return FakeProc(stdout=b"1.0")
        if args[0] == "ffmpeg" and args[-1] == "pipe:1":
            return FakeProc(stdout=(1).to_bytes(2, "little", signed=True) * 512)
        if args[0] == "ffmpeg":
            # Write the OGG output file the rest of the code expects.
            Path(args[-1]).write_bytes(b"fake ogg")
            return FakeOggProc()
        raise AssertionError(f"unexpected command: {args}")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.chdir(tmp_path)

    sent = []

    async def send_voice_file(path):
        sent.append(path)

    message = SimpleNamespace(
        id=999,
        send_voice_file=send_voice_file,
    )

    async def run():
        result = await TtsTool(
            SimpleNamespace(
                config=SimpleNamespace(NVIDIA_API_KEY="", FISH_API_KEY="sk-fish-test")
            )
        ).execute(
            message,
            text="hello from fish",
            language="english",
        )
        assert result == "__TTS_SENT__"

    asyncio.run(run())

    assert len(fish_calls) == 1
    assert fish_calls[0][0] == "hello from fish"
    assert fish_calls[0][2] == "s2.1-pro-free"
    assert len(sent) == 1
    assert sent[0].startswith("tts_") and sent[0].endswith(".ogg")
