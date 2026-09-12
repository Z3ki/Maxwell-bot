import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from bot_tools import LookupUserTool


def test_lookup_user_with_bio_and_profile():
    async def _run():
        bot = MagicMock()
        tool = LookupUserTool(bot)

        # Mock user object with bio, banner, accent
        mock_user = MagicMock()
        mock_user.id = 123456789
        mock_user.name = "testuser"
        mock_user.display_name = "Test User"
        mock_user.bot = False
        mock_user.created_at.strftime.return_value = "2024-01-01 00:00:00 UTC"
        mock_user.display_avatar.url = "https://cdn.discordapp.com/avatars/123/abc.png"
        mock_user.bio = "Hello, I am a cool hacker!"
        mock_user.banner = MagicMock()
        mock_user.banner.url = "https://cdn.discordapp.com/banners/123/banner.png"
        mock_user.accent_color = 0xFF5733

        bot.get_user = MagicMock(return_value=None)
        bot.fetch_user = AsyncMock(return_value=mock_user)
        bot.fetch_user_profile = AsyncMock(side_effect=AttributeError("No profile endpoint"))

        mock_msg = MagicMock()
        mock_msg.guild = None

        result = await tool.execute(mock_msg, user_id="123456789")

        assert "Name: Test User (@testuser)" in result
        assert "ID: 123456789" in result
        assert "Bio: Hello, I am a cool hacker!" in result
        assert "Banner: https://cdn.discordapp.com/banners/123/banner.png" in result
        assert "Accent Color: #FF5733" in result

    asyncio.run(_run())


def test_lookup_user_lists_all_roles_and_role_perms():
    async def _run():
        bot = MagicMock()
        bot.guilds = []
        tool = LookupUserTool(bot)
        mock_user = MagicMock()
        mock_user.id = 5
        mock_user.name = "ada"
        mock_user.display_name = "Ada"
        mock_user.bot = False
        mock_user.created_at.strftime.return_value = "2024-01-01 00:00:00 UTC"
        mock_user.display_avatar.url = "https://cdn.discordapp.com/avatars/5/abc.png"
        mock_user.bio = None
        mock_user.banner = None
        mock_user.accent_color = None
        bot.get_user = MagicMock(return_value=mock_user)
        bot.fetch_user = AsyncMock(return_value=mock_user)

        mod = SimpleNamespace(
            name="Mod",
            id=11,
            position=5,
            is_default=lambda: False,
            permissions=SimpleNamespace(
                administrator=False, kick_members=True, manage_messages=True
            ),
        )
        member = SimpleNamespace(
            id=5,
            nick="Ada",
            joined_at=None,
            roles=[mod],
            guild_permissions=SimpleNamespace(
                administrator=False, kick_members=True, manage_messages=True
            ),
        )
        guild = SimpleNamespace(
            id=10,
            name="Villa",
            get_member=lambda uid: member if int(uid) == 5 else None,
        )
        mock_msg = MagicMock()
        mock_msg.guild = guild

        result = await tool.execute(mock_msg, user_id="5")
        assert "Roles:" in result
        assert "Mod (11, pos 5)" in result
        assert "kick_members" in result
        assert "manage_messages" in result
        assert "Effective perms:" in result
        assert "Mod tools they can authorize:" in result
        assert "kick_member" in result

    asyncio.run(_run())
