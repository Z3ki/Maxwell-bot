"""Compatibility shims for discord-ext-voice-recv on discord.py-self 2.2+."""

from enum import Enum


def ensure_voice_recv_compat() -> None:
    """Patch discord.enums so voice_recv can import on discord.py-self 2.2+."""
    import discord.enums as enums

    if hasattr(enums, "SpeakingState"):
        return

    # discord.py-self 2.2 moved speaking mode to discord.flags.SpeakingFlags;
    # voice_recv still imports SpeakingState from discord.enums.
    class SpeakingState(Enum):
        none = 0
        voice = 1
        soundshare = 2
        priority = 4

        def __str__(self) -> str:
            return self.name

        def __int__(self) -> int:
            return self.value

    enums.SpeakingState = SpeakingState
