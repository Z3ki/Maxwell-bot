"""join_server invite parsing.

The old optional-prefix regex captured the URL scheme ``https`` from
``https://discord.gg/<code>``, so every HTTPS invite joined discord.gg/https
instead of the server the user asked for.
"""

import asyncio
from types import SimpleNamespace

from bot_tools import (
    JoinServerTool,
    _extract_invite_code,
    _invite_raw_from_params,
)


def test_https_invite_url_does_not_extract_scheme():
    assert _extract_invite_code("https://discord.gg/coolserver") == "coolserver"
    assert _extract_invite_code("https://discord.gg/coolserver") != "https"


def test_extract_invite_code_common_shapes():
    assert _extract_invite_code("coolserver") == "coolserver"
    assert _extract_invite_code("discord.gg/coolserver") == "coolserver"
    assert _extract_invite_code("https://discord.com/invite/coolserver") == "coolserver"
    assert (
        _extract_invite_code("https://discordapp.com/invite/coolserver") == "coolserver"
    )
    assert _extract_invite_code("https://ptb.discord.com/invite/AbC_12") == "AbC_12"
    assert _extract_invite_code("https://canary.discord.com/invite/xyz") == "xyz"
    assert _extract_invite_code("<https://discord.gg/coolserver>") == "coolserver"
    assert (
        _extract_invite_code("https://discord.gg/coolserver?event=123") == "coolserver"
    )
    assert _extract_invite_code("join this: https://discord.gg/coolserver please") == (
        "coolserver"
    )
    assert _extract_invite_code("https://discord.gg/coolserver/") == "coolserver"


def test_extract_invite_code_rejects_non_invites():
    assert _extract_invite_code("") == ""
    assert _extract_invite_code("https://example.com/coolserver") == ""
    assert _extract_invite_code("not a link") == ""


def test_invite_raw_from_params_aliases():
    assert _invite_raw_from_params("https://discord.gg/a") == "https://discord.gg/a"
    assert _invite_raw_from_params(None, {"url": "https://discord.gg/b"}) == (
        "https://discord.gg/b"
    )
    assert _invite_raw_from_params("", {"code": "abc123"}) == "abc123"
    assert _invite_raw_from_params(["https://discord.gg/listed"]) == (
        "https://discord.gg/listed"
    )


def test_join_server_fetches_path_code_not_https():
    captured = {}

    class FakeInvite:
        guild = SimpleNamespace(
            name="Target",
            id=42,
            features=[],
            verification_level=SimpleNamespace(name="none"),
        )
        approximate_member_count = 10

        async def accept(self):
            captured["accepted"] = True

    class FakeBot:
        def _is_admin(self, _uid):
            return True

        async def fetch_invite(self, code, with_counts=True):
            captured["code"] = code
            return FakeInvite()

        def get_guild(self, gid):
            if captured.get("accepted"):
                return SimpleNamespace(
                    name="Target", id=gid, member_count=10, channels=[]
                )
            return None

        async def _auto_onboard(self, _guild, detail=False, **_kwargs):
            result = {"ok": False, "summary": "no onboarding", "prompts": []}
            return result if detail else result["summary"]

    message = SimpleNamespace(author=SimpleNamespace(id=1))

    async def run():
        tool = JoinServerTool(FakeBot())
        result = await tool.execute(
            message, invite="https://discord.gg/coolserver"
        )
        return result

    result = asyncio.run(run())
    assert captured["code"] == "coolserver"
    assert captured.get("accepted") is True
    assert "JOINED Target" in result
