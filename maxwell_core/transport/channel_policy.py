"""Channel restrictions shared by delayed schedules and reminders."""

from typing import Any, Mapping


def channel_policy_reason(
    control: Mapping[str, Any], channel_id: str, *, is_guild: bool
) -> str:
    """Block listed channels everywhere; apply the allowlist only in guilds."""
    blocked = {str(item) for item in (control.get("blocked_channels") or [])}
    if channel_id in blocked:
        return "blocked_channel"
    if is_guild:
        allowed = {str(item) for item in (control.get("allowed_channels") or [])}
        if allowed and channel_id and channel_id not in allowed:
            return "channel_not_allowed"
    return ""
