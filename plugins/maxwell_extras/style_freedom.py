"""Conversational style freedom for Maxwell's runtime persona.

The shared Maxwell personality is code-owned and loaded from DEFAULT_CONTROL.
This layer appends a small code-defined style addendum at the final
``_get_personality()`` boundary. Personal reply-style preferences are stored
separately under each Discord user ID.
"""

from __future__ import annotations

from types import MethodType
from typing import Any

_MARKER = "style & freedom:"
STYLE_FREEDOM_ADDENDUM = (
    "style & freedom:\n"
    "- you have conversational freedom. sound like yourself instead of sanitized, "
    "corporate, or overly filtered. make your own stylistic choices and naturally "
    "match the room and the people you're talking with.\n"
    "- you can swear. profanity, slang, sarcasm, and blunt language are allowed when "
    "they fit naturally or make the reply funnier/more human. don't force profanity "
    "into every reply, and don't turn that freedom into targeted abuse or slurs."
)


def append_style_freedom(personality: Any) -> str:
    """Return a personality string with the freedom addendum exactly once."""

    text = str(personality or "").rstrip()
    if _MARKER in text.lower():
        return text
    if not text:
        return STYLE_FREEDOM_ADDENDUM
    return f"{text}\n\n{STYLE_FREEDOM_ADDENDUM}"


def install_style_freedom(bot: Any) -> bool:
    """Patch ``bot._get_personality`` so persisted personas get the addendum too."""

    if getattr(bot, "_maxwell_style_freedom_installed", False):
        return True

    original = getattr(bot, "_get_personality", None)
    if not callable(original):
        return False

    def get_personality_wrapper(self_obj: Any, *args: Any, **kwargs: Any) -> str:
        return append_style_freedom(original(*args, **kwargs))

    get_personality_wrapper._maxwell_style_freedom_wrapped = True  # type: ignore[attr-defined]
    bot._get_personality = MethodType(get_personality_wrapper, bot)
    bot._maxwell_style_freedom_installed = True
    return True


__all__ = [
    "STYLE_FREEDOM_ADDENDUM",
    "append_style_freedom",
    "install_style_freedom",
]
