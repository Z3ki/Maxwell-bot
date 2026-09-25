"""Purpose-specific Discord application commands for Maxwell."""

from __future__ import annotations

import inspect
import logging
from types import SimpleNamespace
from typing import Any

import user_install as ui

logger = logging.getLogger(__name__)
_ACTIVE_STORE: Any = None

_APP_META = {"type": 1, "integration_types": [0, 1], "contexts": [0, 1, 2]}
_ACTION_CHOICES = [
    {"name": "View", "value": "view"},
    {"name": "Set", "value": "set"},
    {"name": "Reset", "value": "reset"},
]
_SCOPE_CHOICES = [
    {"name": "My personal defaults", "value": "personal"},
    {"name": "This server", "value": "server"},
]
_KEY_CHOICES = [
    {"name": "Response mode", "value": "mode"},
    {"name": "Web research", "value": "web"},
    {"name": "Answer detail", "value": "detail"},
    {"name": "Channel context", "value": "context"},
    {"name": "Response language", "value": "language"},
    {"name": "Tool progress", "value": "progress"},
    {"name": "Ticket greetings", "value": "ticket"},
    {"name": "Server instructions", "value": "personality"},
]
_PERSONAL_DEFAULT_VALUES = {
    "mode": {"ask", "research", "summarize", "explain", "rewrite", "translate", "brainstorm", "code"},
    "web": {"auto", "search", "off"},
    "detail": {"quick", "balanced", "deep"},
    "context": {"0", "10", "25", "50"},
}
_PROMPT_INTENTS = {
    "image": (
        "Create an image for the user's request. Use the available image-generation tool "
        "when appropriate, and tell the user plainly if image generation is unavailable."
    ),
    "chess": (
        "Handle this as a chess request. Use the chess game tools for game state and moves; "
        "do not claim a move or game succeeded unless the tool confirms it."
    ),
    "checkers": (
        "Handle this as a checkers request. Use the checkers game tools for game state and moves; "
        "do not claim a move or game succeeded unless the tool confirms it."
    ),
    "moderation": (
        "Handle this as a Discord moderation request. Use only moderation tools the requester "
        "is authorized to use in this server, and confirm the actual tool result."
    ),
    "memory": (
        "Handle this as a memory request. Respect the current user's and server's memory scope; "
        "do not claim that information was saved or deleted unless a tool confirms it."
    ),
    "reminder": (
        "Handle this as a reminder request. Create, inspect, or cancel only the reminder the "
        "user asks for, and confirm the tool result."
    ),
}

CONFIG_COMMAND = {
    "name": "config",
    "description": "View or update your defaults or this server's settings.",
    **_APP_META,
    "options": [
        {
            "name": "action",
            "description": "View, change, or reset a setting",
            "type": 3,
            "required": False,
            "choices": _ACTION_CHOICES,
        },
        {
            "name": "scope",
            "description": "Personal defaults or settings for this server",
            "type": 3,
            "required": False,
            "choices": _SCOPE_CHOICES,
        },
        {
            "name": "key",
            "description": "Setting to view, change, or reset",
            "type": 3,
            "required": False,
            "choices": _KEY_CHOICES,
        },
        {
            "name": "value",
            "description": "New setting value",
            "type": 3,
            "required": False,
            "max_length": 4000,
        },
    ],
}

PERSONALITY_COMMAND = {
    "name": "personality",
    "description": "Set a personal style preference for Maxwell's replies.",
    **_APP_META,
    "options": [
        {
            "name": "action",
            "description": "View, set, or reset your personal style",
            "type": 3,
            "required": False,
            "choices": _ACTION_CHOICES,
        },
        {
            "name": "text",
            "description": "A short style preference (up to 800 characters)",
            "type": 3,
            "required": False,
            "max_length": 800,
        },
    ],
}


