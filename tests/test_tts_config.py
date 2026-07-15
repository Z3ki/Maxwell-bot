import asyncio
import sys
from types import ModuleType, SimpleNamespace

from bot_tools import TtsTool, _tts_language_key, _tts_riva_voice_config


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
        result = await TtsTool(SimpleNamespace(config=SimpleNamespace(NVIDIA_API_KEY=""))).execute(
            message,
            text="hola mundo",
            language="spanish",
        )
        assert result == "__TTS_SENT__"

    asyncio.run(run())

    assert calls == [("hola mundo", "es")]
    assert sent == ["tts_123.ogg"]
