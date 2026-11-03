#!/usr/bin/env python3
"""Measure a synthetic Discord turn without starting the bot or contacting services.

Run: python scripts/prompt_size_report.py [--repo /path/to/checkout]
The same script can target another checkout; its Python dependencies must be
installed in the interpreter running this script. No checkout configuration,
operator data, environment variables, or credentials are used. The tool set is
an explicitly enabled, representative selection of bundled plugins, not an
inventory of any installation. Numbers use Unicode characters, not wire bytes;
chars/4 is a rough estimate, not provider tokenization or billing.
"""

import argparse
import asyncio
import importlib
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
from types import SimpleNamespace


# These bundled setups only construct tools; they do not start plugin services.
# Keep this list the same when comparing checkouts.
BUNDLED_PLUGINS = (
    "discord_messages", "discord_guild", "discord_moderation", "discord_presence",
    "discord_threads", "web", "shell", "sites", "images", "media", "diagnostics",
    "runtime_controls", "chess", "checkers", "tts_voice", "plugin_admin",
)
CONTENT = "Can you look into this and help me decide what to do next?"


class Memory:
    async def get_channel_memory(self, channel_id, *, requester=None):
        return [
            {"author": "Alice", "author_id": "456", "content": "I have a question about a project.", "timestamp": "2026-01-01T12:00:00+00:00"},
            {"author": "Maxwell", "author_id": "1", "content": "Sure, tell me more.", "timestamp": "2026-01-01T12:01:00+00:00"},
        ]


def deny_network():
    """Block outgoing connections, including accidental plugin initialization."""
    original = socket.socket

    class OfflineSocket(original):
        def connect(self, *args, **kwargs):
            raise RuntimeError("network access is disabled by prompt_size_report")

        connect_ex = connect
        sendto = connect

    def denied(*args, **kwargs):
        raise RuntimeError("network access is disabled by prompt_size_report")

    socket.socket = OfflineSocket
    socket.create_connection = denied
    socket.getaddrinfo = denied


def make_bot(MaxwellBot, ToolCircuitBreaker, temp):
    # A test-style host: no Discord client, persistent memory, plugin manager,
    # services, or provider is started. Bind the production methods exercised by
    # prompt assembly and schema selection to this small host.
    config = SimpleNamespace(DATA_DIR=str(temp), CUSTOM_TOOL_CALLS=False)
    for flag in ("ENABLE_WEB_SEARCH", "ENABLE_FETCH_URL", "ENABLE_SHELL",
                 "ENABLE_CREATE_SITE", "ENABLE_IMAGE_GEN", "ENABLE_HD_IMAGE",
                 "ENABLE_IMAGE_INPUT", "ENABLE_VIDEO_INPUT", "ENABLE_AVATAR"):
        setattr(config, flag, True)
    bot = SimpleNamespace(
        config=config, tools={}, plugin_manager=None, prompts=None,
        _tool_breaker=ToolCircuitBreaker(failure_threshold=999, recovery_seconds=0),
        _control={"base_personality": "Friendly and helpful", "cross_context_enabled": False,
                  "emoji_context_enabled": False, "long_term_memory_enabled": False,
                  "memory_context_budget": 30000, "memory_history_messages": 20,
                  "music_context_enabled": False, "tools_enabled": True,
                  "native_tool_calls": True, "subagents_require_premium": False},
        _drugged_until={}, _guild_emojis={}, _recent_users={}, _conversation_watch={},
        bot_name="Maxwell", memory=Memory(),
        user=SimpleNamespace(display_name="Maxwell", name="Maxwell", id=1, bot=True),
        _is_admin=lambda uid: uid == 456,
    )
    for name in (
        "_reply_parent", "_replying_to_own_message", "_render_reply_parent",
        "_author_is_self", "_iter_resolved_reply_chain", "_reply_parent_context_lines",
        "_directly_addressed", "_conversation_watch_active", "_is_short_live_turn",
        "_tool_system_prompt", "_native_tools_enabled",
    ):
        setattr(bot, name, getattr(MaxwellBot, name).__get__(bot))
    for plugin in BUNDLED_PLUGINS:
        module = importlib.import_module(f"plugins.{plugin}")
        for tool in module.setup(bot):
            name = tool.get_name()
            if name in bot.tools:
                raise RuntimeError(f"duplicate tool name: {name}")
            bot.tools[name] = tool
    # Construct the actual music/extras schema objects without invoking plugin
    # setup: that would start services, register jobs and open persistent stores.
    # Their service/store references are never used during prompt construction.
    from plugins.music.tools import MusicControlTool, MusicInfoTool
    from plugins.maxwell_extras.task_workers import DelegateTaskTool
    from plugins.maxwell_extras.task_jobs import ManageTasksTool
    # Settings schemas import UI modules that otherwise import the credential
    # vault. That subsystem is deliberately unavailable in this offline fixture.
    from types import ModuleType
    byok = ModuleType("plugins.maxwell_extras.byok")
    byok.PROVIDERS = {}
    def no_credentials(*args, **kwargs):
        raise RuntimeError("Credential operations are disabled in this report")
    byok.effective_settings = byok.make_request_provider = byok.parse_model_support = no_credentials
    sys.modules[byok.__name__] = byok
    from plugins.maxwell_extras.config_tools import ConfigureTool, GetConfigurationTool, OpenConfigurationTool
    from plugins.maxwell_extras.tools import ReminderTool, SendRichMessageTool, InspectMediaUrlTool
    from plugins.maxwell_extras.updates import MaxwellUpdatesTool

    extra_tools = (
        MusicControlTool(bot, None), MusicInfoTool(bot, None),
        DelegateTaskTool(bot), ManageTasksTool(bot),
        ConfigureTool(bot), GetConfigurationTool(bot), OpenConfigurationTool(bot),
        ReminderTool(bot, None), SendRichMessageTool(bot), InspectMediaUrlTool(bot),
        MaxwellUpdatesTool(bot, root=temp),
    )
    for tool in extra_tools:
        name = tool.get_name()
        if name in bot.tools:
            raise RuntimeError(f"duplicate tool name: {name}")
        bot.tools[name] = tool
    return bot


