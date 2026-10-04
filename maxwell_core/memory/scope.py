"""Requester identity and fail-closed visibility policy for stored memory.

Keep Discord transport and SQLite outside this module so every retrieval path
uses the same policy and can be tested without a bot or an embedding backend.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timezone


def _parse_expiry(value: str) -> datetime | None:
    try:
        expiry = datetime.fromisoformat(value)
        return expiry if expiry.tzinfo else expiry.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class MemoryRequester:
    """Validated, Discord-independent authorization context for memory access."""

    user_id: str
    channel_id: str
    guild_id: str = ""
    is_dm: bool = False
    is_admin: bool = False
    channel_is_public: bool = False

    def __post_init__(self):
        for name in ("user_id", "channel_id", "guild_id"):
            value = str(getattr(self, name) or "").strip()
            object.__setattr__(self, name, value if len(value) <= 128 else "")
        for name in ("is_dm", "is_admin", "channel_is_public"):
            object.__setattr__(self, name, getattr(self, name) is True)

    @property
    def valid(self) -> bool:
        return bool(self.user_id and self.channel_id and
                    (not self.guild_id if self.is_dm else self.guild_id))

    @classmethod
    def from_message(cls, message, *, is_admin: bool = False):
        author = getattr(message, "author", None)
        channel = getattr(message, "channel", None)
        guild = getattr(message, "guild", None)
        public = False
        if guild is not None and channel is not None:
            try:
                public = bool(channel.permissions_for(guild.default_role).view_channel)
            except Exception:
                public = False
        return cls(
            user_id=str(getattr(author, "id", "") or ""),
            channel_id=str(getattr(channel, "id", "") or ""),
            guild_id=str(getattr(guild, "id", "") or ""),
            is_dm=guild is None,
            is_admin=is_admin is True,
            channel_is_public=public,
        )


def _has_requester(requester: MemoryRequester | None) -> bool:
    return isinstance(requester, MemoryRequester) and requester.valid


def _decode_metadata(raw) -> dict:
    try:
        result = json.loads(raw or "{}")
        return result if isinstance(result, dict) else {}
    except (TypeError, ValueError):
        return {}


def _memory_row_visible(row: dict, requester: MemoryRequester | None) -> bool:
    """Fail closed unless source provenance fits the requester's current scope."""
    if not _has_requester(requester):
        return False
    metadata = row.get("metadata")
    if not isinstance(metadata, dict):
        metadata = _decode_metadata(metadata)
    kind = str(row.get("kind") or "")
    channel_id = str(row.get("channel_id") or "")
    guild_id = str(row.get("guild_id") or "")

    # Administrator status never widens ordinary transcript retrieval.
    if kind in {"message", "bot_output"}:
        return channel_id == requester.channel_id and (
            guild_id == "" if requester.is_dm else guild_id == requester.guild_id
        )

    if kind == "web_result":
        source_user = str(metadata.get("source_user_id") or "")
        source_channel = str(metadata.get("source_channel_id") or "")
        source_guild = str(metadata.get("source_guild_id") or "")
        scope = str(row.get("scope") or "")
        if metadata.get("source_is_dm") is True:
            return bool(
                requester.is_dm
                and source_user == requester.user_id
                and source_channel == requester.channel_id
                and not guild_id
                and scope == f"dm:{requester.user_id}"
            )
        if requester.is_dm or not requester.guild_id:
            return False
        if (
            guild_id != requester.guild_id
            or source_guild != requester.guild_id
            or not source_channel
        ):
            return False
        if scope == "guild":
            return metadata.get("source_channel_public") is True
        return bool(
            scope == f"channel:{requester.channel_id}"
            and source_channel == requester.channel_id
        )

    source_user = str(metadata.get("source_user_id") or row.get("author_id") or "")
    source_channel = str(metadata.get("source_channel_id") or channel_id or "")
    source_guild = str(metadata.get("source_guild_id") or guild_id or "")
    if not source_user or not source_channel:
        return False
    expires = str(metadata.get("expires_at") or "").strip()
    if expires:
        expiry = _parse_expiry(expires)
        if expiry is None or expiry < datetime.now(timezone.utc):
            return False

    scope = str(row.get("scope") or "")
    scope_kind, _, scope_id = scope.partition(":")
    visibility = str(metadata.get("visibility") or "private").strip().lower()
    source_is_dm = metadata.get("source_is_dm") is True
    if source_is_dm:
        return bool(
            requester.is_dm
            and source_user == requester.user_id
            and source_channel == requester.channel_id
            and scope_kind == "dm"
            and scope_id == requester.user_id
        )

    # Only explicitly approved operator facts are cross-community.
    if scope == "global":
        return bool(
            metadata.get("public_approved") is True
            and metadata.get("source_kind") == "operator_public"
            and (
                visibility in {"public", "shared"}
                or (visibility == "admin_only" and requester.is_admin)
            )
        )
    if requester.is_dm or not requester.guild_id or source_guild != requester.guild_id:
        return False

    same_channel = source_channel == requester.channel_id
    source_public = metadata.get("source_channel_public") is True
    if scope_kind == "channel":
        in_scope = scope_id == requester.channel_id and same_channel
    elif scope_kind == "guild":
        in_scope = scope_id == requester.guild_id and (same_channel or source_public)
    elif scope_kind == "user":
        in_scope = (
            scope_id == requester.user_id == source_user
            and (same_channel or source_public)
        )
    else:
        in_scope = False

    if visibility == "private":
        return bool(
            source_user == requester.user_id
            and same_channel
            and scope_kind in {"channel", "user"}
            and scope_id in {requester.channel_id, requester.user_id}
        )
    if visibility == "restricted":
        return bool(requester.is_admin and same_channel and in_scope)
    if visibility == "admin_only":
        return bool(
            requester.is_admin
            and in_scope
            and (scope_kind != "user" or source_user == requester.user_id)
        )
    if visibility not in {"shared", "public_hint", "public"}:
        return False
    return in_scope
