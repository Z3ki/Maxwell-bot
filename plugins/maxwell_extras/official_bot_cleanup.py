"""Remove leftover user-profile behavior from the old self-bot era.

Maxwell now uses the official bot account transport. Discord's bot user/member
objects can still expose ordinary server data such as roles and voice state,
but personal profile fields such as an About Me/bio were a self-bot-era
assumption and should not be advertised or surfaced.
"""

from __future__ import annotations

from typing import Any

from bot_tools import LookupUserTool
import tool_schemas

_INSTALLED = False


def _clean_lookup_description() -> str:
    return (
        "Look up a Discord user by ID or mention. Params: user_id "
        "(required, numeric ID or @mention). Returns name, account creation date, "
        "banner, accent color, guild roles and permissions, avatar, and voice state."
    )


def install_official_bot_cleanup(bot: Any) -> None:
    """Stop exposing legacy About Me/bio lookup behavior to the model."""

    del bot
    global _INSTALLED
    if _INSTALLED or getattr(LookupUserTool, "_maxwell_official_cleanup", False):
        _INSTALLED = True
        return

    original_execute = LookupUserTool.execute

    def get_description(self: Any) -> str:
        return _clean_lookup_description()

    async def execute(self: Any, *args: Any, **kwargs: Any) -> Any:
        result = await original_execute(self, *args, **kwargs)
        if not isinstance(result, str):
            return result
        # Older cached/fake user objects may still carry a `bio` attribute.
        # Do not surface it now that Maxwell is official-bot-only.
        lines = [line for line in result.splitlines() if not line.lstrip().startswith("Bio:")]
        return "\n".join(lines)

    LookupUserTool.get_description = get_description
    LookupUserTool.execute = execute
    LookupUserTool._maxwell_official_cleanup = True  # type: ignore[attr-defined]

    schema = tool_schemas.TOOL_PARAMETERS.get("lookup_user") or {}
    props = schema.get("properties") if isinstance(schema, dict) else None
    user_id = props.get("user_id") if isinstance(props, dict) else None
    if isinstance(user_id, dict):
        user_id["description"] = (
            "Numeric user ID or @mention. Returns account creation date, banner, "
            "accent color, guild roles/nickname, avatar, and voice state."
        )

    _INSTALLED = True


__all__ = ["install_official_bot_cleanup"]