def _prompt_command(
    name: str,
    description: str,
    *,
    guild_only: bool = False,
    allow_image: bool = False,
) -> dict[str, Any]:
    meta = (
        {"integration_types": [0], "contexts": [0]}
        if guild_only
        else {"integration_types": [0, 1], "contexts": [0, 1, 2]}
    )
    options = [
        {
            "name": "prompt",
            "description": "What you want Maxwell to do",
            "type": 3,
            "required": True,
            "max_length": 4000,
        }
    ]
    if allow_image:
        options.append(
            {
                "name": "image",
                "description": "Optional image to edit or use as a reference",
                "type": 11,
                "required": False,
            }
        )
    return {
        "name": name,
        "description": description,
        "type": 1,
        **meta,
        "options": options,
    }


PROMPT_COMMANDS = [
    _prompt_command(
        "image", "Create or edit an image from your request.", allow_image=True
    ),
    _prompt_command("chess", "Start a chess game or make a chess move."),
    _prompt_command("checkers", "Start a checkers game or make a move."),
    _prompt_command("moderation", "Ask Maxwell to help with server moderation.", guild_only=True),
    _prompt_command("memory", "Ask Maxwell to recall, save, or manage scoped memory."),
    _prompt_command("reminder", "Create, inspect, or cancel one of your reminders."),
]

# Useful prefix-command behavior is exposed through discoverable slash commands.
# Retired detached-agent, shell, and confirmation commands are intentionally absent.
_LEGACY_SLASH_COMMANDS = {
    "stop": ("stop", "Stop a running response in this channel.", True),
    "jobs": ("jobs", "List your current Maxwell jobs.", False),
    "job": ("job", "Inspect or cancel one of your jobs.", True),
    "server-prompt": ("prompt", "View or set this server's Maxwell instructions.", True),
    "clear-server-prompt": ("clearprompt", "Clear this server's Maxwell instructions.", False),
    "clear-memory": ("clearmem", "Clear this channel's stored conversation memory.", False),
    "downvote": ("downvote", "Mark a recent reply as unhelpful.", True),
    "negative-memory": ("neg", "Manage a negative memory record.", True),
    "summarize-memory": ("summarize", "Summarize recent messages into long-term memory.", True),
    "context": ("context", "Inspect or manage this channel's context.", True),
    "rem": ("rem", "Inspect or run the REM maintenance process.", True),
    "autonomy": ("autonomy", "View or update autonomous server behavior.", True),
    "sleep": ("sleep", "Pause Maxwell's replies for a short period.", True),
    "wake": ("wake", "End Maxwell's sleep window.", False),
    "progress": ("progress", "Set this server's tool-progress message setting.", True),
    "ticket-greetings": ("ticket", "Set ticket-channel greetings for this server.", True),
    "admin": ("admin", "Manage Maxwell's configured operator list.", True),
    "solo": ("solo", "Restrict Maxwell's responses to one server channel.", True),
    "voice": ("vc", "Control Maxwell's voice connection and speech.", True),
    "plugins": ("plugin", "List or enable and disable available plugins.", True),
    "blacklist": ("blacklist", "Manage Maxwell's user blacklist.", True),
    "unblacklist": ("unblacklist", "Remove a user from Maxwell's blacklist.", True),
    "debug": ("debug", "View restricted runtime diagnostics.", False),
}


def _legacy_command_definitions() -> list[dict[str, Any]]:
    rows = []
    for name, (_target, description, has_argument) in _LEGACY_SLASH_COMMANDS.items():
        row: dict[str, Any] = {"name": name, "description": description, **_APP_META}
        if has_argument:
            row["options"] = [
                {
                    "name": "arguments",
                    "description": "Command arguments",
                    "type": 3,
                    "required": False,
                    "max_length": 1000,
                }
            ]
        rows.append(row)
    return rows


def command_definitions() -> list[dict[str, Any]]:
    return [CONFIG_COMMAND, PERSONALITY_COMMAND, *PROMPT_COMMANDS, *_legacy_command_definitions()]


def _options(interaction: Any) -> dict[str, Any]:
    data = ui._interaction_data(interaction)
    return dict(ui._option_pairs(data.get("options")))


