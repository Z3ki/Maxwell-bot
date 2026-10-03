"""Bulk deletion previews an exact set and can only be confirmed by its user."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from plugins.discord_messages.impl import PurgeMessagesTool


class _Target:
    def __init__(self, mid, author_id):
        self.id = mid
        self.author = SimpleNamespace(id=author_id)
        self.created_at = datetime(2026, 9, 28, tzinfo=timezone.utc)
        self.deleted = False

    async def delete(self, **_kwargs):
        self.deleted = True


class _Guild:
    id = 500
    name = "test guild"

    def __init__(self):
        self.permissions = {7: True, 999: True}
        self.members = {7: SimpleNamespace(id=7, guild=self, display_name="Ada")}
        self.me = SimpleNamespace(id=999, guild=self)

    def get_member(self, uid):
        return self.members.get(int(uid))

    async def fetch_member(self, uid):
        if int(uid) not in self.members:
            raise LookupError("not a member")
        return self.members[int(uid)]


class _Channel:
    id = 600
    name = "moderation"

    def __init__(self, guild, targets):
        self.guild = guild
        self.targets = {str(item.id): item for item in targets}
        self.preview = None

    def permissions_for(self, member):
        return SimpleNamespace(
            administrator=False,
            manage_messages=bool(self.guild.permissions.get(int(member.id), False)),
        )

    async def history(self, *, limit):
        for target in list(self.targets.values())[:limit]:
            yield target

    async def send(self, content, **kwargs):
        self.preview = SimpleNamespace(content=content, view=kwargs.get("view"))
        return self.preview

    async def fetch_message(self, mid):
        return self.targets[str(mid)]


class _Response:
    def __init__(self):
        self.deferred = False
        self.messages = []

    def is_done(self):
        return self.deferred

    async def send_message(self, content, **kwargs):
        self.messages.append((content, kwargs))

    async def defer(self, **_kwargs):
        self.deferred = True


class _Interaction:
    def __init__(self, guild, channel, uid):
        self.guild_id = str(guild.id)
        self.channel_id = str(channel.id)
        self.user = SimpleNamespace(id=uid)
        self.response = _Response()
        self.followup = SimpleNamespace(send=self._send_followup)
        self.followups = []
        self.message = None

    async def _send_followup(self, content, **kwargs):
        self.followups.append((content, kwargs))


def _setup():
    guild = _Guild()
    targets = [_Target(101, 7), _Target(102, 8), _Target(103, 7)]
    channel = _Channel(guild, targets)
    bot = SimpleNamespace(user=SimpleNamespace(id=999))
    requester = guild.members[7]
    message = SimpleNamespace(author=requester, guild=guild, channel=channel)
    return guild, channel, targets, bot, message


def test_purge_only_previews_exact_bounded_targets_until_confirmation():
    guild, channel, targets, bot, message = _setup()
    tool = PurgeMessagesTool(bot)

    result = asyncio.run(
        tool.execute(message, limit="3", user_id="7")
    )

    assert "nothing has been deleted yet" in result
    assert channel.preview is not None
    view = channel.preview.view
    assert view.target_ids == ("101", "103")
    assert "message_id=101" in channel.preview.content
    assert "message_id=103" in channel.preview.content
    assert "message_id=102" not in channel.preview.content
    assert all(not target.deleted for target in targets)
    assert not hasattr(channel, "purge")

    # A different user cannot confirm the pending destructive target set.
    intruder = _Interaction(guild, channel, 8)
    assert asyncio.run(view.interaction_check(intruder)) is False
    assert all(not target.deleted for target in targets)

    # The original requester can confirm only after a fresh membership/role
    # lookup. The callback executes exactly the IDs shown in the preview.
    confirmer = _Interaction(guild, channel, 7)
    asyncio.run(view.interaction_check(confirmer))
    asyncio.run(view.children[0].callback(confirmer))
    assert targets[0].deleted is True
    assert targets[1].deleted is False
    assert targets[2].deleted is True


def test_purge_confirmation_rechecks_permissions_after_role_revocation():
    guild, channel, targets, bot, message = _setup()
    tool = PurgeMessagesTool(bot)
    asyncio.run(tool.execute(message, limit="1", user_id="7"))
    view = channel.preview.view
    guild.permissions[7] = False

    interaction = _Interaction(guild, channel, 7)
    asyncio.run(view.children[0].callback(interaction))

    assert not targets[0].deleted
    assert "permissions no longer allow it" in interaction.followups[0][0]


def test_purge_confirm_deletes_when_message_delete_rejects_reason():
    """discord.py 2.7 Message.delete(reason=) raises TypeError and used to abort."""
    guild, channel, _targets, bot, message = _setup()
    reasons = []

    class _LibraryMessage(_Target):
        def __init__(self, mid, author_id):
            super().__init__(mid, author_id)
            self.channel = SimpleNamespace(id=channel.id)

            async def delete_message(channel_id, message_id, *, reason=None):
                assert channel_id == channel.id
                assert message_id == mid
                reasons.append(reason)
                self.deleted = True

            self._state = SimpleNamespace(
                http=SimpleNamespace(delete_message=delete_message)
            )

        async def delete(self, *, delay=None):
            raise TypeError("unexpected keyword reason")

    target = _LibraryMessage(101, 7)
    channel.targets = {"101": target}
    tool = PurgeMessagesTool(bot)
    asyncio.run(tool.execute(message, limit="1", user_id="7"))
    confirmer = _Interaction(guild, channel, 7)
    asyncio.run(channel.preview.view.children[0].callback(confirmer))
    assert target.deleted is True
    assert reasons and reasons[0]
    assert "finished" in confirmer.followups[0][0].lower() or "deleted" in confirmer.followups[0][0].lower()


def test_invalid_user_filter_cannot_turn_into_an_unfiltered_purge():
    guild, channel, targets, bot, message = _setup()
    result = asyncio.run(
        PurgeMessagesTool(bot).execute(message, user_id="not-a-user-id")
    )
    assert "valid Discord user ID" in result
    assert channel.preview is None
    assert all(not target.deleted for target in targets)
