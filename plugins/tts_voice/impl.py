"""Speak in the channel with Mistral Voxtral TTS.

This replaces Fish Audio. Tone comes from the preset voice, not from tags
in the text.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import tempfile
import urllib.error
import urllib.request
from io import BytesIO
from pathlib import Path
from typing import Any, ClassVar

from process_utils import communicate_process
from tools import Tool

from .clones import download_cdn_audio, prepare_sample, select_audio
from .voices import prepare_line, resolve_voice

logger = logging.getLogger("maxwell.tts")

SPEECH_URL = "https://api.mistral.ai/v1/audio/speech"
DEFAULT_MODEL = "voxtral-mini-tts-2603"
MAX_AUDIO_BYTES = 8 * 1024 * 1024
_COOLDOWN_SECONDS = 8.0


class SpeechError(RuntimeError):
    def __init__(self, status: int, detail: str) -> None:
        self.status = status
        super().__init__(detail)


def mistral_api_key(bot: Any) -> str:
    config = getattr(bot, "config", None)
    return (
        os.environ.get("MISTRAL_API_KEY", "").strip()
        or str(getattr(config, "MISTRAL_API_KEY", "") or "").strip()
    )


def tts_model(bot: Any) -> str:
    config = getattr(bot, "config", None)
    raw = (
        os.environ.get("MISTRAL_TTS_MODEL", "").strip()
        or str(getattr(config, "MISTRAL_TTS_MODEL", "") or "").strip()
        or DEFAULT_MODEL
    )
    if not raw or len(raw) > 64 or any(ch.isspace() for ch in raw):
        return DEFAULT_MODEL
    return raw


def synthesize_speech(
    text: str,
    voice_id: str,
    api_key: str,
    model: str,
    ref_audio: str = "",
) -> bytes:
    """POST /v1/audio/speech and return the mp3 bytes. Runs in a worker thread.

    ``ref_audio`` is a one-shot clone (base64). It replaces ``voice_id``.
    """
    body: dict[str, Any] = {
        "model": model,
        "input": text,
        "response_format": "mp3",
        "stream": False,
    }
    if ref_audio:
        body["ref_audio"] = ref_audio
    elif voice_id:
        body["voice_id"] = voice_id
    else:
        raise SpeechError(0, "Mistral TTS needs a voice")
    payload = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        SPEECH_URL,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            body = response.read(MAX_AUDIO_BYTES + 1)
            status = getattr(response, "status", 200)
    except urllib.error.HTTPError as exc:
        detail = exc.read(2000).decode("utf-8", "replace")
        detail = detail.replace(api_key, "")
        message = _error_detail(exc.code, detail)
        raise SpeechError(exc.code, message) from None
    except urllib.error.URLError as exc:
        raise SpeechError(0, "Mistral TTS could not be reached") from exc
    if len(body) > MAX_AUDIO_BYTES:
        raise SpeechError(status, "Mistral TTS audio was too large")
    if body.startswith((b"ID3", b"\xff\xfb")):
        return body
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise SpeechError(
            status, "Mistral TTS returned an unreadable audio body"
        ) from exc
    encoded = str(parsed.get("audio_data") or "")
    try:
        audio = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise SpeechError(status, "Mistral TTS returned no audio") from exc
    if not audio or len(audio) > MAX_AUDIO_BYTES:
        raise SpeechError(status, "Mistral TTS returned no audio")
    return audio


def _error_detail(status: int, body: str) -> str:
    if status in {401, 403}:
        if status == 403:
            return "Mistral refused that line"
        return "Mistral TTS key was rejected"
    message = ""
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        raw = parsed.get("message") or parsed.get("detail") or ""
        if isinstance(raw, dict):
            raw = raw.get("message") or ""
        message = str(raw or "").strip()
    message = " ".join(message.split())[:160]
    if message:
        return f"Mistral TTS failed (HTTP {status}): {message}"
    return f"Mistral TTS failed (HTTP {status})"


async def _run_ffmpeg(args: list[str], timeout: float) -> tuple[int, bytes, bytes]:
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await communicate_process(process, timeout=timeout)
    except asyncio.TimeoutError:
        return 124, b"", b"timeout"
    return process.returncode or 0, stdout, stderr


async def transcode_voice(mp3: bytes) -> tuple[bytes, float, str] | None:
    """Turn mp3 into a Discord voice-message ogg, plus duration and waveform."""
    with tempfile.TemporaryDirectory(prefix="maxwell-tts-") as directory:
        folder = Path(directory)
        source = folder / "speech.mp3"
        target = folder / "voice-message.ogg"
        source.write_bytes(mp3)
        code, _stdout, _stderr = await _run_ffmpeg(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(source),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "48000",
                "-c:a",
                "libopus",
                "-b:a",
                "32k",
                str(target),
            ],
            30,
        )
        if code != 0 or not target.is_file():
            return None
        ogg = target.read_bytes()
        probe_code, probe_out, _probe_err = await _run_ffmpeg(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(target),
            ],
            15,
        )
        duration = 1.0
        if probe_code == 0:
            with contextlib.suppress(ValueError):
                duration = max(0.1, float(probe_out.decode().strip()))
        wave_code, wave_out, _wave_err = await _run_ffmpeg(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(target),
                "-f",
                "s16le",
                "-ac",
                "1",
                "-ar",
                "8000",
                "pipe:1",
            ],
            30,
        )
        waveform = _waveform(wave_out if wave_code == 0 else b"")
        return ogg, duration, waveform


def _waveform(pcm: bytes) -> str:
    if len(pcm) < 2:
        return base64.b64encode(bytes([128] * 256)).decode("ascii")
    sample_count = len(pcm) // 2
    bucket = max(1, sample_count // 256)
    peaks = bytearray()
    for start in range(0, min(sample_count, bucket * 256), bucket):
        end = min(sample_count, start + bucket)
        peak = 0
        for index in range(start, end):
            sample = int.from_bytes(
                pcm[index * 2 : index * 2 + 2], "little", signed=True
            )
            peak = max(peak, abs(sample))
        peaks.append(min(255, int(peak / 32767 * 255)))
    if len(peaks) < 256:
        peaks.extend([0] * (256 - len(peaks)))
    return base64.b64encode(bytes(peaks[:256])).decode("ascii")


async def deliver_speech(bot: Any, message: Any, audio: bytes) -> Any:
    """Post a voice message, or an mp3 attachment when voice upload is unavailable."""
    converted = await transcode_voice(audio)
    if converted is not None:
        ogg, duration, waveform = converted
        try:
            return await _send_voice_message(message, ogg, duration, waveform)
        except Exception as exc:
            logger.warning("TTS voice-message upload failed (%s)", type(exc).__name__)
    from tooling.helpers import deliver_attachment
    from discord import File

    sent, error = await deliver_attachment(
        message, File(BytesIO(audio), filename="voice.mp3"), label="voice"
    )
    if error:
        raise RuntimeError(error)
    return sent


async def _send_voice_message(
    message: Any, ogg: bytes, duration: float, waveform: str
) -> Any:
    from discord import File
    from discord.flags import MessageFlags
    from discord.http import handle_message_parameters

    class VoiceMessageFile(File):
        def to_dict(self, index: int) -> dict[str, Any]:
            payload = super().to_dict(index)
            payload["duration_secs"] = duration
            payload["waveform"] = waveform
            return payload

    channel = getattr(message, "channel", None)
    state = getattr(channel, "_state", None) or getattr(message, "_state", None)
    http = getattr(state, "http", None)
    channel_id = getattr(channel, "id", None)
    if http is None or channel_id is None or not hasattr(http, "send_message"):
        raise RuntimeError("voice message transport unavailable")
    flags = MessageFlags._from_value(0)
    flags.voice = True
    voice_file = VoiceMessageFile(BytesIO(ogg), filename="voice-message.ogg")
    with handle_message_parameters(file=voice_file, flags=flags) as params:
        return await http.send_message(channel_id, params=params)


async def _pick_voice(
    message: Any,
    voice: str,
    emotion: str,
    language: str,
    reference: str,
) -> tuple[str, str, str]:
    """Return voice_id, one-shot ref_audio base64, and a log label."""
    if str(reference or "").strip():
        _filename, url = select_audio(message, reference)
        raw = await asyncio.to_thread(download_cdn_audio, url)
        sample = await prepare_sample(raw)
        encoded = base64.b64encode(sample).decode("ascii")
        return "", encoded, "reference"
    chosen = resolve_voice(voice, emotion, language)
    return chosen.voice_id, "", chosen.name


def _record(bot: Any, message: Any, sent: Any) -> None:
    response_id = (
        sent.get("id") if isinstance(sent, dict) else getattr(sent, "id", None)
    )
    if not response_id:
        return
    record = getattr(bot, "_record_delivery", None)
    if not callable(record):
        return
    if not hasattr(sent, "id"):
        sent = type("Sent", (), {"id": response_id})()
    record(message, sent)


class TtsTool(Tool):
    """Speak a line in this channel."""

    tool_name = "tts"
    # The voice message is the reply. Sending this result back makes the
    # model call tts again on the next round.
    returns_result = False
    ends_turn = False
    produces_visible_output = True
    timeout_seconds = 60
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": (
                    "Words to speak, any length. No markdown, emoji, or emotion tags."
                ),
            },
            "emotion": {
                "type": "string",
                "description": (
                    "Feeling of the voice: happy, sad, angry, frustrated, excited, "
                    "confident, cheerful, neutral, sarcastic, curious, confused, "
                    "jealous, or ashamed."
                ),
            },
            "voice": {
                "type": "string",
                "description": "paul, oliver, jane, or marie. Default is paul.",
            },
            "language": {
                "type": "string",
                "description": "Optional hint: english, british, or french.",
            },
            "reference": {
                "type": "string",
                "description": (
                    "Clone an audio attachment for this one line. "
                    "Filename, or 'attachment'. Never saved. Only use this "
                    "for a voice the person asking owns or has permission to "
                    "clone — never to imitate a real person without consent."
                ),
            },
        },
        "required": ["text"],
    }
    _last_tts: ClassVar[dict[str, float]] = {}

    def get_description(self) -> str:
        return (
            "Speak in this channel as a Discord voice message. "
            "Text length is unlimited. "
            "voice= paul, oliver, jane, or marie. "
            "emotion= applies to those presets only. "
            "reference= clones one of the requester's own audio attachments for this "
            "one line and never saves it. Never clone a voice to imitate someone who "
            "did not agree to it; only the requester's own clips are accepted. "
            "A success returns nothing; do not call it again in the same turn. "
            "An error is returned so you can fix the voice or emotion and retry once. "
            "On a cooldown error, wait. Do not retry in this turn."
        )

    async def execute(
        self,
        message: Any,
        text: str | None = None,
        emotion: str | None = None,
        voice: str | None = None,
        language: str | None = None,
        lang: str | None = None,
        reference: str | None = None,
        **kwargs: Any,
    ) -> str:
        spoken, tagged = prepare_line(text)
        if not spoken:
            return "Error: text parameter is required"
        feeling = emotion or tagged or kwargs.get("mood") or ""
        reference = reference or kwargs.get("ref") or kwargs.get("sample") or ""
        try:
            voice_id, ref_audio, label = await _pick_voice(
                message,
                voice or "",
                feeling,
                language or lang or "",
                reference,
            )
        except ValueError as exc:
            return f"Error: {exc}"

        channel_id = str(getattr(getattr(message, "channel", None), "id", "") or "")
        if channel_id:
            now = asyncio.get_running_loop().time()
            last = TtsTool._last_tts.get(channel_id, 0.0)
            if now - last < _COOLDOWN_SECONDS:
                wait = int(_COOLDOWN_SECONDS - (now - last)) + 1
                return (
                    f"Error: TTS on cooldown for this channel (~{wait}s left). "
                    "Wait and try again."
                )

        api_key = mistral_api_key(self.bot)
        if not api_key:
            return "Error: Mistral TTS is not configured"
        try:
            audio = await asyncio.to_thread(
                synthesize_speech,
                spoken,
                voice_id,
                api_key,
                tts_model(self.bot),
                ref_audio,
            )
            sent = await deliver_speech(self.bot, message, audio)
        except SpeechError as exc:
            logger.warning("TTS synthesis failed (HTTP %s)", exc.status)
            return f"Error: {exc}"
        except RuntimeError as exc:
            text = str(exc).strip()
            if text.startswith("Error:"):
                return text
            logger.warning("TTS delivery failed (%s)", type(exc).__name__)
            return "Error: could not send the voice message"
        except Exception as exc:
            logger.warning("TTS delivery failed (%s)", type(exc).__name__)
            return f"Error: could not send the voice message ({type(exc).__name__})"

        if channel_id:
            now = asyncio.get_running_loop().time()
            TtsTool._last_tts[channel_id] = now
            if len(TtsTool._last_tts) > 200:
                cutoff = now - 600
                TtsTool._last_tts = {
                    key: stamp
                    for key, stamp in TtsTool._last_tts.items()
                    if stamp > cutoff
                }
        _record(self.bot, message, sent)
        logger.info("TTS provider: mistral voice=%s chars=%s", label, len(spoken))
        return f"__TTS_SENT__ Voice message sent ({label})."
