#!/usr/bin/env python3
"""One-shot splitter: move built-in Tool classes out of bot_tools.py.

Run from the repo root. Safe to re-run only on the original bot_tools.py
(it refuses if tooling/helpers.py already exists).
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "bot_tools.py"
HELPERS = ROOT / "tooling" / "helpers.py"

# plugin_id -> (display, description, enable_attr or None, protected, tools)
# each tool: (class_name, runtime_name, returns_result, ends_turn, extra_flags)
PLUGIN_SPECS: list[dict] = [
    {
        "id": "images",
        "name": "Image Generation",
        "description": "Generate and edit images with the configured image provider.",
        "enable": "ENABLE_IMAGE_GEN",
        "permissions": ["network"],
        "tools": [
            ("ImageGeneratorTool", "image_generator", True, False, {}),
            ("HDImageGeneratorTool", "hd_image", True, False, {}),
        ],
    },
    {
        "id": "discord_messages",
        "name": "Discord Messaging",
        "description": "Send, edit, react, pin, poll, and otherwise talk in Discord.",
        "protected": True,
        "uninstallable": False,
        "permissions": ["messages.send", "messages.read"],
        "tools": [
            ("ReactTool", "react", False, False, {}),
            ("EditMessageTool", "edit_message", True, False, {}),
            ("DeleteMessageTool", "delete_message", False, False, {}),
            ("CreatePollTool", "create_poll", True, False, {"visible": True}),
            ("ForwardMessageTool", "forward_message", True, False, {}),
            ("TypingTool", "typing", False, False, {}),
            ("SendMessageTool", "send_message", False, True, {"visible": True}),
            ("SendFileTool", "send_file", True, False, {"visible": True}),
            ("PinMessageTool", "pin_message", True, False, {}),
            ("PurgeMessagesTool", "purge_messages", True, False, {}),
            ("SearchMessagesTool", "search_messages", True, False, {}),
            ("CreateInviteTool", "create_invite", True, False, {}),
            ("NoResponseTool", "no_response", False, True, {}),
        ],
    },
    {
        "id": "discord_presence",
        "name": "Discord Presence",
        "description": "Status, activity, nickname, and avatar.",
        "tools": [
            ("ChangePresenceTool", "change_presence", False, False, {}),
            ("SetActivityTool", "set_activity", True, False, {}),
            ("SetNicknameTool", "set_nickname", False, False, {}),
            ("ChangeAvatarTool", "change_avatar", True, False, {"enable": "ENABLE_AVATAR", "admin": True}),
        ],
    },
    {
        "id": "discord_guild",
        "name": "Discord Servers",
        "description": "List and manage servers, channels, roles, members, and invites.",
        "permissions": ["discord.channels"],
        "tools": [
            ("LeaveServerTool", "leave_server", True, False, {"admin": True}),
            ("LookupUserTool", "lookup_user", True, False, {}),
            ("ListServersTool", "list_servers", True, False, {}),
            ("ListAdminServersTool", "list_admin_servers", True, False, {}),
            ("ListChannelsTool", "list_channels", True, False, {}),
            ("ListRolesTool", "list_roles", True, False, {}),
            ("ListMembersTool", "list_members", True, False, {}),
            ("CreateCategoryTool", "create_category", True, False, {}),
            ("CreateChannelTool", "create_channel", True, False, {}),
            ("EditChannelTool", "edit_channel", True, False, {}),
            ("DeleteChannelTool", "delete_channel", True, False, {}),
            ("EditCategoryTool", "edit_category", True, False, {}),
            ("MoveChannelTool", "move_channel", True, False, {}),
            ("CloneChannelTool", "clone_channel", True, False, {}),
            ("SyncChannelTool", "sync_channel", True, False, {}),
            ("ListPermissionsTool", "list_permissions", True, False, {}),
            ("ManageInvitesTool", "manage_invites", True, False, {}),
            ("EditServerTool", "edit_server", True, False, {}),
            ("AuditLogTool", "audit_log", True, False, {}),
            ("ManageEmojiTool", "manage_emoji", True, False, {}),
        ],
    },
    {
        "id": "discord_moderation",
        "name": "Discord Moderation",
        "description": "Kick, ban, timeout, lock, and otherwise moderate a server.",
        "permissions": ["discord.moderate"],
        "tools": [
            ("KickMemberTool", "kick_member", True, False, {}),
            ("BanMemberTool", "ban_member", True, False, {}),
            ("UnbanMemberTool", "unban_member", True, False, {}),
            ("SoftbanMemberTool", "softban_member", True, False, {}),
            ("ListBansTool", "list_bans", True, False, {}),
            ("TimeoutMemberTool", "timeout_member", True, False, {}),
            ("ListTimeoutsTool", "list_timeouts", True, False, {}),
            ("ManageRoleTool", "manage_role", True, False, {}),
            ("VoiceModTool", "voice_mod", True, False, {}),
            ("LockChannelTool", "lock_channel", True, False, {}),
            ("LockdownTool", "lockdown", True, False, {}),
            ("SetChannelPermissionsTool", "set_channel_permissions", True, False, {}),
            ("SetMemberNicknameTool", "set_member_nickname", True, False, {}),
        ],
    },
    {
        "id": "runtime_controls",
        "name": "Runtime Controls",
        "description": "Sleep, wait, and leftover catalog compatibility tools.",
        "protected": True,
        "tools": [
            ("SleepTool", "sleep", False, True, {}),
            ("ClearSleepTool", "clear_sleep", False, False, {}),
            ("WaitTool", "wait", False, False, {}),
            ("MoreToolsTool", "more_tools", True, False, {}),
        ],
    },
    {
        "id": "sites",
        "name": "Generated Sites",
        "description": "Create, edit, host, and list generated websites.",
        "enable": "ENABLE_CREATE_SITE",
        "permissions": ["files.write", "network"],
        "tools": [
            ("CreateSiteTool", "create_site", True, False, {}),
            ("EditSiteTool", "edit_site", True, False, {}),
            ("DeleteSiteTool", "delete_site", True, False, {}),
            ("SiteServerTool", "site_server", True, False, {}),
            ("ListSitesTool", "list_sites", True, False, {}),
            ("HostFileTool", "host_file", True, False, {}),
        ],
    },
    {
        "id": "web",
        "name": "Web",
        "description": "Search the web and fetch URLs.",
        "permissions": ["network"],
        "tools": [
            ("WebSearchTool", "web_search", True, False, {"enable": "ENABLE_WEB_SEARCH"}),
            ("FetchUrlTool", "fetch_url", True, False, {"enable": "ENABLE_FETCH_URL"}),
        ],
    },
    {
        "id": "media",
        "name": "Media Understanding",
        "description": "Look at images and video, send memes and media.",
        "permissions": ["network"],
        "tools": [
            ("SeeImageTool", "see_image", True, False, {}),
            ("SeeVideoTool", "see_video", True, False, {}),
            ("SendMemeTool", "send_meme", True, False, {"visible": True}),
            ("SendMediaTool", "send_media", True, False, {"visible": True}),
        ],
    },
    {
        "id": "shell",
        "name": "Shell Sandbox",
        "description": "Run commands in the Docker shell sandbox.",
        "enable": "ENABLE_SHELL",
        "permissions": ["shell"],
        "tools": [
            ("ShellTool", "shell", True, False, {"destructive": True}),
        ],
    },
    {
        "id": "tts_voice",
        "name": "Voice and TTS",
        "description": "Text-to-speech and Discord voice-channel tools.",
        "permissions": ["voice"],
        "tools": [
            ("TtsTool", "tts", False, False, {"enable": "ENABLE_TTS", "visible": True}),
            ("JoinVcTool", "join_vc", True, False, {}),
            ("VcStatusTool", "vc_status", True, False, {}),
            ("VcWhereTool", "vc_where", True, False, {}),
            ("LeaveVcTool", "leave_vc", True, False, {}),
        ],
    },
    {
        "id": "inbox",
        "name": "Inbox",
        "description": "Owner inbox for inbound requests Maxwell cannot handle alone.",
        "tools": [
            ("InboxListTool", "inbox_list", True, False, {}),
            ("InboxActTool", "inbox_act", True, False, {}),
        ],
    },
    {
        "id": "email",
        "name": "Email",
        "description": "Send and read email through the local mail stack.",
        "enable": "ENABLE_EMAIL_TOOLS",
        "permissions": ["network"],
        "tools": [
            ("EmailSendTool", "email_send", True, False, {"destructive": True}),
            ("EmailReadInboxTool", "email_read_inbox", True, False, {"destructive": True}),
            ("EmailGetMessageTool", "email_get_message", True, False, {"destructive": True}),
            ("EmailSearchTool", "email_search", True, False, {"destructive": True}),
        ],
    },
    {
        "id": "personality",
        "name": "Personality and Server Prompts",
        "description": "Owner-editable personality and per-server instructions.",
        "permissions": ["config.modify"],
        "tools": [
            ("UpdateBasePersonalityTool", "update_base_personality", True, False, {"admin": True, "destructive": True}),
            ("UpdateServerPromptTool", "update_server_prompt", True, False, {"admin": True, "destructive": True}),
        ],
    },
    {
        "id": "chess",
        "name": "Chess",
        "description": "Play chess against Maxwell in a channel.",
        "optional_python": ["chess"],
        "tools": [
            ("ChessStartTool", "chess_start", True, False, {"chess": True}),
            ("ChessMoveTool", "chess_move", True, False, {"chess": True}),
            ("ChessStateTool", "chess_state", True, False, {"chess": True}),
            ("ChessResignTool", "chess_resign", True, False, {"chess": True}),
        ],
    },
    {
        "id": "diagnostics",
        "name": "Diagnostics",
        "description": "Usage, debug latency, and owner reports.",
        "tools": [
            ("UsageTool", "usage", True, False, {}),
            ("ReportTool", "report", True, False, {}),
            ("DebugTool", "debug", True, False, {}),
        ],
    },
    {
        "id": "plugin_admin",
        "name": "Plugin Administration",
        "description": "List, enable, and disable Maxwell plugins.",
        "protected": True,
        "uninstallable": False,
        "permissions": ["plugins.manage"],
        "tools": [
            ("ManagePluginTool", "manage_plugin", True, False, {}),
        ],
    },
]


IMPL_HEADER = '''"""Tool implementations for the {plugin_id} plugin.