def _user_id(interaction: Any) -> str:
    user = getattr(interaction, "user", None)
    return str(getattr(user, "id", "") or "")


def _guild_id(interaction: Any) -> str:
    guild = getattr(interaction, "guild", None)
    guild_id = getattr(guild, "id", None) or getattr(interaction, "guild_id", None)
    return str(guild_id or "")


def _can_manage_server(bot: Any, interaction: Any) -> bool:
    guild_id = _guild_id(interaction)
    if not guild_id:
        return False
    guild = getattr(interaction, "guild", None)
    user = getattr(interaction, "user", None)
    if str(getattr(guild, "owner_id", "")) == _user_id(interaction):
        return True
    permissions = (
        getattr(getattr(interaction, "member", None), "guild_permissions", None)
        or getattr(user, "guild_permissions", None)
        or getattr(interaction, "permissions", None)
    )
    if any(
        bool(getattr(permissions, key, False))
        for key in ("administrator", "manage_guild")
    ):
        return True
    checker = getattr(bot, "_is_admin", None)
    return bool(callable(checker) and checker(getattr(user, "id", None)))


async def _send(interaction: Any, text: str) -> None:
    await ui._ephemeral(interaction, str(text)[:1900])


def _preference_store(bot: Any) -> Any:
    return getattr(bot, "_user_preferences", None)


def _render_defaults(defaults: dict[str, Any]) -> str:
    labels = {
        "mode": "Mode",
        "web": "Web research",
        "detail": "Detail",
        "context": "Channel context",
        "language": "Language",
    }
    return "\n".join(f"{labels[key]}: **{defaults.get(key)}**" for key in labels)


