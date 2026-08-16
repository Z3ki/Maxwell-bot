"""Compatibility shims for discord-ext-voice-recv on discord.py-self 2.2+."""

from typing import Any, cast


def ensure_voice_recv_compat() -> None:
    """Patch discord.enums and voice_recv so guild-only code works in DMs."""
    _ensure_speaking_state()
    _patch_voice_recv_dm_hook()
    _patch_voice_recv_reader_lookup()


def _ensure_speaking_state() -> None:
    import discord.enums as enums

    if hasattr(enums, "SpeakingState"):
        return

    # discord.py-self 2.2 moved speaking mode to discord.flags.SpeakingFlags;
    # voice_recv still imports SpeakingState from discord.enums.
    class SpeakingState(enums.Enum):
        none = 0
        voice = 1
        soundshare = 2
        priority = 4

        def __str__(self) -> str:
            return self.name

        def __int__(self) -> int:
            return self.value

    enums.SpeakingState = SpeakingState


def _vc_self_id(vc) -> int:
    user = getattr(vc, "user", None) or getattr(
        getattr(vc, "client", None), "user", None
    )
    return int(getattr(user, "id", 0) or 0)


def _vc_member(vc, uid: int):
    uid = int(uid)
    guild = getattr(getattr(vc, "channel", None), "guild", None)
    if guild is not None:
        member = guild.get_member(uid)
        if member is not None:
            return member
    channel = getattr(vc, "channel", None)
    me = getattr(vc, "user", None) or getattr(getattr(vc, "client", None), "user", None)
    if me is not None and int(getattr(me, "id", 0) or 0) == uid:
        return me
    recipient = getattr(channel, "recipient", None)
    if recipient is not None and int(getattr(recipient, "id", 0) or 0) == uid:
        return recipient
    for user in getattr(channel, "recipients", None) or []:
        if int(getattr(user, "id", 0) or 0) == uid:
            return user
    client = getattr(vc, "client", None)
    if client is not None:
        return client.get_user(uid)
    return None


def _patch_voice_recv_dm_hook() -> None:
    """voice_recv's gateway hook uses vc.guild.me, which is None in DM calls.

    Do not override VoiceRecvClient.guild — VoiceConnectionState needs it
    to stay None so DM connect uses client.change_voice_state.
    """
    try:
        from discord.enums import SpeakingState, try_enum
        from discord.ext.voice_recv import gateway as gw
        from discord.ext.voice_recv.enums import VoiceFlags, VoicePlatform
        from discord.ext.voice_recv.video import VoiceVideoStreams
        from discord.ext.voice_recv.voice_client import VoiceRecvClient
        from discord.voice_state import VoiceConnectionState
    except Exception:
        return
    if getattr(gw.hook, "_maxwell_dm_hook", False):
        return

    log = gw.log
    CLIENT_CONNECT = gw.CLIENT_CONNECT
    VIDEO = gw.VIDEO
    CLIENT_DISCONNECT = gw.CLIENT_DISCONNECT
    FLAGS = gw.FLAGS
    PLATFORM = gw.PLATFORM

    async def hook(self, msg: dict[str, Any]):
        op: int = msg["op"]
        data: dict[str, Any] = msg.get("d", {})
        vc = self._connection.voice_client

        if op not in (3, 6):
            from pprint import pformat

            log.debug("Received op %s: \n%s", op, pformat(data, compact=True))
            if len(msg.keys()) > 2:
                extra = msg.copy()
                extra.pop("op")
                extra.pop("d")
                log.info("WS payload has extra keys: %s", extra)

        if op == self.READY:
            self_id = _vc_self_id(vc)
            if self_id:
                vc._add_ssrc(self_id, data["ssrc"])

        elif op == self.SESSION_DESCRIPTION:
            if vc._reader:
                vc._reader.update_secret_key(bytes(self.secret_key))

        elif op == self.SPEAKING:
            uid = int(data["user_id"])
            ssrc = data["ssrc"]
            vc._add_ssrc(uid, ssrc)
            vc.dispatch(
                "voice_member_speaking_state",
                _vc_member(vc, uid),
                ssrc,
                try_enum(SpeakingState, data["speaking"]),
            )

        elif op == CLIENT_CONNECT:
            for uid in (int(x) for x in data["user_ids"]):
                vc.dispatch("voice_member_connect", _vc_member(vc, uid))

        elif op == VIDEO:
            uid = int(data["user_id"])
            vc._add_ssrc(uid, data["audio_ssrc"])
            streams = VoiceVideoStreams(data=cast("Any", data), vc=vc)
            vc.dispatch("voice_member_video", _vc_member(vc, uid), streams)

        elif op == CLIENT_DISCONNECT:
            uid = int(data["user_id"])
            ssrc = vc._get_ssrc_from_id(uid)
            if vc._reader and ssrc is not None:
                log.debug("Destroying decoder for %s, ssrc=%s", uid, ssrc)
                vc._reader.packet_router.destroy_decoder(ssrc)
            vc._remove_ssrc(user_id=uid)
            vc.dispatch("voice_member_disconnect", _vc_member(vc, uid), ssrc)

        elif op == FLAGS:
            uid = int(data["user_id"])
            vc.dispatch(
                "voice_member_flags",
                _vc_member(vc, uid),
                VoiceFlags._from_value(data["flags"] or 0),
            )

        elif op == PLATFORM:
            uid = int(data["user_id"])
            vc.dispatch(
                "voice_member_platform",
                _vc_member(vc, uid),
                try_enum(VoicePlatform, data["platform"])
                if data["platform"] is not None
                else None,
            )

    hook._maxwell_dm_hook = True  # type: ignore[attr-defined]
    gw.hook = hook
    VoiceRecvClient.create_connection_state = lambda self: VoiceConnectionState(
        self, hook=hook
    )


def _patch_voice_recv_reader_lookup() -> None:
    """voice_recv's SpeakingTimer._lookup_member assumes vc.guild is non-None.

    In DM/group calls ``voice_client.guild`` is None, so a speaking-start
    dispatch raises ``AttributeError: 'NoneType' object has no attribute
    'get_member'`` on the audio thread and tears down the listener. Route the
    lookup through _vc_member, which falls back to DM recipients and the bot's
    own user instead of dereferencing guild unconditionally.
    """
    try:
        from discord.ext.voice_recv.reader import SpeakingTimer
    except Exception:
        return
    if getattr(SpeakingTimer._lookup_member, "_maxwell_dm_lookup", False):
        return

    def _lookup_member(self, ssrc: int):
        whoid = self.voice_client._get_id_from_ssrc(ssrc)
        if not whoid:
            return None
        return _vc_member(self.voice_client, whoid)

    _lookup_member._maxwell_dm_lookup = True  # type: ignore[attr-defined]
    SpeakingTimer._lookup_member = _lookup_member
