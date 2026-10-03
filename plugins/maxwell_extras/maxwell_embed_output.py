"""Compatibility entry point for removing the retired automatic embed wrapper."""
from typing import Any

import user_install as ui


def install_maxwell_embed_output(bot: Any) -> None:
    """Keep legacy plugin reloads on the normal text transport."""
    del bot
    ui.unwrap_session_send("maxwell_embed")


__all__ = ["install_maxwell_embed_output"]