async def _handle_config(bot: Any, interaction: Any) -> bool:
    data = ui._interaction_data(interaction)
    if str(data.get("name") or "") != "config":
        return False
    store = _preference_store(bot)
    if store is None:
        await _send(interaction, "Personal settings are unavailable right now.")
        return True
    opts = _options(interaction)
    action = str(opts.get("action") or "view").strip().lower()
    scope = str(opts.get("scope") or "personal").strip().lower()
    key = str(opts.get("key") or "").strip().lower()
    value = str(opts.get("value") or "").strip()
    uid = _user_id(interaction)

    if scope == "personal":
        current = store.get(uid)
        if action == "view":
            await _send(
                interaction,
                "Your personal defaults:\n"
                + _render_defaults(current["defaults"])
                + "\nStyle preference: **"
                + (current["personality"] or "none set")
                + "**",
            )
            return True
        if key == "personality":
            if action == "reset":
                store.set_personality(uid, "")
                await _send(interaction, "Reset your personal style preference.")
            elif action == "set":
                try:
                    store.set_personality(uid, value)
                except ValueError as exc:
                    await _send(interaction, str(exc))
                else:
                    await _send(interaction, "Saved your personal style preference.")
            else:
                await _send(interaction, "For personal style, use `/personality` or choose action `set`/`reset`.")
            return True
        if key not in {"mode", "web", "detail", "context", "language"}:
            await _send(interaction, "Choose a personal setting in `key`.")
            return True
        if action == "reset":
            store.reset_default(uid, key)
            await _send(interaction, f"Reset your **{key}** default.")
            return True
        if action != "set":
            await _send(interaction, "Choose `view`, `set`, or `reset`.")
            return True
        if key in _PERSONAL_DEFAULT_VALUES and value.lower() not in _PERSONAL_DEFAULT_VALUES[key]:
            allowed = ", ".join(sorted(_PERSONAL_DEFAULT_VALUES[key]))
            await _send(interaction, f"For `{key}`, choose one of: {allowed}.")
            return True
        if key == "context":
            store.set_default(uid, key, int(value))
        elif key == "language":
            if len(value) > 80:
                await _send(interaction, "Language preference is limited to 80 characters.")
                return True
            store.set_default(uid, key, value)
        else:
            store.set_default(uid, key, value.lower())
        await _send(interaction, f"Saved your personal **{key}** default.")
        return True

    if scope != "server":
        await _send(interaction, "Choose `personal` or `server` for `scope`.")
        return True
    guild_id = _guild_id(interaction)
    if not guild_id:
        await _send(interaction, "Server settings are only available inside a server.")
        return True
    if not _can_manage_server(bot, interaction):
        await _send(interaction, "Server settings require the server owner, a Manage Server administrator, or a configured Maxwell admin.")
        return True

    if key == "progress":
        enabled = bool(getattr(bot, "_progress_enabled", lambda _gid: False)(guild_id))
        if action == "view":
            await _send(interaction, f"Tool progress messages are **{'on' if enabled else 'off'}** in this server.")
            return True
        if action == "reset":
            getattr(bot, "_progress_servers", set()).discard(guild_id)
            getattr(bot, "_progress_servers_off", set()).discard(guild_id)
            saver = getattr(bot, "_save_progress_servers", None)
            if callable(saver):
                saver()
            await _send(interaction, "Reset tool-progress messages to the configured default.")
            return True
        if action == "set" and value.lower() in {"on", "off"}:
            enabled_set = getattr(bot, "_progress_servers", None)
            disabled_set = getattr(bot, "_progress_servers_off", None)
            if not isinstance(enabled_set, set):
                enabled_set = bot._progress_servers = set()
            if not isinstance(disabled_set, set):
                disabled_set = bot._progress_servers_off = set()
            if value.lower() == "on":
                enabled_set.add(guild_id)
                disabled_set.discard(guild_id)
            else:
                enabled_set.discard(guild_id)
                disabled_set.add(guild_id)
            saver = getattr(bot, "_save_progress_servers", None)
            if callable(saver):
                saver()
            await _send(interaction, f"Tool progress messages are now **{value.lower()}** in this server.")
            return True
        await _send(interaction, "For `progress`, use action `set` with value `on` or `off`.")
        return True

    if key == "ticket":
        enabled = bool(getattr(bot, "_ticket_greeting_enabled", lambda _gid: False)(guild_id))
        if action == "view":
            await _send(interaction, f"Ticket greetings are **{'on' if enabled else 'off'}** in this server.")
            return True
        if action == "reset":
            getattr(bot, "_ticket_greeting_servers", set()).discard(guild_id)
            saver = getattr(bot, "_save_ticket_greeting_servers", None)
            if callable(saver):
                saver()
            await _send(interaction, "Reset ticket greetings to the configured default.")
            return True
        if action == "set" and value.lower() in {"on", "off"}:
            enabled_set = getattr(bot, "_ticket_greeting_servers", None)
            if not isinstance(enabled_set, set):
                enabled_set = bot._ticket_greeting_servers = set()
            if value.lower() == "on":
                enabled_set.add(guild_id)
            else:
                enabled_set.discard(guild_id)
            saver = getattr(bot, "_save_ticket_greeting_servers", None)
            if callable(saver):
                saver()
            await _send(interaction, f"Ticket greetings are now **{value.lower()}** in this server.")
            return True
        await _send(interaction, "For `ticket`, use action `set` with value `on` or `off`.")
        return True

    if key == "personality":
        memory = getattr(bot, "memory", None)
        if memory is None:
            await _send(interaction, "Server instructions are unavailable right now.")
            return True
        if action == "view":
            current = memory.get_server_prompt(guild_id) or ""
            await _send(interaction, f"Server instructions:\n{current or '(none set)'}")
            return True
        if action == "reset":
            memory.clear_server_prompt(guild_id)
            await _send(interaction, "Cleared this server's custom instructions.")
            return True
        if action == "set":
            if not value or len(value) > 4000:
                await _send(interaction, "Server instructions must contain 1–4000 characters.")
                return True
            memory.set_server_prompt(guild_id, value)
            await _send(interaction, "Saved this server's custom instructions.")
            return True
    await _send(interaction, "Choose a supported server setting in `key`.")
    return True


