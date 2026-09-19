"""Compatibility re-exports. Routing guards live on AutonomyEngine itself."""

from __future__ import annotations

from typing import Any

from autonomy import (
    autonomy_configured_channels as _configured_channels,
    autonomy_known_kind as _known_kind,
    autonomy_reply_channel as _reply_channel,
    autonomy_reply_matches_route as _reply_matches_route,
    autonomy_route_allowed as _route_allowed,
    _digits_id as _digits,
)


def install_autonomy_routing_guards(bot: Any) -> bool:
    """No-op: fail-closed routing is part of AutonomyEngine."""
    del bot
    return True


__all__ = [
    "install_autonomy_routing_guards",
    "_configured_channels",
    "_digits",
    "_known_kind",
    "_reply_channel",
    "_reply_matches_route",
    "_route_allowed",
]