Moved out of the historical bot_tools.py monolith. Shared helpers live in
``tooling.helpers``.
"""
from __future__ import annotations

from tooling import helpers as _helpers
from tools import Tool

# Mechanical split: the original classes used the bot_tools module globals.
# Bind every helper/name here so execute() bodies keep working unchanged.
for _name in dir(_helpers):
    if _name.startswith("__"):
        continue
    globals().setdefault(_name, getattr(_helpers, _name))
del _name
'''


SETUP_TEMPLATE = '''"""Register {display} tools with Maxwell."""
from __future__ import annotations

from typing import Any


def _flag(bot: Any, name: str | None) -> bool:
    if not name:
        return True
    cfg = getattr(bot, "config", None)
    if cfg is None:
        return True
    return bool(getattr(cfg, name, True))


def setup(bot, ctx=None):
    {extra_guard}
    from .impl import {imports}

    mapping = {mapping}
    tools = []
    for runtime_name, cls, enable_name in mapping:
        if not _flag(bot, enable_name):
            continue
        inst = cls(bot)
        inst.name = runtime_name
        inst.tool_name = runtime_name
        tools.append(inst)
    return tools
'''


def class_ranges(tree: ast.AST) -> dict[str, tuple[int, int]]:
    out: dict[str, tuple[int, int]] = {}
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            end = int(node.end_lineno or node.lineno)
            out[node.name] = (node.lineno, end)
    return out


def inject_attrs(source: str, class_name: str, runtime: str, flags: dict) -> str:
    """Insert tool_name / contract attributes after the class line."""
    lines = source.splitlines()
    # Find `class Foo`
    insert_at = None
    indent = "    "
    for i, line in enumerate(lines):
        if line.startswith(f"class {class_name}"):
            insert_at = i + 1
            break
    if insert_at is None:
        return source
    # Skip docstring
    j = insert_at
    if j < len(lines) and lines[j].strip().startswith(('"""', "'''")):
        quote = lines[j].strip()[:3]
        if lines[j].strip().count(quote) >= 2 and len(lines[j].strip()) > 3:
            j += 1
        else:
            j += 1
            while j < len(lines) and quote not in lines[j]:
                j += 1
            j += 1
    attrs = [
        f'{indent}tool_name = {runtime!r}',
        f'{indent}returns_result = {bool(flags.get("returns_result"))}',
        f'{indent}ends_turn = {bool(flags.get("ends_turn"))}',
    ]
    if flags.get("destructive"):
        attrs.append(f"{indent}is_destructive = True")
    if flags.get("admin"):
        attrs.append(f"{indent}requires_admin = True")
    if flags.get("visible"):
        attrs.append(f"{indent}produces_visible_output = True")
    # Don't duplicate if already present
    existing = "\n".join(lines[insert_at : insert_at + 12])
    keep = []
    for attr in attrs:
        key = attr.split("=", 1)[0].strip()
        if f"{key} =" in existing or f"{key}:" in existing:
            continue
        keep.append(attr)
    if not keep:
        return source
    lines[j:j] = keep + [""]
    return "\n".join(lines) + "\n"


