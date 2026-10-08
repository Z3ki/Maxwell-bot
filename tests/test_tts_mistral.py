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


def test_reference_refuses_a_url_that_is_not_attached(tmp_path, monkeypatch):
    def refuse(_url):
        raise AssertionError("fetched a url that was not attached")

    monkeypatch.setattr("plugins.tts_voice.impl.download_cdn_audio", refuse)
    monkeypatch.setenv("MISTRAL_API_KEY", "test-key")
    TtsTool._last_tts.clear()
    tool = TtsTool(_bot(tmp_path))
    message = SimpleNamespace(
        channel=SimpleNamespace(id=23),
        attachments=[],
        reference=None,
    )
    result = asyncio.run(
        tool.execute(
            message,
            text="hello there",
            reference="https://cdn.discordapp.com/attachments/9/9/nope.mp3",
        )
    )
    assert result.startswith("Error: attach a short voice clip")
    assert not (tmp_path / "plugins" / "tts_voice").exists()
    TtsTool._last_tts.clear()


def test_download_rejects_clips_that_are_not_discord_attachments():
    from plugins.tts_voice.clones import download_cdn_audio

    with pytest.raises(ValueError, match="attachments"):
        download_cdn_audio("https://example.com/clip.mp3")
    with pytest.raises(ValueError, match="attachments"):
        download_cdn_audio("https://user:pass@cdn.discordapp.com/clip.mp3")


def test_prepare_sample_rejects_a_short_clip_and_trims_a_long_one():
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

    with pytest.raises(ValueError, match="too short"):
        asyncio.run(prepare_sample(sine(0.4)))
    sample = asyncio.run(prepare_sample(sine(3)))
    assert sample.startswith(b"ID3") or sample[0] == 0xFF
