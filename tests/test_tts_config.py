from bot_tools import _tts_language_key, _tts_riva_voice_config


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