def write_plugin(spec: dict, classes: dict[str, str]) -> None:
    plugin_id = spec["id"]
    directory = ROOT / "plugins" / plugin_id
    directory.mkdir(parents=True, exist_ok=True)
    parts = [IMPL_HEADER.format(plugin_id=plugin_id)]
    mapping = []
    imports = []
    tools_meta = []
    for class_name, runtime, returns_result, ends_turn, extra in spec["tools"]:
        src = classes[class_name]
        flags = {
            "returns_result": returns_result,
            "ends_turn": ends_turn,
            **extra,
        }
        parts.append(inject_attrs(src, class_name, runtime, flags).rstrip() + "\n")
        imports.append(class_name)
        enable = extra.get("enable") or spec.get("enable")
        mapping.append((runtime, class_name, enable))
        tools_meta.append(
            {
                "name": runtime,
                "returns_result": returns_result,
                "ends_turn": ends_turn,
                "is_destructive": bool(extra.get("destructive")),
                "requires_admin": bool(extra.get("admin")),
            }
        )
    (directory / "impl.py").write_text("\n".join(parts), encoding="utf-8")

    extra_guard = ""
    if spec["id"] == "chess":
        extra_guard = (
            "from tooling.helpers import __CHESS_IMPORTED__\n"
            "    if not __CHESS_IMPORTED__:\n"
            "        return []"
        )
    mapping_src = "[\n"
    for runtime, class_name, enable in mapping:
        mapping_src += f"        ({runtime!r}, {class_name}, {enable!r}),\n"
    mapping_src += "    ]"
    setup = SETUP_TEMPLATE.format(
        display=spec["name"],
        extra_guard=extra_guard or "pass",
        imports=", ".join(imports),
        mapping=mapping_src,
    )
    # fix extra_guard indentation if it's `pass`
    if extra_guard == "":
        setup = setup.replace("{extra_guard}", "pass")
    (directory / "__init__.py").write_text(setup, encoding="utf-8")

    manifest = {
        "id": plugin_id,
        "name": spec["name"],
        "version": "1.0.0",
        "description": spec["description"],
        "author": "Maxwell",
        "api_version": 1,
        "bundled": True,
        "enabled_globally": True,
        "enabled_by_default": True,
        "protected": bool(spec.get("protected")),
        "uninstallable": bool(spec.get("uninstallable", True)),
        "permissions": spec.get("permissions") or [],
        "optional_dependencies": spec.get("optional_python") or [],
        "platforms": ["discord", "telegram"],
        "tools": tools_meta,
    }
    (directory / "plugin.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )


def write_wrapper_plugin(
    plugin_id: str,
    display: str,
    description: str,
    setup_source: str,
    tools_meta: list[dict],
    **manifest_extra,
) -> None:
    directory = ROOT / "plugins" / plugin_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "__init__.py").write_text(setup_source, encoding="utf-8")
    manifest = {
        "id": plugin_id,
        "name": display,
        "version": "1.0.0",
        "description": description,
        "author": "Maxwell",
        "api_version": 1,
        "bundled": True,
        "enabled_globally": True,
        "enabled_by_default": True,
        "tools": tools_meta,
        **manifest_extra,
    }
    (directory / "plugin.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )


def main() -> None:
    if HELPERS.exists():
        raise SystemExit("tooling/helpers.py already exists; refusing to split twice")
    source = SRC.read_text(encoding="utf-8")
    tree = ast.parse(source)
    ranges = class_ranges(tree)
    lines = source.splitlines(keepends=True)

    wanted: dict[str, str] = {}
    extract_ranges: list[tuple[int, int]] = []
    for spec in PLUGIN_SPECS:
        for class_name, *_rest in spec["tools"]:
            if class_name not in ranges:
                raise SystemExit(f"missing class {class_name} in bot_tools.py")
            start, end = ranges[class_name]
            wanted[class_name] = "".join(lines[start - 1 : end])
            extract_ranges.append((start, end))

    extract_ranges.sort(reverse=True)
    helper_lines = list(lines)
    for start, end in extract_ranges:
        helper_lines[start - 1 : end] = [
            f"# {lines[start - 1].rstrip()}  — moved to a plugin\n"
        ]

    HELPERS.parent.mkdir(parents=True, exist_ok=True)
    helper_text = "".join(helper_lines)
    helper_text = helper_text.replace(
        '"""Tools for Maxwell Bot\n',
        '"""Shared helpers for Maxwell tools.\n\n'
        "Tool classes live in ``plugins/*/impl.py``. This module keeps the\n"
        "helpers, private bases, and constants those classes share.\n",
        1,
    )
    HELPERS.write_text(helper_text, encoding="utf-8")
    (HELPERS.parent / "__init__.py").write_text(
        '"""Shared tool helpers. Feature implementations live in plugins/."""\n',
        encoding="utf-8",
    )

    class_exports: list[str] = []
    for spec in PLUGIN_SPECS:
        write_plugin(spec, wanted)
        for class_name, *_rest in spec["tools"]:
            class_exports.append(
                f"from plugins.{spec['id']}.impl import {class_name}  # noqa: F401"
            )

    write_wrapper_plugin(
        "discord_threads",
        "Discord Threads",
        "Create and control Discord threads Maxwell can work in.",
        '''"""Register Discord thread tools."""
from discord_threads import CreateThreadTool, ThreadControlTool


def setup(bot, ctx=None):
    start = CreateThreadTool(bot)
    start.name = "create_thread"
    start.tool_name = "create_thread"
    start.returns_result = True
    control = ThreadControlTool(bot)
    control.name = "thread_control"
    control.tool_name = "thread_control"
    control.returns_result = True
    return [start, control]
''',
        [
            {"name": "create_thread", "returns_result": True},
            {"name": "thread_control", "returns_result": True},
        ],
        protected=False,
        permissions=["discord.channels"],
    )

    write_wrapper_plugin(
        "background_jobs",
        "Background Jobs",
        "Spawn detached background coding/research jobs.",
        '''"""Register the spawn_background tool."""
from jobs import SpawnBackgroundTool


def setup(bot, ctx=None):
    tool = SpawnBackgroundTool(bot)
    tool.name = "spawn_background"
    tool.tool_name = "spawn_background"
    tool.returns_result = True
    return [tool]
''',
        [{"name": "spawn_background", "returns_result": True}],
        protected=True,
        permissions=["jobs.run"],
    )

    reexport = '''"""Compatibility re-exports for Maxwell tools.

Implementations live in ``plugins/*/impl.py``. Shared helpers live in
``tooling.helpers``. Existing ``from bot_tools import X`` imports keep
working; new code should import from the owning plugin.
"""

from tooling import helpers as _helpers
from tooling.helpers import *  # noqa: F401,F403

{class_exports}

from jobs import SpawnBackgroundTool  # noqa: F401
from discord_threads import CreateThreadTool, ThreadControlTool  # noqa: F401


def __getattr__(name: str):
    return getattr(_helpers, name)
'''.format(class_exports="\n".join(class_exports))
    SRC.write_text(reexport, encoding="utf-8")
    print(f"split {len(wanted)} tool classes into {len(PLUGIN_SPECS)} plugins")


if __name__ == "__main__":
    main()
