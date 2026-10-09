"""Mistral Voxtral TTS replaces Fish Audio."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from maxwell_core.tools.results import _tool_results_need_followup
from plugins.tts_voice.impl import SpeechError, TtsTool
from plugins.tts_voice.voices import prepare_line, resolve_voice


def test_tts_success_stays_silent_and_errors_come_back():
    assert TtsTool.returns_result is False
    spoken = "Tool tts: __TTS_SENT__ Voice message sent (Paul - Angry)."
    failed = "Tool tts: Error: paul has no sarcasm voice. Use sad, neutral"
    cooldown = "Tool tts: Error: TTS on cooldown for this channel (~7s left)."
    assert _tool_results_need_followup([spoken]) is False
    assert _tool_results_need_followup([failed]) is True
    assert _tool_results_need_followup([cooldown]) is True
    assert _tool_results_need_followup([spoken, failed]) is True
    assert _tool_results_need_followup([spoken, "Tool shell: done"]) is True


def test_default_voice_is_paul_neutral():
    voice = resolve_voice()
    assert voice.slug == "en_paul_neutral"
    assert voice.speaker == "paul"
    assert voice.emotion == "neutral"


def test_emotion_selects_pauls_preset():
    voice = resolve_voice(emotion="happy")
    assert voice.slug == "en_paul_happy"
    assert voice.voice_id == "1024d823-a11e-43ee-bf3d-d440dccc0577"


def test_sarcasm_uses_jane_because_paul_has_no_sarcastic_preset():
    voice = resolve_voice(emotion="sarcastic")
    assert voice.slug == "gb_jane_sarcasm"


def test_named_speaker_keeps_the_emotion_on_that_speaker():
    voice = resolve_voice(voice="marie", emotion="angry", language="english")
    assert voice.slug == "fr_marie_angry"


def test_paul_cannot_borrow_janes_sarcasm():
    with pytest.raises(ValueError, match="paul has no sarcasm"):
        resolve_voice(voice="paul", emotion="sarcastic")


def test_french_hint_uses_marie():
    assert resolve_voice(language="french").slug == "fr_marie_neutral"


def test_british_female_uses_jane():
    voice = resolve_voice(voice="female", language="british")
    assert voice.slug == "gb_jane_neutral"


def test_fish_voice_names_are_rejected():
    with pytest.raises(ValueError, match="Fish Audio"):
        resolve_voice(voice="tiktok")


def test_slug_plus_emotion_switches_feeling_on_the_same_speaker():
    voice = resolve_voice(voice="en_paul_neutral", emotion="angry")
    assert voice.slug == "en_paul_angry"


def test_prepare_line_strips_a_fish_style_emotion_tag():
    spoken, emotion = prepare_line("**[happy]** Hey, this is Maxwell! :)")
    assert emotion == "happy"
    assert spoken == "Hey, this is Maxwell! :)"
    assert "*" not in spoken


def test_tts_sends_the_emotion_voice_and_marks_delivery(monkeypatch):
    seen = {}

    def fake_synth(text, voice_id, api_key, model, ref_audio=""):
        seen["text"] = text
        seen["voice_id"] = voice_id
        seen["model"] = model
        seen["ref_audio"] = ref_audio
        assert api_key == "test-key"
        return b"mp3"

    async def fake_deliver(bot, message, audio):
        seen["audio"] = audio
        return SimpleNamespace(id=42)

    monkeypatch.setattr("plugins.tts_voice.impl.synthesize_speech", fake_synth)
    monkeypatch.setattr("plugins.tts_voice.impl.deliver_speech", fake_deliver)
    monkeypatch.setenv("MISTRAL_API_KEY", "test-key")
    TtsTool._last_tts.clear()
    tool = TtsTool(
        SimpleNamespace(
            config=None, _record_delivery=lambda *_a: seen.setdefault("recorded", True)
        )
    )
    message = SimpleNamespace(channel=SimpleNamespace(id=7))

    result = asyncio.run(
        tool.execute(message, text="[sad] I missed you.", voice="paul")
    )

    assert result == "__TTS_SENT__ Voice message sent (Paul - Sad)."
    assert seen["text"] == "I missed you."
    assert seen["voice_id"] == "530e2e20-58e2-45d8-b0a5-4594f4915944"
    assert seen["model"] == "voxtral-mini-tts-2603"
    assert seen["ref_audio"] == ""
    assert seen["audio"] == b"mp3"
    assert seen["recorded"] is True
    TtsTool._last_tts.clear()


def test_tts_speaks_a_line_past_the_old_length_cap(monkeypatch):
    seen = {}

    def fake_synth(text, voice_id, api_key, model, ref_audio=""):
        del voice_id, model, ref_audio
        seen["text"] = text
        assert api_key == "test-key"
        return b"mp3"

    async def fake_deliver(bot, message, audio):
        del bot, message
        seen["audio"] = audio
        return SimpleNamespace(id=9)

    monkeypatch.setattr("plugins.tts_voice.impl.synthesize_speech", fake_synth)
    monkeypatch.setattr("plugins.tts_voice.impl.deliver_speech", fake_deliver)
    monkeypatch.setenv("MISTRAL_API_KEY", "test-key")
    TtsTool._last_tts.clear()
    tool = TtsTool(SimpleNamespace(config=None))
    message = SimpleNamespace(channel=SimpleNamespace(id=77))
    long_line = " ".join(["word"] * 400)

    result = asyncio.run(tool.execute(message, text=long_line))

    assert len(long_line) > 1800
    assert result.startswith("__TTS_SENT__")
    assert seen["text"] == long_line
    assert seen["audio"] == b"mp3"
    TtsTool._last_tts.clear()


def test_tts_cooldown_and_missing_key(monkeypatch):
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    TtsTool._last_tts.clear()
    tool = TtsTool(SimpleNamespace(config=SimpleNamespace(MISTRAL_API_KEY="")))
    message = SimpleNamespace(channel=SimpleNamespace(id=9))
    assert (
        asyncio.run(tool.execute(message, text="hello"))
        == "Error: Mistral TTS is not configured"
    )

    monkeypatch.setenv("MISTRAL_API_KEY", "test-key")

    def fake_synth(*_args):
        return b"mp3"

    async def fake_deliver(*_args):
        return SimpleNamespace(id=1)

    monkeypatch.setattr("plugins.tts_voice.impl.synthesize_speech", fake_synth)
    monkeypatch.setattr("plugins.tts_voice.impl.deliver_speech", fake_deliver)
    first = asyncio.run(tool.execute(message, text="hello"))
    second = asyncio.run(tool.execute(message, text="hello again"))
    assert first.startswith("__TTS_SENT__")
    assert second.startswith("Error: TTS on cooldown")
    TtsTool._last_tts.clear()


def test_synthesis_error_is_safe_and_does_not_start_cooldown(monkeypatch):
    def fake_synth(*_args):
        raise SpeechError(403, "Mistral refused that line")

    monkeypatch.setattr("plugins.tts_voice.impl.synthesize_speech", fake_synth)
    monkeypatch.setenv("MISTRAL_API_KEY", "test-key")
    TtsTool._last_tts.clear()
    tool = TtsTool(SimpleNamespace(config=None))
    message = SimpleNamespace(channel=SimpleNamespace(id=11))
    result = asyncio.run(tool.execute(message, text="nope", emotion="angry"))
    assert result == "Error: Mistral refused that line"
    assert "11" not in TtsTool._last_tts


def _clip(name: str = "clip.mp3", url: str | None = None):
    return SimpleNamespace(
        filename=name,
        content_type="audio/mpeg",
        url=url or f"https://cdn.discordapp.com/attachments/1/2/{name}",
    )


def _bot(tmp_path, **config):
    fields = {"DATA_DIR": str(tmp_path), "MISTRAL_API_KEY": "test-key"}
    fields.update(config)
    return SimpleNamespace(config=SimpleNamespace(**fields))


def test_a_saved_clone_name_is_not_reused(tmp_path, monkeypatch):
    store = tmp_path / "plugins" / "tts_voice"
    store.mkdir(parents=True)
    (store / "clones.json").write_text(
        '{"voices":[{"name":"zeke","voice_id":"aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"}]}',
        encoding="utf-8",
    )

    def fake_synth(*_args, **_kwargs):
        raise AssertionError("a saved clone must not be spoken")

    monkeypatch.setattr("plugins.tts_voice.impl.synthesize_speech", fake_synth)
    monkeypatch.setenv("MISTRAL_API_KEY", "test-key")
    TtsTool._last_tts.clear()
    tool = TtsTool(_bot(tmp_path))
    message = SimpleNamespace(channel=SimpleNamespace(id=21), attachments=[])
    result = asyncio.run(tool.execute(message, text="hello there", voice="zeke"))
    assert result.startswith("Error:")
    assert "unknown voice" in result
    TtsTool._last_tts.clear()


def test_reference_clones_one_attachment_and_does_not_save_it(tmp_path, monkeypatch):
    seen = {}

    def fake_download(url):
        seen["url"] = url
        return b"raw-audio"

    async def fake_prepare(audio):
        assert audio == b"raw-audio"
        return b"ID3sample"

    def fake_synth(text, voice_id, api_key, model, ref_audio=""):
        seen["voice_id"] = voice_id
        seen["ref_audio"] = ref_audio
        return b"mp3"

    async def fake_deliver(*_args):
        return SimpleNamespace(id=4)

    monkeypatch.setattr("plugins.tts_voice.impl.download_cdn_audio", fake_download)
    monkeypatch.setattr("plugins.tts_voice.impl.prepare_sample", fake_prepare)
    monkeypatch.setattr("plugins.tts_voice.impl.synthesize_speech", fake_synth)
    monkeypatch.setattr("plugins.tts_voice.impl.deliver_speech", fake_deliver)
    monkeypatch.setenv("MISTRAL_API_KEY", "test-key")
    TtsTool._last_tts.clear()
    tool = TtsTool(_bot(tmp_path))
    message = SimpleNamespace(
        channel=SimpleNamespace(id=22),
        attachments=[_clip()],
        reference=None,
    )
    result = asyncio.run(
        tool.execute(message, text="hello there", voice="paul", reference="attachment")
    )
    assert result == "__TTS_SENT__ Voice message sent (reference)."
    assert seen["voice_id"] == ""
    assert seen["ref_audio"]
    assert seen["url"].endswith("/clip.mp3")
    assert not (tmp_path / "plugins" / "tts_voice").exists()
    TtsTool._last_tts.clear()


@pytest.mark.parametrize("parameter", ["reference", "reference_audio_url"])
def test_reference_accepts_url_without_attachment(tmp_path, monkeypatch, parameter):
    seen = {}
    async def download(url):
        seen['url'] = url
        return b'raw-audio'
    async def prepare(raw):
        assert raw == b'raw-audio'
        return b'ID3sample'
    def synth(text, voice_id, api_key, model, ref_audio=''):
        seen['ref_audio'] = ref_audio
        assert not voice_id
        return b'mp3'
    async def deliver(*_args):
        return SimpleNamespace(id=4)
    monkeypatch.setattr('plugins.tts_voice.impl.download_reference_audio', download)
    monkeypatch.setattr('plugins.tts_voice.impl.prepare_sample', prepare)
    monkeypatch.setattr('plugins.tts_voice.impl.synthesize_speech', synth)
    monkeypatch.setattr('plugins.tts_voice.impl.deliver_speech', deliver)
    TtsTool._last_tts.clear()
    message = SimpleNamespace(channel=SimpleNamespace(id=23), attachments=[])
    result = asyncio.run(TtsTool(_bot(tmp_path)).execute(message, text='hello', **{parameter: 'https://example.com/voice.wav'}))
    assert result.startswith('__TTS_SENT__')
    assert seen['url'] == 'https://example.com/voice.wav' and seen['ref_audio']
    assert not (tmp_path / 'plugins' / 'tts_voice').exists()
    TtsTool._last_tts.clear()


def test_download_rejects_clips_that_are_not_discord_attachments():
    from plugins.tts_voice.clones import download_cdn_audio

    with pytest.raises(ValueError, match="attachments"):
        download_cdn_audio("https://example.com/clip.mp3")
    with pytest.raises(ValueError, match="attachments"):
        download_cdn_audio("https://user:pass@cdn.discordapp.com/clip.mp3")


def test_voice_upload_uses_the_attachment_slot_without_channel_state(monkeypatch):
    from plugins.tts_voice.impl import _send_voice_message

    calls = []

    class HTTP:
        async def request(self, route, **kwargs):
            calls.append((route.method, route.path, kwargs.get("json")))
            if route.path.endswith("/attachments"):
                return {
                    "attachments": [
                        {
                            "upload_url": "https://cdn.discordapp.com/upload/slot",
                            "upload_filename": "stored.ogg",
                        }
                    ]
                }
            return {
                "id": "99",
                "attachments": [{"filename": "voice-message.ogg", "size": 12}],
            }

    def fake_put(url, payload):
        calls.append(("PUT", url, payload))

    monkeypatch.setattr("plugins.tts_voice.impl._put_upload", fake_put)
    ogg = b"OggS" + b"x" * 8
    sent = asyncio.run(
        _send_voice_message(
            SimpleNamespace(http=HTTP()),
            SimpleNamespace(channel=SimpleNamespace(id="55")),
            ogg,
            1.25,
            "waveform",
        )
    )

    assert sent["id"] == "99"
    assert calls[0][0] == "POST"
    assert calls[0][1].endswith("/attachments")
    assert calls[0][2]["files"][0]["file_size"] == len(ogg)
    assert calls[1] == ("PUT", "https://cdn.discordapp.com/upload/slot", ogg)
    posted = calls[2][2]
    assert posted["flags"] == 8192
    assert posted["attachments"][0]["uploaded_filename"] == "stored.ogg"
    assert posted["attachments"][0]["duration_secs"] == 1.25
    assert posted["attachments"][0]["waveform"] == "waveform"


def test_live_channel_http_is_used_before_the_bot_client(monkeypatch):
    from plugins.tts_voice.impl import _send_voice_message

    seen = {}

    class HTTP:
        def __init__(self, name):
            self.name = name

        async def request(self, route, **kwargs):
            seen["client"] = self.name
            if route.path.endswith("/attachments"):
                return {
                    "attachments": [
                        {
                            "upload_url": "https://cdn.discordapp.com/upload/slot",
                            "upload_filename": "stored.ogg",
                        }
                    ]
                }
            return {"id": "5", "attachments": [{"size": 8}]}

    monkeypatch.setattr("plugins.tts_voice.impl._put_upload", lambda *_args: None)
    message = SimpleNamespace(
        channel=SimpleNamespace(id=5, _state=SimpleNamespace(http=HTTP("channel")))
    )
    asyncio.run(
        _send_voice_message(
            SimpleNamespace(http=HTTP("bot")),
            message,
            b"OggSxxxx",
            1.0,
            "wave",
        )
    )
    assert seen["client"] == "channel"


def test_zero_byte_voice_message_is_deleted_and_rejected(monkeypatch):
    from plugins.tts_voice.impl import _send_voice_message

    deleted = []

    class HTTP:
        async def request(self, route, **_kwargs):
            if route.method == "DELETE":
                deleted.append(route.path)
                return None
            if route.path.endswith("/attachments"):
                return {
                    "attachments": [
                        {
                            "upload_url": "https://cdn.discordapp.com/upload/slot",
                            "upload_filename": "stored.ogg",
                        }
                    ]
                }
            return {"id": "7", "attachments": [{"size": 0}]}

    monkeypatch.setattr("plugins.tts_voice.impl._put_upload", lambda *_args: None)
    with pytest.raises(RuntimeError, match="0-byte"):
        asyncio.run(
            _send_voice_message(
                SimpleNamespace(http=HTTP()),
                SimpleNamespace(channel=SimpleNamespace(id=3)),
                b"OggSxxxx",
                1.0,
                "wave",
            )
        )
    assert any(path.endswith("/messages/{message_id}") for path in deleted)


def test_failed_voice_upload_sends_the_original_mp3(monkeypatch):
    from plugins.tts_voice.impl import deliver_speech

    async def transcode(_mp3):
        return b"OggSxxxx", 1.0, "wave"

    async def send_voice(*_args, **_kwargs):
        raise RuntimeError("voice message transport unavailable")

    captured = {}

    async def deliver(_message, file, *, label):
        assert label == "voice"
        captured["name"] = file.filename
        captured["bytes"] = file.fp.read()
        return SimpleNamespace(id=4, attachments=[]), None

    monkeypatch.setattr("plugins.tts_voice.impl.transcode_voice", transcode)
    monkeypatch.setattr("plugins.tts_voice.impl._send_voice_message", send_voice)
    monkeypatch.setattr("tooling.helpers.deliver_attachment", deliver)
    audio = b"ID3hello"
    sent = asyncio.run(deliver_speech(SimpleNamespace(), SimpleNamespace(), audio))
    assert sent.id == 4
    assert captured["name"] == "voice.mp3"
    assert captured["bytes"] == audio


def test_empty_audio_is_not_sent():
    from plugins.tts_voice.impl import deliver_speech

    with pytest.raises(RuntimeError, match="no audio"):
        asyncio.run(deliver_speech(SimpleNamespace(), SimpleNamespace(), b""))


def test_upload_url_must_be_public_https():
    from plugins.tts_voice.impl import _put_upload

    with pytest.raises(RuntimeError, match="rejected"):
        _put_upload("http://cdn.discordapp.com/up", b"OggS")
    with pytest.raises(RuntimeError, match="rejected"):
        _put_upload("https://127.0.0.1/up", b"OggS")
    with pytest.raises(RuntimeError, match="rejected"):
        _put_upload("https://localhost/up", b"OggS")
    with pytest.raises(RuntimeError, match="empty"):
        _put_upload("https://cdn.discordapp.com/up", b"")


def test_prepare_sample_keeps_short_and_full_length_clips():
    import shutil
    import subprocess

    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg is not installed")

    from plugins.tts_voice.clones import prepare_sample

    def sine(seconds: float) -> bytes:
        completed = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=" + str(seconds),
                "-f",
                "mp3",
                "-",
            ],
            check=True,
            capture_output=True,
        )
        return completed.stdout

    assert asyncio.run(prepare_sample(sine(0.4)))
    sample = asyncio.run(prepare_sample(sine(3)))
    assert sample.startswith(b"ID3") or sample[0] == 0xFF

    long_sample = asyncio.run(prepare_sample(sine(23)))
    _duration = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', '-i', 'pipe:0'], input=long_sample, capture_output=True)
    # Pipe MP3 duration can be N/A; measure decoded PCM samples instead.
    decoded = subprocess.run(['ffmpeg', '-v', 'error', '-i', 'pipe:0', '-f', 's16le', '-ac', '1', '-ar', '24000', 'pipe:1'], input=long_sample, capture_output=True, check=True)
    assert len(decoded.stdout) / (24000 * 2) > 22.5
