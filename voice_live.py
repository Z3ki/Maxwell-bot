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
                    st.active = bytearray()
                    for p in st.pre_roll:
                        st.active.extend(p)
                st.voiced_frames += 1
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
        if duration >= min_secs and st.user_obj is not None:
            self._ready.append((st.user_obj, bytes(st.active), duration))
            if self.debug:
                logger.info("VC utterance ready guild=%s user=%s dur=%.2fs (%s)", self.guild_id, getattr(st.user_obj, 'id', '?'), duration, why)
        st.currently_speaking = False
        st.active = bytearray()
        st.voiced_frames = 0

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
                for user, pcm, dur in out:
                    wav_path = await self._write_temp_wav(user, pcm)
                    await self.on_utterance(user, wav_path, dur)
        except asyncio.CancelledError:
            return

    async def _write_temp_wav(self, user, pcm: bytes) -> str:
        name = f"vc-{self.guild_id}-{getattr(user, 'id', 'u')}-{int(time.time()*1000)}.wav"
        out_dir = Path("temp")
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / name
        with wave.open(str(path), "wb") as w:
            w.setnchannels(self.channels)
            w.setsampwidth(self.sample_width)
            w.setframerate(self.sample_rate)
            w.writeframes(pcm)
        return str(path)

    def cleanup(self):
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        with self._lock:
            self._states.clear()
            self._ready.clear()

    @staticmethod
    def _rms16le(buf: bytes) -> float:
        if len(buf) < 2:
            return 0.0
        mv = memoryview(buf).cast("h")
        n = len(mv)
        if n == 0:
            return 0.0
        total = 0
        for s in mv:
            total += s * s
        return (total / n) ** 0.5
