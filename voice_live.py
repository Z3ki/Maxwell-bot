import asyncio
import logging
import threading
import time
import wave
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from discord.ext import voice_recv

logger = logging.getLogger(__name__)


@dataclass
class _SpeakerState:
    pre_roll: deque = field(default_factory=deque)
    active: bytearray = field(default_factory=bytearray)
    currently_speaking: bool = False
    voiced_frames: int = 0
    voiced_seconds: float = 0.0
    decode_drops: int = 0
    first_voice_time: float = 0.0
    last_voice_time: float = 0.0
    last_packet_time: float = 0.0
    user_obj: object | None = None


class LiveSpeechSink(voice_recv.AudioSink):
    """RMS-based speech detector for discord-ext-voice-recv decoded PCM."""

    sample_rate = 48000
    channels = 2
    sample_width = 2

    def __init__(self, *, loop, on_utterance, guild_id, control, self_user_id, debug=False):
        super().__init__()
        self.loop = loop
        self.on_utterance = on_utterance
        self.guild_id = guild_id
        self.control = control
        self.self_user_id = int(self_user_id) if self_user_id else 0
        self.debug = bool(debug)
        self._states: dict[int, _SpeakerState] = {}
        self._ready: list[tuple[object, bytes, float]] = []
        self._lock = threading.RLock()
        self._ignore_until = 0.0
        self._running = True
        self._task = loop.create_task(self._flush_loop())

    def wants_opus(self):
        return False

    def set_ignore_until(self, monotonic_ts: float):
        with self._lock:
            self._ignore_until = max(self._ignore_until, float(monotonic_ts))

    def record_decode_drop(self, user_id: int):
        with self._lock:
            st = self._states.get(int(user_id))
            if st is None:
                st = _SpeakerState()
                self._states[int(user_id)] = st
            st.decode_drops += 1

    def write(self, user, data):
        if not self._running or user is None:
            return
        now = time.monotonic()
        uid = int(getattr(user, "id", 0) or 0)
        if uid == 0 or uid == self.self_user_id:
            return
        pcm = getattr(data, "pcm", None)
        if not pcm:
            return
        frame = bytes(pcm)
        frame_dur = len(frame) / (self.sample_rate * self.channels * self.sample_width)
        rms = self._rms16le(frame)
        with self._lock:
            if now < self._ignore_until:
                if rms >= int(self.control.get("vc_rms_threshold", 500)) and self.control.get("vc_interrupt_enabled", True):
                    vc = self.voice_client
                    if vc and vc.is_playing():
                        logger.info("VC playback interrupted by user=%s", uid)
                        # Schedule stop on the event loop thread, not the audio thread
                        self.loop.call_soon_threadsafe(vc.stop)
                    self._ignore_until = 0.0
                else:
                    return
            if now < self._ignore_until:
                return
            st = self._states.get(uid)
            if st is None:
                st = _SpeakerState()
                self._states[uid] = st
            st.user_obj = user
            st.last_packet_time = now
            st.pre_roll.append(frame)
            pre_roll_max = max(1, int(float(self.control.get("vc_preroll_seconds", 0.25)) / max(frame_dur, 0.001)))
            while len(st.pre_roll) > pre_roll_max:
                st.pre_roll.popleft()
            if rms >= int(self.control.get("vc_rms_threshold", 500)):
                st.last_voice_time = now
                if not st.currently_speaking:
                    st.currently_speaking = True
                    st.first_voice_time = now
                    st.voiced_frames = 0
                    st.voiced_seconds = 0.0
                    st.decode_drops = 0
                    st.active = bytearray()
                    for p in st.pre_roll:
                        st.active.extend(p)
                st.voiced_frames += 1
                st.voiced_seconds += frame_dur
                st.active.extend(frame)
                max_secs = float(self.control.get("vc_max_seconds", 18.0))
                if len(st.active) >= int(max_secs * self.sample_rate * self.channels * self.sample_width):
                    self._finalize_locked(st, now, "max")
            elif st.currently_speaking:
                st.active.extend(frame)

    def _finalize_locked(self, st: _SpeakerState, now: float, why: str):
        if not st.currently_speaking or not st.active:
            st.currently_speaking = False
            st.active = bytearray()
            return
        duration = len(st.active) / (self.sample_rate * self.channels * self.sample_width)
        min_secs = float(self.control.get("vc_min_seconds", 0.75))
        min_voiced_secs = float(self.control.get("vc_min_voiced_seconds", min(0.35, max(0.12, min_secs * 0.45))))
        min_voiced_frames = int(self.control.get("vc_min_voiced_frames", 8))
        max_decode_drops = int(self.control.get("vc_max_decode_drops", max(8, int(st.voiced_frames * 0.25))))
        if duration >= min_secs and st.voiced_seconds >= min_voiced_secs and st.voiced_frames >= min_voiced_frames and st.decode_drops <= max_decode_drops and st.user_obj is not None:
            self._ready.append((st.user_obj, bytes(st.active), duration))
            if self.debug:
                logger.info("VC utterance ready guild=%s user=%s dur=%.2fs voiced=%.2fs frames=%s drops=%s (%s)", self.guild_id, getattr(st.user_obj, 'id', '?'), duration, st.voiced_seconds, st.voiced_frames, st.decode_drops, why)
        elif self.debug:
            logger.info("VC utterance discarded guild=%s user=%s dur=%.2fs voiced=%.2fs frames=%s drops=%s max_drops=%s (%s)", self.guild_id, getattr(st.user_obj, 'id', '?'), duration, st.voiced_seconds, st.voiced_frames, st.decode_drops, max_decode_drops, why)
        st.currently_speaking = False
        st.active = bytearray()
        st.voiced_frames = 0
        st.voiced_seconds = 0.0
        st.decode_drops = 0

    async def _flush_loop(self):
        try:
            while self._running:
                await asyncio.sleep(0.1)
                now = time.monotonic()
                out = []
                with self._lock:
                    pause = float(self.control.get("vc_pause_seconds", 0.9))
                    for st in self._states.values():
                        if st.currently_speaking and st.last_voice_time and (now - st.last_voice_time) >= pause:
                            self._finalize_locked(st, now, "pause")
                    if self._ready:
                        out = self._ready[:]
                        self._ready.clear()
                # Dispatch utterances as background tasks to avoid blocking pause detection
                for user, pcm, dur in out:
                    asyncio.ensure_future(self._process_utterance(user, pcm, dur))
        except asyncio.CancelledError:
            return

    async def _process_utterance(self, user, pcm: bytes, dur: float):
        try:
            wav_path = await self._write_temp_wav(user, pcm)
            await self.on_utterance(user, wav_path, dur)
        except Exception as e:
            logger.warning("Utterance processing error: %s", e)

    async def _write_temp_wav(self, user, pcm: bytes) -> str:
        name = f"vc-{self.guild_id}-{getattr(user, 'id', 'u')}-{int(time.time()*1000)}.wav"
        out_dir = Path(__file__).resolve().parent / "temp"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / name
        # Downmix to mono. Discord delivers stereo 48kHz PCM, but audio-input
        # LLMs (OpenRouter mimo/gemini/etc.) expect mono and often return empty
        # on stereo WAVs. Averaging the two channels preserves the signal.
        mono = self._downmix_to_mono(pcm)
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(self.sample_width)
            w.setframerate(self.sample_rate)
            w.writeframes(mono)
        return str(path)

    def _downmix_to_mono(self, pcm: bytes) -> bytes:
        if self.channels == 1:
            return pcm
        import array
        try:
            samples = array.array("h", pcm)
        except ValueError:
            n = len(pcm) - (len(pcm) % 2)
            samples = array.array("h", pcm[:n]) if n else array.array("h")
        ch = self.channels
        if len(samples) < ch:
            return b""
        # Fast path for stereo (Discord's default): slice L/R and average.
        # zip + generator runs in C, so a 3s utterance (~144k frames) is sub-ms.
        if ch == 2:
            left = samples[0::2]
            right = samples[1::2]
            mono = array.array("h", ((ls + rs) // 2 for ls, rs in zip(left, right)))
            return mono.tobytes()
        # General case (rare): average each frame's channels.
        mono = array.array("h", bytes(2 * (len(samples) // ch)))
        for i in range(0, len(samples) - ch + 1, ch):
            s = 0
            for c in range(ch):
                s += samples[i + c]
            mono[i // ch] = s // ch
        return mono.tobytes()

    def cleanup(self):
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        with self._lock:
            self._states.clear()
            self._ready.clear()
        # Clean up stale temp WAV files from this guild
        try:
            temp_dir = Path(__file__).resolve().parent / "temp"
            if temp_dir.exists():
                prefix = f"vc-{self.guild_id}-"
                now = time.time()
                for f in temp_dir.glob(f"{prefix}*.wav"):
                    if now - f.stat().st_mtime > 60:
                        f.unlink(missing_ok=True)
        except Exception:
            pass

    @staticmethod
    def _rms16le(buf: bytes) -> float:
        if len(buf) < 2:
            return 0.0
        if len(buf) % 2:
            buf = buf[:-1]
        mv = memoryview(buf).cast("h")
        n = len(mv)
        if n == 0:
            return 0.0
        total = sum(s * s for s in mv)
        return (total / n) ** 0.5