async def _handle_personality(bot: Any, interaction: Any) -> bool:
    data = ui._interaction_data(interaction)
    if str(data.get("name") or "") != "personality":
        return False
    store = _preference_store(bot)
    if store is None:
        await _send(interaction, "Personal style preferences are unavailable right now.")
        return True
    opts = _options(interaction)
    action = str(opts.get("action") or "view").strip().lower()
    uid = _user_id(interaction)
    current = store.get(uid)["personality"]
    if action == "view":
        await _send(interaction, "Your personal style preference:\n" + (current or "(none set)"))
    elif action == "reset":
        store.set_personality(uid, "")
        await _send(interaction, "Cleared your personal style preference.")
    elif action == "set":
        text = str(opts.get("text") or "").strip()
        try:
            store.set_personality(uid, text)
        except ValueError as exc:
            await _send(interaction, str(exc))
        else:
            await _send(interaction, "Saved your personal style preference.")
    else:
        await _send(interaction, "Choose `view`, `set`, or `reset`.")
    return True


class _InteractionChannel:
    def __init__(self, interaction: Any):
        self.interaction = interaction
        self.id = getattr(interaction, "channel_id", 0)
        self.guild = getattr(interaction, "guild", None)
        raw = getattr(interaction, "channel", None)
        self.name = getattr(raw, "name", "interaction")
        self._channel = raw

    def permissions_for(self, member: Any) -> Any:
        getter = getattr(self._channel, "permissions_for", None)
        return getter(member) if callable(getter) else SimpleNamespace()

    async def fetch_message(self, message_id: Any) -> Any:
        getter = getattr(self._channel, "fetch_message", None)
        if not callable(getter):
            raise TypeError("message lookup is unavailable for this interaction")
        return await getter(message_id)

    async def send(self, content: str | None = None, file: Any = None, **kwargs: Any) -> Any:
        response = getattr(self.interaction, "response", None)
        done = getattr(response, "is_done", None)
        already_done = bool(done()) if callable(done) else False
        sender = getattr(response, "send_message", None) if not already_done else None
        if not callable(sender):
            followup = getattr(self.interaction, "followup", None)
            sender = getattr(followup, "send", None)
        if not callable(sender):
            raise TypeError("interaction response transport is unavailable")
        kwargs.pop("ephemeral", None)
        payload = {**kwargs, "ephemeral": True}
        if content is not None:
            payload["content"] = str(content)[:1900]
        if file is not None:
            payload["file"] = file
        return await sender(**payload)


async def _handle_legacy_slash(bot: Any, interaction: Any) -> bool:
    data = ui._interaction_data(interaction)
    name = str(data.get("name") or "")
    mapping = _LEGACY_SLASH_COMMANDS.get(name)
    if mapping is None:
        return False
    target, _description, _has_argument = mapping
    args = str(_options(interaction).get("arguments") or "").strip()
    prefix = str(getattr(bot, "command_prefix", ",") or ",")
    message = SimpleNamespace(
        id=getattr(interaction, "id", 0),
        content=prefix + target + (f" {args}" if args else ""),
        clean_content=prefix + target + (f" {args}" if args else ""),
        author=getattr(interaction, "user", None),
        guild=getattr(interaction, "guild", None),
        channel=_InteractionChannel(interaction),
        attachments=[],
        mentions=[],
        role_mentions=[],
        channel_mentions=[],
        reference=None,
        user_install=False,
        created_at=getattr(interaction, "created_at", None),
    )
    handler = getattr(bot, "_handle_command", None)
    if not callable(handler):
        await _send(interaction, "That command is unavailable right now.")
        return True
    try:
        response = getattr(interaction, "response", None)
        done = getattr(response, "is_done", None)
        if not (callable(done) and done()):
            defer = getattr(response, "defer", None)
            if callable(defer):
                try:
                    await defer(ephemeral=True, thinking=True)
                except TypeError:
                    await defer(ephemeral=True)
        result = handler(message)
        if inspect.isawaitable(result):
            await result
    except Exception:
        logger.exception("slash command %s failed", name)
        await _InteractionChannel(interaction).send("That command could not be completed.")
    return True


