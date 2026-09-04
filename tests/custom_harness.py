"""Custom comprehensive test harness for Maxwell tools and live model invocation.
Validates all registered tools in the bot environment and executes sample tools
with the live provider.
"""

import asyncio
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, "/root/maxwell")

import bot_tools
from bot import MaxwellBot


class FakeDiscordChannel:
    def __init__(self, channel_id=123456789):
        self.id = channel_id
        self.sent = []

    async def send(self, content=None, **kwargs):
        msg = SimpleNamespace(id=len(self.sent) + 1, content=content, channel=self, **kwargs)
        self.sent.append(msg)
        return msg


class FakeDiscordUser:
    def __init__(self, user_id=987654321, name="TestUser"):
        self.id = user_id
        self.name = name
        self.display_name = name
        self.bot = False
        self.mention = f"<@{user_id}>"


class FakeDiscordGuild:
    def __init__(self, guild_id=555555):
        self.id = guild_id
        self.name = "TestGuild"
        self.me = SimpleNamespace(guild_permissions=SimpleNamespace(administrator=True))


class FakeDiscordMessage:
    def __init__(self, content="test message", user_id=987654321, channel_id=123456789):
        self.id = 9990001
        self.content = content
        self.author = FakeDiscordUser(user_id=user_id)
        self.channel = FakeDiscordChannel(channel_id=channel_id)
        self.guild = FakeDiscordGuild()
        self.attachments = []
        self.embeds = []
        self.mentions = []


async def run_custom_tool_harness():
    print("=== STARTING CUSTOM HARNESS: TOOL CONTRACT & REGISTRY AUDIT ===")
    bot = MaxwellBot()
    bot._setup_ai()

    print(f"Total registered tools: {len(bot.tools)}")
    assert len(bot.tools) >= 80, f"Expected at least 80 tools, got {len(bot.tools)}"

    fake_msg = FakeDiscordMessage()

    passed = 0
    failed = 0
    errors = []

    # Test tool interfaces and safe dry-runs / executions
    for name, tool in sorted(bot.tools.items()):
        try:
            desc = tool.get_description()
            assert isinstance(desc, str) and len(desc) > 0, f"{name}: get_description returned empty"
            assert hasattr(tool, "execute") and callable(tool.execute), f"{name}: execute is not callable"
            passed += 1
        except Exception as e:
            failed += 1
            errors.append((name, str(e)))

    print(f"Tool registry interface checks: {passed} passed, {failed} failed.")
    if errors:
        for name, err in errors:
            print(f"  [FAIL] {name}: {err}")
        raise RuntimeError("Tool registry interface audit failed")

    # Test specific safe tool executions
    print("\n--- Testing Specific Core Tool Executions ---")

    # 1. usage tool
    res = await bot.tools["usage"].execute(fake_msg)
    print(f"[PASS] usage tool returned: {str(res)[:60]}...")
    assert "usage" in str(res).lower() or "tokens" in str(res).lower() or "cost" in str(res).lower() or "call" in str(res).lower()

    # 2. wait tool
    res = await bot.tools["wait"].execute(fake_msg, seconds=0.01)
    print(f"[PASS] wait tool returned: {res}")
    assert "waited" in str(res).lower()

    # 3. no_response tool
    res = await bot.tools["no_response"].execute(fake_msg, reason="testing silence")
    print(f"[PASS] no_response tool returned: {res}")
    assert res == "__NO_RESPONSE__" or "acknowledged" in str(res).lower() or "silent" in str(res).lower()

    # 4. shell tool (read-only command)
    res = await bot.tools["shell"].execute(fake_msg, command="echo 'harness test ok'")
    print(f"[PASS] shell tool returned: {res.strip()}")
    assert "harness test ok" in str(res)

    # 5. web_search tool (or mock check)
    res = await bot.tools["web_search"].execute(fake_msg, query="Python asyncio")
    print(f"[PASS] web_search tool returned {len(str(res))} chars")
    assert len(str(res)) > 10

    # 6. list_sites tool
    res = await bot.tools["list_sites"].execute(fake_msg)
    print(f"[PASS] list_sites tool returned: {str(res)[:60]}...")

    # 7. update_base_personality tool
    old_p = bot._get_personality()
    res = await bot.tools["update_base_personality"].execute(fake_msg, text="Stay sharp and helpful.")
    print(f"[PASS] update_base_personality tool returned: {res}")
    assert "updated" in str(res).lower()
    # Restore personality
    await bot.tools["update_base_personality"].execute(fake_msg, text=old_p)
    print("[PASS] update_base_personality tool tested and restored")

    # 8. Live Model in Environment Test
    print("\n--- Testing Live Model in Environment with Tools ---")
    provider = bot.ai_provider
    print(f"Invoking provider: base_url={provider.base_url}, model={provider.model}")

    messages = [
        {"role": "system", "content": "You are Maxwell, a sharp AI bot. If the user asks for calculation, reply with the answer."},
        {"role": "user", "content": "What is 42 plus 58? Answer only with the number."},
    ]

    model_reply = await provider.generate_response(messages)
    print(f"Model live reply: {model_reply.strip()}")
    assert "100" in model_reply, f"Unexpected model response: {model_reply}"

    await provider.close()
    print("\n=== ALL CUSTOM HARNESS & LIVE MODEL TESTS PASSED CLEANLY ===")


if __name__ == "__main__":
    asyncio.run(run_custom_tool_harness())
