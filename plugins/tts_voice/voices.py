"""Mistral Voxtral preset voices.

Emotion is not a request field. Each feeling is its own preset voice
(Paul - Happy, Jane - Sarcasm, and so on). Fish Audio tags are not spoken.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.I,
)
_TAG = re.compile(r"[<\(\[]\s*([a-z][a-z \-]{0,24})\s*[>\)\]]", re.I)
_FENCE = re.compile(r"```.*?```", re.S)
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_MENTION = re.compile(r"<@[!&]?\d+>|<#\d+>|<a?:([a-zA-Z0-9_]+):\d+>")
_URL = re.compile(r"https?://\S+")
_EMOJI = re.compile(
    "[\U0001f300-\U0001faff\U00002600-\U000027bf\U0001f1e6-\U0001f1ff]+",
    flags=re.UNICODE,
)
_SPEAKER_ORDER = ("paul", "oliver", "jane", "marie")
_EMOTION_ALIASES = {
    "sad": "sad",
    "unhappy": "sad",
    "neutral": "neutral",
    "calm": "neutral",
    "flat": "neutral",
    "happy": "happy",
    "joy": "happy",
    "glad": "happy",
    "frustrated": "frustrated",
    "annoyed": "frustrated",
    "irritated": "frustrated",
    "excited": "excited",
    "hype": "excited",
    "hyped": "excited",
    "confident": "confident",
    "bold": "confident",
    "cheerful": "cheerful",
    "upbeat": "cheerful",
    "cheery": "cheerful",
    "angry": "angry",
    "anger": "angry",
    "mad": "angry",
    "sarcasm": "sarcasm",
    "sarcastic": "sarcasm",
    "wry": "sarcasm",
    "confused": "confused",
    "uncertain": "confused",
    "curious": "curious",
    "shameful": "shameful",
    "ashamed": "shameful",
    "shame": "shameful",
    "guilty": "shameful",
    "jealousy": "jealousy",
    "jealous": "jealousy",
}
_SPEAKERS = {
    "paul": "paul",
    "american": "paul",
    "us": "paul",
    "oliver": "oliver",
    "british": "oliver",
    "uk": "oliver",
    "gb": "oliver",
    "jane": "jane",
    "marie": "marie",
    "french": "marie",
}
_FISH = {"tiktok", "mommy", "fish", "fish audio"}
_FEMALE = {"female", "woman", "girl"}
_MALE = {"male", "man", "guy"}
_FRENCH = {"fr", "french", "francais", "fr fr", "fr_fr"}
_BRITISH = {"en gb", "en_gb", "british", "uk", "gb"}
_AMERICAN = {"en", "en us", "en_us", "english", "american", "us"}


@dataclass(frozen=True)
class Voice:
    slug: str
    voice_id: str
    speaker: str
    emotion: str
    gender: str
    language: str
    name: str


def load_voices(path: Path | None = None) -> tuple[Voice, ...]:
    raw = json.loads((path or Path(__file__).with_name("voices.json")).read_text())
    return tuple(
        Voice(
            slug=str(row["slug"]),
            voice_id=str(row["id"]),
            speaker=str(row["speaker"]),
            emotion=str(row["emotion"]),
            gender=str(row.get("gender") or ""),
            language=str(row.get("language") or ""),
            name=str(row.get("name") or row["slug"]),
        )
        for row in raw
    )


PRESETS = load_voices()


def _norm(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _catalog(voices: tuple[Voice, ...] | None) -> tuple[Voice, ...]:
    return voices if voices is not None else PRESETS


def known_emotions(voices: tuple[Voice, ...] | None = None) -> tuple[str, ...]:
    seen: list[str] = []
    for voice in _catalog(voices):
        if voice.emotion not in seen:
            seen.append(voice.emotion)
    return tuple(seen)


def _find(voices: tuple[Voice, ...], *, speaker: str, emotion: str) -> Voice | None:
    for voice in voices:
        if voice.speaker == speaker and voice.emotion == emotion:
            return voice
    return None


def _speaker_emotions(voices: tuple[Voice, ...], speaker: str) -> str:
    names = [voice.emotion for voice in voices if voice.speaker == speaker]
    return ", ".join(names) if names else "(none)"


def _language_speaker(language: str, gender: str | None) -> str | None:
    if language in _FRENCH:
        if gender == "male":
            return None
        return "marie"
    if language in _BRITISH:
        return "jane" if gender == "female" else "oliver"
    if language in _AMERICAN:
        return "jane" if gender == "female" else "paul"
    return None


def _default_speaker(
    voices: tuple[Voice, ...], emotion: str, gender: str | None
) -> str:
    order = list(_SPEAKER_ORDER)
    if gender == "female":
        order = ["jane", "marie", "paul", "oliver"]
    elif gender == "male":
        order = ["paul", "oliver", "jane", "marie"]
    for speaker in order:
        if _find(voices, speaker=speaker, emotion=emotion):
            return speaker
    raise ValueError(
        f"no preset uses emotion {emotion}. "
        f"Choose one of: {', '.join(known_emotions(voices))}"
    )


def resolve_voice(
    voice: object = "",
    emotion: object = "",
    language: object = "",
    voices: tuple[Voice, ...] | None = None,
) -> Voice:
    """Pick a preset. A bare emotion uses Paul when he has it, otherwise the closest speaker."""
    catalog = _catalog(voices)
    raw_voice = str(voice or "").strip()
    voice_key = _norm(raw_voice)
    emotion_key = _norm(emotion)
    language_key = _norm(language)

    if _UUID.fullmatch(raw_voice):
        found = next((item for item in catalog if item.voice_id == raw_voice), None)
        if found and emotion_key:
            canon = _EMOTION_ALIASES.get(emotion_key)
            if canon is None:
                raise ValueError(f"unknown emotion {emotion_key!r}")
            match = _find(catalog, speaker=found.speaker, emotion=canon)
            if match is None:
                raise ValueError(
                    f"{found.speaker} has no {canon} voice. "
                    f"Use {_speaker_emotions(catalog, found.speaker)}"
                )
            return match
        if found:
            return found
        label = emotion_key or "custom"
        return Voice(
            slug="custom",
            voice_id=raw_voice,
            speaker="custom",
            emotion=label,
            gender="",
            language="",
            name="Custom",
        )

    if voice_key in _FISH:
        raise ValueError(
            "Fish Audio voices are gone. Use paul, oliver, jane, or marie, "
            "and pass the feeling as emotion."
        )

    by_slug = {item.slug: item for item in catalog}
    compact = raw_voice.lower().replace(" ", "_").replace("-", "_")
    by_name = {_norm(item.name): item for item in catalog}
    named = by_slug.get(raw_voice) or by_slug.get(compact) or by_name.get(voice_key)

    speaker: str | None = None
    gender: str | None = None
    if named is not None and not emotion_key:
        return named
    if named is not None:
        speaker = named.speaker
        voice_key = ""
    if voice_key in _SPEAKERS:
        speaker = _SPEAKERS[voice_key]
    elif voice_key in _FEMALE:
        gender = "female"
    elif voice_key in _MALE:
        gender = "male"
    elif voice_key in _EMOTION_ALIASES and not emotion_key:
        emotion_key = voice_key
    elif voice_key:
        parts = voice_key.split()
        if len(parts) == 2 and parts[0] in _SPEAKERS and parts[1] in _EMOTION_ALIASES:
            speaker = _SPEAKERS[parts[0]]
            emotion_key = parts[1]
        else:
            raise ValueError(
                f"unknown voice {raw_voice!r}. Use paul, oliver, jane, or marie."
            )

    canon = _EMOTION_ALIASES.get(emotion_key or "neutral")
    if canon is None:
        raise ValueError(
            f"unknown emotion {emotion_key!r}. Use {', '.join(known_emotions(catalog))}"
        )
    if speaker is None:
        speaker = _language_speaker(language_key, gender)
    if speaker is None:
        speaker = _default_speaker(catalog, canon, gender)
    match = _find(catalog, speaker=speaker, emotion=canon)
    if match is None:
        raise ValueError(
            f"{speaker} has no {canon} voice. Use {_speaker_emotions(catalog, speaker)}"
        )
    return match


def prepare_line(text: object) -> tuple[str, str]:
    """Return spoken words and an emotion pulled out of a leftover Fish-style tag."""
    raw = str(text or "")
    raw = _FENCE.sub(" ", raw)
    raw = _MD_LINK.sub(r"\1", raw)
    raw = _MENTION.sub(lambda match: match.group(1) or " ", raw)
    raw = _URL.sub(" ", raw)
    raw = _EMOJI.sub(" ", raw)
    found = ""

    def _tag(match: re.Match[str]) -> str:
        nonlocal found
        key = _norm(match.group(1))
        canon = _EMOTION_ALIASES.get(key)
        if canon is None:
            return match.group(0)
        if not found:
            found = canon
        return " "

    raw = _TAG.sub(_tag, raw)
    raw = re.sub(r"[*_~`#]+", "", raw)
    spoken = re.sub(r"\s+", " ", raw).strip()
    return spoken, found