def make_message(discord, kind):
    admin = kind == "guild admin"
    author = SimpleNamespace(id=456 if kind == "DM owner/admin" else 457,
                             display_name="Alice", name="Alice", bot=False,
                             guild_permissions=discord.Permissions(administrator=admin), roles=[])
    if kind == "DM owner/admin":
        # A real DMChannel type, with synthetic state: type checks distinguish DMs.
        channel = object.__new__(discord.DMChannel)
        channel.id = 123
        guild = None
    else:
        me = SimpleNamespace(id=1, display_name="Maxwell", name="Maxwell", nick=None,
                             guild_permissions=discord.Permissions(administrator=True), roles=[])
        channel = SimpleNamespace(id=123, name="general", permissions_for=lambda member: member.guild_permissions)
        guild = SimpleNamespace(id=789, name="Example server", me=me, owner_id=999,
                                channels=[channel], roles=[], members=[author, me],
                                get_member=lambda uid: {author.id: author, me.id: me}.get(uid))
        channel.guild = guild
    return SimpleNamespace(id=1000, author=author, channel=channel, guild=guild,
                           content=CONTENT, mentions=[], reference=None, attachments=[])


def size(label, value):
    count = len(value)
    print(f"  {label:28s} {count:7,d} chars  {count / 4:9,.1f} estimated tokens")


async def report(MaxwellBot, bot, discord):
    print("Synthetic fixture: bundled tool selection; native calls enabled; empty plugin manager;")
    print("Includes music, worker and configuration tools; no plugin prompt components or services.")
    print("two synthetic history messages; no media or retrieved memory. Estimates are chars/4.")
    print(f"Registered fixture tools: {len(bot.tools)}")
    for kind in ("DM owner/admin", "guild member non-admin", "guild admin"):
        message = make_message(discord, kind)
        # Capture the actual tool header passed into the real builder, including
        # any turn-specific additions, without rebuilding it independently.
        real_header = bot._tool_system_prompt
        captured = []

        def capture(*args, _header=real_header, _captured=captured, **kwargs):
            header = _header(*args, **kwargs)
            _captured.append(header)
            return header

        bot._tool_system_prompt = capture
        try:
            messages = await MaxwellBot._build_messages(bot, message, CONTENT)
        finally:
            bot._tool_system_prompt = real_header
        if len(captured) != 1 or not messages or messages[0].get("role") != "system":
            raise RuntimeError("unexpected conversation prompt shape")
        system = str(messages[0].get("content") or "")
        dynamic = "\n\n".join(str(m.get("content") or "") for m in messages[1:] if m.get("role") == "system")
        tools = MaxwellBot._build_openai_tools(bot, message=message, content=CONTENT)
        payload = json.dumps(tools, ensure_ascii=False, separators=(",", ":"))
        print(f"\n{kind} ({len(tools)} native tools)")
        size("leading system message", system)
        size("tool header (included above)", captured[0])
        size("dynamic system block", dynamic)
        size("native tools JSON payload", payload)
        size("system + dynamic + tools", system + dynamic + payload)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1],
                        help="checkout to measure (default: this script's checkout)")
    args = parser.parse_args()
    repo = args.repo.resolve()
    if not (repo / "bot.py").is_file():
        parser.error(f"not a Maxwell checkout: {repo}")
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(repo))
    with tempfile.TemporaryDirectory(prefix=".prompt-report-", dir=repo) as tmp:
        temp = Path(tmp)
        # config.py loads MAXWELL_ENV_FILE at import; point it at a nonexistent
        # temporary path and discard all inherited environment, including secrets.
        os.environ.clear()
        os.environ.update({"HOME": tmp, "MAXWELL_ENV_FILE": str(temp / "no-env"),
                           "DATA_DIR": tmp, "LOG_LEVEL": "ERROR"})
        os.chdir(tmp)
        deny_network()
        import discord
        if not hasattr(discord, "ui"):
            # Only class declarations are imported; UI objects are never used.
            # Some test environments have discord.py-self alongside discord.py,
            # whose incompatible UI package cannot be imported.
            class UnusedUI:
                def __init__(self, *args, **kwargs):
                    raise RuntimeError("Discord UI is unavailable in this report")

                def __init_subclass__(cls, **kwargs):
                    super().__init_subclass__()

            discord.ui = SimpleNamespace(View=UnusedUI, Button=UnusedUI,
                                         Modal=UnusedUI, Select=UnusedUI,
                                         ChannelSelect=UnusedUI, TextInput=UnusedUI)
        from bot import MaxwellBot, ToolCircuitBreaker

        bot = make_bot(MaxwellBot, ToolCircuitBreaker, temp)
        asyncio.run(report(MaxwellBot, bot, discord))


if __name__ == "__main__":
    main()
