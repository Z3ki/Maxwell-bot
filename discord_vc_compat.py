"""Voice-receive compatibility hook for Maxwell's official Discord bot.

Older releases used ``discord.py-self`` and patched ``discord-ext-voice-recv``
to make user-account DM/group-call voice work. Maxwell now uses official
``discord.py`` bot accounts, which cannot join those calls, so those invasive
monkey patches are obsolete.  The hook stays as a no-op for the existing import
sites and for third-party extensions that imported it during the transition.
"""


def ensure_voice_recv_compat() -> None:
    """No-op retained for compatibility after the official-bot migration."""
    return None
