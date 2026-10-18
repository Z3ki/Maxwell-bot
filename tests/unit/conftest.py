"""Shared fakes for the unit suite.

Everything here is offline and deterministic: no Discord gateway, no provider
HTTP, no real user IDs. Snowflakes are small synthetic integers.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

_ids = itertools.count(1001)


def snowflake() -> int:
    """A synthetic, obviously fake Discord ID."""
    return next(_ids)


@dataclass
class FakeUser:
    id: int = field(default_factory=snowflake)
    name: str = "tester"
    bot: bool = False
    display_name: str = "Tester"

    @property
    def mention(self) -> str:
        return f"<@{self.id}>"


@dataclass
class FakeChannel:
    id: int = field(default_factory=snowflake)
    name: str = "general"
    guild: Any = None
    sent: list[dict] = field(default_factory=list)

    async def send(self, content: str | None = None, **kwargs: Any) -> Any:
        self.sent.append({"content": content, **kwargs})
        return SimpleNamespace(id=snowflake(), content=content, channel=self)


@dataclass
class FakeGuild:
    id: int = field(default_factory=snowflake)
    name: str = "test-guild"
    owner_id: int = 0


@dataclass
class FakeMessage:
    content: str = ""
    author: FakeUser = field(default_factory=FakeUser)
    channel: FakeChannel = field(default_factory=FakeChannel)
    guild: FakeGuild | None = None
    id: int = field(default_factory=snowflake)
    created_at: datetime = field(
        default_factory=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc)
    )
    response_visibility: str = "public"


@pytest.fixture
def make_message():
    """Build a guild (default) or DM-less message with fresh fake IDs."""

    def build(
        content: str = "hello", *, in_guild: bool = True, **overrides: Any
    ) -> FakeMessage:
        guild = overrides.pop("guild", FakeGuild() if in_guild else None)
        channel = overrides.pop("channel", FakeChannel(guild=guild))
        return FakeMessage(content=content, guild=guild, channel=channel, **overrides)

    return build
