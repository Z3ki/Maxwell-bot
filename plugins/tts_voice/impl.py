"""Tool implementations for the tts_voice plugin.

Moved out of the historical bot_tools.py monolith. Shared helpers live in
``tooling.helpers``.
"""
from __future__ import annotations

from typing import ClassVar

from tooling import helpers as _helpers
from tools import Tool

# Mechanical split: the original classes used the bot_tools module globals.
# Bind every helper/name here so execute() bodies keep working unchanged.
for _name in dir(_helpers):
    if _name.startswith("__"):
        continue
    globals().setdefault(_name, getattr(_helpers, _name))
del _name

class TtsTool(Tool):
    """Text to Speech generator tool"""
    tool_name = 'tts'
    returns_result = False
    ends_turn = False
    produces_visible_output = True


    # Per-channel last-TTS monotonic timestamp; bounds Riva (paid) + gTTS
    # quota drain and channel spam. The bot is single-process so a class-level
    # dict is sufficient.
    _COOLDOWN_SECONDS = 15.0
    _last_tts: ClassVar[dict[str, float]] = {}

    def get_description(self):
        return (
            "Convert a text response into a speech voice message and send it to the triggering channel. "
            "Params: text (required string), language/lang (optional: english or spanish), "
            "voice (optional: tiktok or mommy — pick the TTS voice)."
        )

    async def execute(
        self,
        message: Message,
        text: str | None = None,
        language: str | None = None,
        lang: str | None = None,
        voice: str | None = None,
        **kwargs,
    ) -> str:
        if not text or not text.strip():
            return "Error: text parameter is required"

        # Per-channel cooldown to prevent quota drain / voice-message spam.
        channel_id = str(getattr(getattr(message, "channel", None), "id", "") or "")
        if channel_id:
            now = asyncio.get_running_loop().time()
            last = TtsTool._last_tts.get(channel_id, 0.0)
            if now - last < TtsTool._COOLDOWN_SECONDS:
                wait = int(TtsTool._COOLDOWN_SECONDS - (now - last))
                return (
                    f"Error: TTS on cooldown for this channel (~{wait}s left). "
                    "Wait and try again."
                )
            TtsTool._last_tts[channel_id] = now
            # Keep the map bounded.
            if len(TtsTool._last_tts) > 200:
                cutoff = now - 600
                TtsTool._last_tts = {
                    c: t for c, t in TtsTool._last_tts.items() if t > cutoff
                }

        language_key = _tts_language_key(language, lang, **kwargs)
        lang_is_spanish = language_key == "spanish"

        # Determine API Key and Setup File
        bot_config = getattr(getattr(self, "bot", None), "config", None)
        nvidia_api_key = os.environ.get("NVIDIA_API_KEY", "") or getattr(
            bot_config, "NVIDIA_API_KEY", ""
        )
        fish_api_key = os.environ.get("FISH_API_KEY", "") or getattr(
            bot_config, "FISH_API_KEY", ""
        )
        token = uuid.uuid4().hex[:12]
        filename = f"tts_{token}.wav"
        voice_filename = f"tts_{token}.ogg"

        tts_source = None  # path to synthesized audio; drives fallback chain

        # Provider order: Fish (best quality, free tier, emotion tags) →
        # Riva (NVIDIA, paid) → gTTS (free fallback). Each block only sets
        # `tts_source` on success; failures fall through silently.
        if not tts_source and fish_api_key:
            fish_model = os.environ.get("TTS_FISH_MODEL", "s2.1-pro-free")
            fish_voice = voice or (
                "spanish" if language_key == "spanish" else None
            )
            fish_ref = _fish_reference_id(fish_voice)
            fish_fmt = os.environ.get("TTS_FISH_FORMAT", "mp3")
            fish_out = await _synthesize_fish_tts(
                text,
                filename,
                api_key=fish_api_key,
                model=fish_model,
                reference_id=fish_ref,
                fmt=fish_fmt,
            )
            if fish_out:
                tts_source = fish_out
                logger.info(
                    "TTS provider: fish (model=%s, voice=%s)", fish_model, voice
                )

        if not tts_source:
            try:
                # Try NVIDIA Riva TTS
                if not nvidia_api_key:
                    raise RuntimeError("NVIDIA_API_KEY is not configured")

                import riva.client
                from riva.client.proto import riva_audio_pb2

                function_id = os.environ.get(
                    "TTS_RIVA_FUNCTION_ID", "877104f7-e885-42b9-8de8-f6e4c6303969"
                )
                auth = riva.client.Auth(
                    use_ssl=True,
                    uri="grpc.nvcf.nvidia.com:443",
                    metadata_args=[
                        ["function-id", function_id],
                        ["authorization", f"Bearer {nvidia_api_key}"],
                    ],
                    options=cast(
                        Any,
                        [
                            ("grpc.max_receive_message_length", 64 * 1024 * 1024),
                            ("grpc.max_send_message_length", 64 * 1024 * 1024),
                        ],
                    ),
                )
                service = riva.client.SpeechSynthesisService(auth)

                tts_voice_name, tts_language_code = _tts_riva_voice_config(language_key)

                # Use gRPC service synchronously (run in executor since it is synchronous gRPC)
                def run_riva():
                    return service.synthesize(
                        text=text,
                        voice_name=tts_voice_name,
                        language_code=tts_language_code,
                        sample_rate_hz=44100,
                        encoding=cast(Any, riva_audio_pb2).AudioEncoding.LINEAR_PCM,
                    )

                loop = asyncio.get_running_loop()
                # Bound the gRPC call: a stalled Riva endpoint would hang this tool
                # and leak an executor thread otherwise.
                resp = await asyncio.wait_for(
                    loop.run_in_executor(None, run_riva), timeout=30
                )
                logger.info(
                    f"Riva TTS synthesized audio with voice={tts_voice_name!r}, language={tts_language_code!r}"
                )

                # Save the WAV file
                with wave.open(filename, "wb") as out_f:
                    out_f.setnchannels(1)
                    out_f.setsampwidth(2)
                    out_f.setframerate(44100)
                    # cast: the riva client returns an untyped stub object; the
                    # synthesized audio bytes live on `.audio` at runtime.
                    out_f.writeframesraw(cast(Any, resp).audio)
                tts_source = filename
                logger.info("TTS provider: riva")
            except Exception as e:
                logger.warning(f"Riva TTS synthesis failed: {e}")

        # Last-resort fallback: gTTS. Used when neither Fish nor Riva produced
        # audio. Kept at the bottom of the provider chain so the comment above
        # about quality (no voice selection / no emotion tags) still applies.
        if not tts_source:
            try:
                from gtts import gTTS

                def run_gtts():
                    tts = gTTS(text=text, lang="es" if lang_is_spanish else "en")
                    tts.save(filename)

                loop = asyncio.get_running_loop()
                await asyncio.wait_for(loop.run_in_executor(None, run_gtts), timeout=30)
                logger.warning(
                    "TTS used gTTS fallback; voice selection/emotion is unavailable in fallback audio"
                )
                tts_source = filename
            except Exception as fallback_err:
                return f"Error: all TTS providers failed (last error: {fallback_err})"

        async def make_voice_ogg(source: str) -> str:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                source,
                "-vn",
                "-ac",
                "1",
                "-ar",
                "48000",
                "-c:a",
                "libopus",
                "-b:a",
                "32k",
                voice_filename,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _stdout, stderr = await communicate_process(proc, timeout=30)
            except asyncio.TimeoutError:
                logger.warning("TTS OGG conversion timed out")
                return source
            if proc.returncode == 0 and os.path.exists(voice_filename):
                return voice_filename
            logger.warning(
                f"Failed to convert TTS to voice OGG: {stderr.decode(errors='replace')[-300:]}"
            )
            return source

        async def get_audio_duration(source: str) -> float:
            proc = await asyncio.create_subprocess_exec(
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                source,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, _stderr = await communicate_process(proc, timeout=15)
            except asyncio.TimeoutError:
                return 1.0
            if proc.returncode != 0:
                return 1.0
            try:
                return max(0.1, float(stdout.decode().strip()))
            except ValueError:
                return 1.0

        async def make_waveform(source: str) -> str:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                source,
                "-f",
                "s16le",
                "-ac",
                "1",
                "-ar",
                "8000",
                "pipe:1",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, _stderr = await communicate_process(proc, timeout=30)
            except asyncio.TimeoutError:
                return base64.b64encode(bytes([128] * 256)).decode("ascii")
            if proc.returncode != 0 or len(stdout) < 2:
                return base64.b64encode(bytes([128] * 256)).decode("ascii")

            sample_count = len(stdout) // 2
            bucket_size = max(1, sample_count // 256)
            waveform = bytearray()
            for bucket_start in range(
                0, min(sample_count, bucket_size * 256), bucket_size
            ):
                bucket_end = min(sample_count, bucket_start + bucket_size)
                peak = 0
                for sample_index in range(bucket_start, bucket_end):
                    byte_index = sample_index * 2
                    sample = int.from_bytes(
                        stdout[byte_index : byte_index + 2], "little", signed=True
                    )
                    peak = max(peak, abs(sample))
                waveform.append(min(255, int(peak / 32767 * 255)))

            if len(waveform) < 256:
                waveform.extend([0] * (256 - len(waveform)))
            return base64.b64encode(bytes(waveform[:256])).decode("ascii")

        async def send_discord_voice_message(source: str):
            from discord.flags import MessageFlags
            from discord.http import handle_message_parameters

            class VoiceMessageFile(discord.File):
                def __init__(self, fp, filename: str, duration: float, waveform: str):
                    super().__init__(fp, filename=filename)
                    self._duration = duration
                    self._waveform = waveform

                def to_dict(self, index: int):
                    payload = super().to_dict(index)
                    payload["duration_secs"] = self._duration
                    payload["waveform"] = self._waveform
                    return payload

            channel = message.channel
            state = getattr(channel, "_state", getattr(message, "_state", None))
            if state is None or not hasattr(state, "http"):
                raise RuntimeError("Discord message state is unavailable")

            flags = MessageFlags._from_value(0)
            flags.voice = True
            duration = await get_audio_duration(source)
            waveform = await make_waveform(source)
            voice_file = VoiceMessageFile(
                source,
                filename="voice-message.ogg",
                duration=duration,
                waveform=waveform,
            )
            with handle_message_parameters(file=voice_file, flags=flags) as params:
                await state.http.send_message(channel.id, params=params)

        # Send as voice-style audio. Telegram adapters use sendVoice; Discord needs a voice flag plus waveform metadata.
        if os.path.exists(filename):
            send_path = filename
            try:
                send_path = await make_voice_ogg(filename)
                if hasattr(message, "send_voice_file"):
                    await cast(Any, message).send_voice_file(send_path)
                else:
                    await send_discord_voice_message(send_path)
                # Distinct from terminal no_response so TTS in a multi-tool batch
                # does not abort follow-up / suppress other tool results.
                return "__TTS_SENT__"
            except Exception as discord_err:
                return f"Error sending TTS voice message to channel: {discord_err}"
            finally:
                for path in {filename, voice_filename}:
                    if os.path.exists(path):
                        with contextlib.suppress(Exception):
                            os.remove(path)
        else:
            return f"Error: Audio file {filename} was not generated"

class JoinVcTool(Tool):
    """Join a voice channel, optionally by following a user."""
    tool_name = 'join_vc'
    returns_result = True
    ends_turn = False


    def get_description(self):
        return (
            "Join a voice channel. Params: voice_channel_id (Discord snowflake), "
            "channel_name in this server, or user_id to hop into that person's "
            "current VC. Then you can hear and talk."
        )

    async def execute(
        self,
        message: Message,
        voice_channel_id: str | None = None,
        channel_id: str | None = None,
        channel_name: str | None = None,
        user_id: str | None = None,
        **kwargs,
    ) -> str:
        if not getattr(getattr(self.bot, "config", None), "ENABLE_VC", True):
            return "Error: voice is disabled (ENABLE_VC=false)"
        target = _resolve_voice_channel(
            self.bot,
            message,
            voice_channel_id or channel_id,
            channel_name,
            user_id,
        )
        if target is None:
            return (
                "Error: no voice channel found. Pass channel_id, channel_name, "
                "or user_id of someone already in a VC."
            )
        guild = getattr(target, "guild", None) or getattr(message, "guild", None)
        text_channel = _vc_listen_text_channel(message, guild)
        try:
            vc = None
            if hasattr(self.bot, "_vc_get_client"):
                vc = self.bot._vc_get_client(guild, target)
            if vc and vc.is_connected():
                if getattr(getattr(vc, "channel", None), "id", None) != getattr(
                    target, "id", None
                ):
                    await vc.move_to(target)
            else:
                if not hasattr(self.bot, "_vc_connect_channel"):
                    return "Error: voice connect is not available on this bot"
                vc = await self.bot._vc_connect_channel(target)
            listening = False
            if hasattr(self.bot, "_vc_start_listening") and guild is not None:
                listening = await self.bot._vc_start_listening(
                    guild, text_channel, target
                )
            return (
                f"Joined #{getattr(target, 'name', target.id)} "
                f"(listening: {bool(listening)})"
            )
        except Exception as e:
            return f"Error joining voice: {e}"

class VcStatusTool(Tool):
    """Show Maxwell's current voice channel and who else is there."""
    tool_name = 'vc_status'
    returns_result = True
    ends_turn = False


    def get_description(self):
        return (
            "Show whether you are in a voice channel and who else is there. "
            "No params. Uses the current server when possible."
        )

    async def execute(self, message: Message, **kwargs) -> str:
        guild = getattr(message, "guild", None)
        clients = list(getattr(self.bot, "voice_clients", None) or [])
        if guild is not None:
            clients = [
                c for c in clients if getattr(c, "guild", None) == guild
            ] or clients
        vc = next(
            (c for c in clients if getattr(c, "is_connected", lambda: False)()), None
        )
        if vc is None or not vc.is_connected():
            extra = ""
            if guild is not None:
                occupied = []
                for ch in getattr(guild, "voice_channels", []) or []:
                    members = [
                        getattr(m, "display_name", str(getattr(m, "id", "?")))
                        for m in (getattr(ch, "members", None) or [])
                    ]
                    if members:
                        occupied.append(
                            f"#{getattr(ch, 'name', ch.id)}: {', '.join(members[:8])}"
                        )
                if occupied:
                    extra = "\nOccupied channels:\n- " + "\n- ".join(occupied[:8])
            return "Not connected to a voice channel." + extra
        channel = getattr(vc, "channel", None)
        members = list(getattr(channel, "members", None) or [])
        names = [
            getattr(m, "display_name", str(getattr(m, "id", "?"))) for m in members[:15]
        ]
        listening = False
        if hasattr(self.bot, "_vc_is_listening"):
            listening = bool(self.bot._vc_is_listening(vc))
        return (
            f"Connected to #{getattr(channel, 'name', getattr(channel, 'id', '?'))} "
            f"in {getattr(getattr(channel, 'guild', None), 'name', '?')} "
            f"(listening: {listening})\n"
            f"Members ({len(members)}): {', '.join(names) or '(empty)'}"
        )

class VcWhereTool(Tool):
    """Find which voice channel a user is in."""
    tool_name = 'vc_where'
    returns_result = True
    ends_turn = False


    def get_description(self):
        return (
            "Find whether a user is in a voice channel and which one. "
            "Params: user_id (required, numeric id or mention)."
        )

    async def execute(
        self, message: Message, user_id: str | None = None, **kwargs
    ) -> str:
        cleaned = re.sub(r"[^0-9]", "", str(user_id or ""))
        if not cleaned:
            return "Error: user_id is required"
        member, channel = _find_member_voice(
            self.bot, int(cleaned), getattr(message, "guild", None)
        )
        if channel is None:
            return f"User {cleaned} is not in a voice channel I can see."
        others = [
            getattr(m, "display_name", str(getattr(m, "id", "?")))
            for m in (getattr(channel, "members", None) or [])
            if str(getattr(m, "id", "")) != cleaned
        ][:8]
        extra = f" with {', '.join(others)}" if others else ""
        return (
            f"{getattr(member, 'display_name', cleaned)} is in "
            f"#{getattr(channel, 'name', channel.id)} "
            f"({getattr(getattr(channel, 'guild', None), 'name', '?')}){extra}"
        )

class LeaveVcTool(Tool):
    """Leave the active voice channel"""
    tool_name = 'leave_vc'
    returns_result = True
    ends_turn = False


    def get_description(self):
        return "Disconnect from the active voice channel in this server."

    async def execute(self, message: Message, **kwargs) -> str:
        if not message.guild:
            return "Error: This tool can only be used within a server/guild."
        vc = None
        for client in self.bot.voice_clients:
            if client.guild.id == message.guild.id:
                vc = client
                break
        if not vc or not vc.is_connected():
            return "Error: I am not currently connected to any voice channel in this server."
        try:
            if hasattr(self.bot, "_vc_stop_listening"):
                await self.bot._vc_stop_listening(
                    message.guild, vc.channel, message.channel
                )
            # Cancel any in-flight VC reply/utterance tasks for this guild.
            key = None
            if hasattr(self.bot, "_vc_context_key"):
                key = self.bot._vc_context_key(
                    message.guild, vc.channel, message.channel
                )
            active = getattr(self.bot, "_vc_active_tasks", None) or {}
            for task in list(active.get(key, []) if key else []):
                if task and not task.done():
                    task.cancel()
            if key and isinstance(active, dict):
                active.pop(key, None)
            await vc.disconnect(force=True)
            return "Successfully disconnected from the voice channel."
        except Exception as e:
            return f"Error leaving voice channel: {e}"
