"""Transport-neutral conversation context.

Discord remains the first-class transport. Adapters map their native
objects into this shape so the AI loop does not import discord.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class NormalizedMessage:
    id: str
    content: str
    author_id: str
    author_name: str = ""
    channel_id: str = ""
    guild_id: str | None = None
    platform: str = "discord"
    is_direct: bool = False
    attachments: list[dict[str, Any]] = field(default_factory=list)
    raw: Any = None


@dataclass
class ExecutionContext:
    message: NormalizedMessage
    platform: str = "discord"
    user_id: str = ""
    is_admin: bool = False
    is_owner: bool = False
    guild_id: str | None = None
    channel_id: str = ""
    tool_platform: str = "discord"
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_discord(cls, message: Any, *, is_admin: bool = False, is_owner: bool = False) -> ExecutionContext:
        author = getattr(message, "author", None)
        channel = getattr(message, "channel", None)
        guild = getattr(message, "guild", None)
        user_id = str(getattr(author, "id", "") or "")
        channel_id = str(getattr(channel, "id", "") or "")
        guild_id = str(getattr(guild, "id", "") or "") or None
        normalized = NormalizedMessage(
            id=str(getattr(message, "id", "") or ""),
            content=str(getattr(message, "content", "") or ""),
            author_id=user_id,
            author_name=str(
                getattr(author, "display_name", None)
                or getattr(author, "name", "")
                or ""
            ),
            channel_id=channel_id,
            guild_id=guild_id,
            platform="discord",
            is_direct=guild is None,
            raw=message,
        )
        return cls(
            message=normalized,
            platform="discord",
            user_id=user_id,
            is_admin=is_admin,
            is_owner=is_owner,
            guild_id=guild_id,
            channel_id=channel_id,
            tool_platform="discord",
        )