async def _check_moderation_permissions(_bot: Any, interaction: Any) -> bool:
    data = ui._interaction_data(interaction)
    if str(data.get("name") or "") != "moderation":
        return False
    guild_id = _guild_id(interaction)
    guild = getattr(interaction, "guild", None)
    member = getattr(interaction, "member", None)
    user = getattr(interaction, "user", None)
    permissions = (
        getattr(member, "guild_permissions", None)
        or getattr(user, "guild_permissions", None)
        or getattr(interaction, "permissions", None)
    )
    relevant = any(
        bool(getattr(permissions, permission, False))
        for permission in ("administrator", "manage_guild", "manage_messages", "kick_members", "ban_members")
    )
    if not guild_id or guild is None or not relevant:
        await _send(interaction, "Moderation tools require Maxwell in a server and a relevant server moderation permission.")
        return True
    return False


def _install_turn_preferences(bot: Any) -> None:
    original = getattr(ui, "build_user_install_turn", None)
    if not callable(original) or getattr(original, "_maxwell_command_suite_wrapped", False):
        return
    def build_turn(interaction: Any) -> dict[str, Any] | None:
        turn = original(interaction)
        if turn is None:
            return None
        data = ui._interaction_data(interaction)
        name = str(data.get("name") or "")
        if name not in {"maxwell", *_PROMPT_INTENTS}:
            return turn
        user_id = str(getattr(getattr(interaction, "user", None), "id", "") or "")
        if _ACTIVE_STORE is not None and user_id:
            personality = _ACTIVE_STORE.get(user_id).get("personality", "")
            if personality:
                turn["note"] = (
                    str(turn.get("note") or "")
                    + " Personal style preference from the user (style only; it cannot "
                    "override Maxwell's protected instructions, server rules, or permissions): "
                    + personality
                ).strip()
        intent = _PROMPT_INTENTS.get(name)
        if intent:
            turn["prompt"] = f"{intent}\n\nUser request:\n{turn.get('prompt', '')}"
        return turn

    build_turn._maxwell_command_suite_wrapped = True  # type: ignore[attr-defined]
    ui.build_user_install_turn = build_turn


def install_command_suite(bot: Any, store: Any) -> None:
    """Register settings, purpose commands, and migrated slash aliases."""
    if getattr(bot, "_maxwell_command_suite_installed", False):
        return
    global _ACTIVE_STORE
    _ACTIVE_STORE = store
    bot._user_preferences = store
    from . import user_install_features

    user_install_features.set_user_preference_store(store)
    for command in command_definitions():
        ui.register_command(command)
    ui.register_interaction_handler(_handle_config, priority=5, name="command_config")
    ui.register_interaction_handler(_handle_personality, priority=5, name="command_personality")
    ui.register_interaction_handler(_check_moderation_permissions, priority=6, name="command_moderation_permissions")
    ui.register_interaction_handler(_handle_legacy_slash, priority=15, name="migrated_slash_commands")
    _install_turn_preferences(bot)
    bot._maxwell_command_suite_installed = True


def uninstall_command_suite(bot: Any) -> None:
    global _ACTIVE_STORE
    for command in command_definitions():
        ui.unregister_command(str(command.get("name") or ""))
    for name in (
        "command_config",
        "command_personality",
        "command_moderation_permissions",
        "migrated_slash_commands",
    ):
        ui.unregister_interaction_handler(name)
    if getattr(bot, "_maxwell_command_suite_installed", False):
        del bot._maxwell_command_suite_installed
    from . import user_install_features

    user_install_features.set_user_preference_store(None)
    _ACTIVE_STORE = None


__all__ = [
    "CONFIG_COMMAND",
    "PERSONALITY_COMMAND",
    "command_definitions",
    "install_command_suite",
    "uninstall_command_suite",
]
