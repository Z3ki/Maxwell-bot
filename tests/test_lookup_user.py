import asyncio
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
