"""Mint one-time dashboard sign-in links for an authenticated Discord user."""

from __future__ import annotations

import asyncio
from pathlib import Path

from site_backend import RateLimiter


class DashboardError(Exception):
    pass


class DashboardLinks:
    def __init__(self, bot):
        from api.dashboard_auth import DashboardConfig, DashboardSessions

        self.bot = bot
        root = Path(getattr(getattr(bot, "config", None), "DATA_DIR", "data"))
        self.config = DashboardConfig.from_env(root)
        self.sessions = None
        if self.config is not None and self.config.link_mode:
            self.sessions = DashboardSessions(root / "dashboard" / "sessions.sqlite3", self.config.key)
        # Each link is a session key. Nobody needs more than a few a minute.
        self.limit = RateLimiter(rate=1 / 60, burst=4)

    @property
    def available(self) -> bool:
        return self.config is not None

    @property
    def url(self) -> str:
        return self.config.url if self.config else ""

    def manageable_guilds(self, uid: str) -> list[dict]:
        """Guilds this user can manage, from the member cache. The API rechecks live."""
        allows = getattr(self.bot, "_dev_scope_allows", None)
        rows = []
        for guild in list(getattr(self.bot, "guilds", None) or []):
            member = guild.get_member(int(uid)) if uid.isdigit() else None
            if member is None:
                continue
            perms = getattr(member, "guild_permissions", None)
            owner = str(getattr(guild, "owner_id", "")) == uid
            if not owner and not (perms and (perms.administrator or perms.manage_guild)):
                continue
            if callable(allows) and not allows(None, guild_id=guild.id):
                continue
            rows.append({"id": str(guild.id), "name": str(guild.name)[:100]})
        rows.sort(key=lambda row: row["name"].lower())
        return rows[:200]

    async def link_for(self, user) -> str:
        """A URL that signs ``user`` in once. OAuth deployments just get the page."""
        if self.config is None:
            raise DashboardError("The web dashboard is not set up on this bot.")
        if not self.config.link_mode:
            return self.config.url
        uid = str(getattr(user, "id", "") or "")
        if not uid.isdigit() or getattr(user, "bot", False):
            raise DashboardError("A signed-in Discord user is required.")
        if not self.limit.allow(uid):
            raise DashboardError("You just made a link. Use that one, or wait a minute.")
        name = str(getattr(user, "global_name", None) or getattr(user, "name", "") or uid)[:100]
        guilds = self.manageable_guilds(uid)
        token = await asyncio.to_thread(self.sessions.mint_link, {"id": uid, "name": name}, guilds)
        return f"{self.config.url}#t={token}"
