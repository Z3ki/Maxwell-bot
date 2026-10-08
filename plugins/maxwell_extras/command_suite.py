"""Purpose-specific Discord application commands for Maxwell."""

from __future__ import annotations

import logging
import asyncio
import json
from functools import wraps
from typing import Any

import discord

import user_install as ui
from control_defaults import GUILD_CAPABILITIES
from .byok import (
    PROVIDERS,
    VaultUnavailable,
    format_generation,
    make_request_provider,
    parse_model_support,
)

logger = logging.getLogger(__name__)
_ACTIVE_STORE: Any = None

_APP_META = {"type": 1, "integration_types": [0, 1], "contexts": [0, 1, 2]}
_PERSONAL_DEFAULT_VALUES = {
    "mode": {"ask", "research", "summarize", "explain", "rewrite", "translate", "brainstorm", "code"},
    "web": {"auto", "search", "off"},
    "detail": {"quick", "balanced", "deep"},
    "context": {str(count) for count in ui.USER_INSTALL_CONTEXT_COUNTS},
    "visibility": {"private", "public"},
}
_PERSONAL_SETTINGS = {
    "mode": ("Response mode", "Choose how Maxwell approaches your requests."),
    "web": ("Web research", "Choose when Maxwell searches the web."),
    "detail": ("Answer detail", "Choose how much detail Maxwell gives."),
    "context": ("Channel context", "Choose up to 1,000 recent channel messages for app requests."),
    "visibility": ("Default visibility", "Choose whether /maxwell and message app replies are private or public."),
    "language": ("Response language", "Set a preferred language, such as English or Spanish."),
    "style": ("Personality", "Your reply personality across channels, servers and DMs."),
    "byok": ("Bring your own key", "Set an OpenAI-compatible endpoint, model, modalities, and encrypted key."),
}
_SERVER_SETTINGS = {
    "channels": ("Response channels", "Limit Maxwell to one channel in this server or allow all channels."),
    "plugins": ("Plugin tools", "Choose which optional plugin tools are available in this server."),
    "capabilities": ("Enabled capabilities", "Choose which tool groups are available in this server."),
    "moderation": ("Moderation policy", "Enable or disable Maxwell's moderation tools in this server."),
    "progress": ("Tool progress", "Show or hide progress messages while Maxwell works."),
    "ticket": ("Ticket greetings", "Enable or disable greetings in ticket channels."),
}
_OWNER_SETTINGS = {
    "diagnostics": ("Runtime diagnostics", "View redacted application diagnostics."),
    "tools_enabled": ("Global tool access", "Enable or disable model tool execution globally."),
    "autonomy_enabled": ("Autonomous activity", "Enable or disable autonomous activity globally."),
    "message_quota_enabled": ("Message allowance", "Enable or disable the current message allowance policy."),
    "user_quota": ("User message allowance", "Set or clear one user's quota override."),
    "reload": ("Reload controls", "Reload the trusted bot_control.json file."),
}
_PERSONAL_VALUE_CHOICES = {
    "mode": [
        ("Answer normally", "ask"), ("Research", "research"),
        ("Summarize", "summarize"), ("Explain", "explain"),
        ("Rewrite", "rewrite"), ("Translate", "translate"),
        ("Brainstorm", "brainstorm"), ("Write code", "code"),
    ],
    "web": [("Automatic", "auto"), ("Always search", "search"), ("Off", "off")],
    "detail": [("Brief", "quick"), ("Balanced", "balanced"), ("Deep", "deep")],
    "context": [
        ("No recent context" if count == 0 else f"Last {count:,} messages", str(count))
        for count in ui.USER_INSTALL_CONTEXT_COUNTS
    ],
    "visibility": [("Public — visible in this channel", "public"), ("Private — only you can see the reply", "private")],
}
_CHOICE_HELP = {
    "auto": "Searches when current information is needed.",
    "search": "Searches the web before answering.",
    "off": "Turn this feature off.",
    "on": "Turn this feature on.",
    "quick": "Short answers for quick questions.",
    "balanced": "Enough explanation without a long reply.",
    "deep": "Detailed explanations and examples.",
    "public": "Others in this channel can see Maxwell's reply.",
    "private": "Only you see the reply; server-wide actions are unavailable.",
    "0": "Use your request without reading recent channel messages.",
    "10": "A little context from this channel.",
    "25": "Useful context for an ongoing conversation.",
    "50": "More context; may take longer to process.",
}
_SETTING_HELP = {
    "mode": "For everyday questions, choose Answer normally. Choose Research for a sourced answer.",
    "web": "Automatic works for most requests. Off prevents web search for this request mode.",
    "detail": "Brief is the default. Balanced adds explanation. Deep asks for a fuller explanation.",
    "context": "Choose up to 1,000 messages. Very long conversations may include fewer messages. Only recent messages Maxwell can access in this channel are included. Choose none to use just your request.",
    "visibility": "Applies to /maxwell and message app actions. Public replies appear in the channel; Private replies are only visible to you. You can override this on /maxwell. Servers must allow Use External Apps for user-installed apps to reply publicly.",
    "language": "For example, enter Spanish or English. Reset lets Maxwell choose the language again.",
    "style": "For example: Keep replies short and skip emojis. Applies to your messages, mentions and commands everywhere. Changes only your replies.",
    "byok": "Paste a public HTTPS OpenAI-compatible endpoint, the model ID, and the modalities that model accepts. The key is encrypted. Private hosts are rejected.",
    "channels": "Choose where Maxwell can respond. Clear the channel selection to allow all channels.",
    "plugins": "Select the optional tools to allow in this server. Reset inherits the existing global and personal choices.",
    "capabilities": "Selected groups are disabled. Leave a group unselected to allow its tools.",
    "moderation": "Controls moderation tools for this server. Discord permissions still apply.",
    "progress": "Show updates while tools run, or keep the channel quieter with Off.",
    "ticket": "Controls automatic greetings in recognized ticket channels.",
}

CONFIG_COMMAND = {
    "name": "config",
    "description": "Customize your replies or manage this server with a private settings menu.",
    **_APP_META,
}
CANCEL_COMMAND = {
    "name": "cancel",
    "description": "Cancel your running Maxwell request in this channel.",
    **_APP_META,
}


def command_definitions() -> list[dict[str, Any]]:
    return [CONFIG_COMMAND, CANCEL_COMMAND]


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
    return False


def _is_application_owner(bot: Any, interaction: Any) -> bool:
    """Only configured application owners can open global controls."""
    user_id = _user_id(interaction)
    config = getattr(bot, "config", None)
    owners = getattr(config, "MAXWELL_OWNER_IDS", ()) or ()
    return bool(user_id and user_id in {str(value) for value in owners})


def _guild_disabled(bot: Any, guild_id: str) -> set[str]:
    from .admin_commands import _control

    mapping = _control(bot).get("guild_disabled_capabilities", {})
    values = mapping.get(str(guild_id), []) if isinstance(mapping, dict) else []
    return {str(value) for value in values if str(value) in GUILD_CAPABILITIES}


def _guild_plugin_names(bot: Any) -> list[str]:
    manager = getattr(bot, "plugin_manager", None)
    loaded = getattr(manager, "loaded_plugins", {}) or {}
    names: list[str] = []
    for name in loaded:
        plugin_id = str(name or "").strip()
        if not plugin_id.isidentifier():
            continue
        is_protected = getattr(manager, "is_protected", None)
        if callable(is_protected) and is_protected(plugin_id):
            continue
        names.append(plugin_id)
    return sorted(set(names))


def _guild_plugin_overrides(bot: Any, guild_id: str) -> dict[str, bool]:
    from .admin_commands import _control

    mapping = _control(bot).get("guild_plugin_overrides", {})
    values = mapping.get(str(guild_id), {}) if isinstance(mapping, dict) else {}
    if not isinstance(values, dict):
        return {}
    return {
        str(name): value
        for name, value in values.items()
        if str(name).isidentifier() and type(value) is bool
    }


async def _set_guild_plugin_selection(
    bot: Any, guild_id: str, enabled: set[str]
) -> None:
    from .admin_commands import _control, _set_control

    names = _guild_plugin_names(bot)
    if len(names) > 25:
        raise ValueError("Discord can display at most 25 plugin choices")
    if not enabled <= set(names):
        raise ValueError("a selected plugin is unavailable")
    mapping = _control(bot).get("guild_plugin_overrides", {})
    mapping = dict(mapping) if isinstance(mapping, dict) else {}
    # Save a complete choice for the currently installed optional plugins.
    # Uninstalled plugin IDs are discarded so stale `true` values cannot
    # silently re-enable a plugin if it is installed again later.
    if names:
        mapping[str(guild_id)] = {name: name in enabled for name in names}
    else:
        mapping.pop(str(guild_id), None)
    await _set_control(bot, "guild_plugin_overrides", json.dumps(mapping))

async def _set_guild_disabled(bot: Any, guild_id: str, disabled: set[str]) -> None:
    from .admin_commands import _control, _set_control

    mapping = _control(bot).get("guild_disabled_capabilities", {})
    mapping = dict(mapping) if isinstance(mapping, dict) else {}
    if disabled:
        mapping[str(guild_id)] = sorted(disabled)
    else:
        mapping.pop(str(guild_id), None)
    await _set_control(bot, "guild_disabled_capabilities", json.dumps(mapping))


async def _send(interaction: Any, text: str) -> None:
    await ui._ephemeral(interaction, str(text)[:1900])


async def _acknowledge(interaction: Any, *, thinking: bool = False) -> None:
    response = getattr(interaction, "response", None)
    done = getattr(response, "is_done", None)
    defer = getattr(response, "defer", None)
    if callable(defer) and not (callable(done) and done()):
        await defer(ephemeral=True, thinking=thinking)


async def _edit_panel(interaction: Any, **payload: Any) -> None:
    response = interaction.response
    done = getattr(response, "is_done", None)
    if callable(done) and done():
        await interaction.edit_original_response(**payload)
    else:
        await response.edit_message(**payload)


def _config_action(*, thinking: bool = False):
    """Acknowledge before I/O and serialize changes to a shared settings panel."""
    def decorate(callback):
        @wraps(callback)
        async def run(self, interaction, *args, **kwargs):
            panel = getattr(self, "panel", self)
            if not await panel.authorized(interaction):
                return
            await _acknowledge(interaction, thinking=thinking)
            async with panel._action_lock:
                context = kwargs.pop("context", getattr(self, "_config_context", None))
                if context is not None and context != (panel.scope, panel.selected_key):
                    await _send(interaction, "This control has changed. Use the current settings menu.")
                    return
                if not await panel.authorized(interaction):
                    return
                await panel._refresh_personal()
                await callback(self, interaction, *args, **kwargs)
        return run
    return decorate


def _preference_store(bot: Any) -> Any:
    return getattr(bot, "_user_preferences", None)


def _friendly_personal_value(key: str, value: Any) -> str:
    text = str(value if value not in (None, "") else "not set")
    if key == "mode":
        return {
            stored: label
            for label, stored in _PERSONAL_VALUE_CHOICES["mode"]
        }.get(text, text)
    if key == "web":
        return {"auto": "Automatic", "search": "Always search", "off": "Off"}.get(text, text)
    if key == "detail":
        return text.capitalize()
    if key == "context":
        return "No recent messages" if text == "0" else f"Last {text} messages"
    if key == "visibility":
        return {"private": "Private — only you", "public": "Public — visible in this channel"}.get(text, "Private — only you")
    return text


class _ConfigScopeSelect(discord.ui.Select):
    def __init__(self, panel: "_ConfigPanel"):
        options = [discord.SelectOption(label="My personal settings", value="personal")]
        if panel.can_manage_server:
            options.append(discord.SelectOption(label="This server", value="server"))
        if panel.is_owner:
            options.append(discord.SelectOption(label="Application owner", value="owner"))
        for option in options:
            option.default = option.value == panel.scope
        super().__init__(
            placeholder="Choose a settings area",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="maxwell:config:scope",
            row=0,
        )
        self.panel = panel

    @_config_action(thinking=False)
    async def callback(self, interaction: Any) -> None:
        if not await self.panel.authorized(interaction):
            return
        requested_scope = self.values[0]
        if requested_scope == "server" and not _can_manage_server(self.panel.bot, interaction):
            await _send(interaction, "You no longer have permission to manage this server's settings.")
            return
        if requested_scope == "owner" and not _is_application_owner(self.panel.bot, interaction):
            await _send(interaction, "Application-owner settings are restricted to the configured application owners.")
            return
        if requested_scope not in {"personal", "server", "owner"}:
            await _send(interaction, "That settings area is unavailable.")
            return
        self.panel.scope = requested_scope
        self.panel.selected_key = "overview"
        self.panel.notice = ""
        self.panel._build()
        await _edit_panel(interaction,
            content=None, embed=self.panel.embed(), view=self.panel,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class _ConfigSettingSelect(discord.ui.Select):
    def __init__(self, panel: "_ConfigPanel"):
        settings = {
            "personal": _PERSONAL_SETTINGS,
            "server": _SERVER_SETTINGS,
            "owner": _OWNER_SETTINGS,
        }[panel.scope]
        quick_home = panel.scope == "personal" and panel.selected_key == "overview"
        if quick_home:
            settings = {key: value for key, value in settings.items()
                        if key not in {"style", "language", "visibility"}}
        options = ([] if quick_home else [discord.SelectOption(
            label="Back to settings", value="overview",
            description="See your setup and choose what to change.",
            default=panel.selected_key == "overview",
        )]) + [
            discord.SelectOption(label=label, value=key, description=description[:100], default=key == panel.selected_key)
            for key, (label, description) in settings.items()
        ]
        super().__init__(
            placeholder="More options" if quick_home else "Choose a setting",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="maxwell:config:setting",
            row=(2 if panel.can_choose_scope else 1) if quick_home else (1 if panel.can_choose_scope else 0),
        )
        self.panel = panel

    @_config_action(thinking=False)
    async def callback(self, interaction: Any) -> None:
        if not await self.panel.authorized(interaction):
            return
        self.panel.selected_key = self.values[0]
        self.panel.notice = ""
        self.panel._build()
        await _edit_panel(interaction,
            content=None, embed=self.panel.embed(), view=self.panel,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class _ConfigValueSelect(discord.ui.Select):
    def __init__(self, panel: "_ConfigPanel", choices: list[tuple[str, str]]):
        current = panel.choice_value()
        options = [
            discord.SelectOption(label=label, value=value, default=value == current, description=_CHOICE_HELP.get(value))
            for label, value in choices
        ]
        super().__init__(
            placeholder="Choose an option — saves immediately",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="maxwell:config:value",
            row=2 if panel.can_choose_scope else 1,
        )
        self.panel = panel

    async def callback(self, interaction: Any) -> None:
        await self.panel.set_choice(interaction, self.values[0], context=self._config_context)


class _GuildCapabilitySelect(discord.ui.Select):
    def __init__(self, panel: "_ConfigPanel", row: int):
        disabled = _guild_disabled(panel.bot, panel.guild_id)
        options = [
            discord.SelectOption(
                label=f"Disable {label}"[:100],
                value=key,
                default=key in disabled,
                description=f"{label} tools {('disabled' if key in disabled else 'enabled')}"[:100],
            )
            for key, label in GUILD_CAPABILITIES.items()
        ]
        super().__init__(
            placeholder="Select capability groups to disable",
            min_values=0,
            max_values=min(25, len(options)),
            options=options,
            custom_id="maxwell:config:capabilities",
            row=row,
        )
        self.panel = panel

    @_config_action(thinking=False)
    async def callback(self, interaction: Any) -> None:
        if not await self.panel.authorized(interaction):
            return
        disabled = {str(value) for value in self.values}
        if not disabled <= set(GUILD_CAPABILITIES):
            await _send(interaction, "That capability group is not available.")
            return
        try:
            await _set_guild_disabled(self.panel.bot, self.panel.guild_id, disabled)
        except Exception as exc:
            logger.warning("Could not update server capabilities (%s)", type(exc).__name__)
            await _send(interaction, "Could not save the server capability settings.")
            return
        self.panel._build()
        await _edit_panel(interaction,
            content=None, embed=self.panel.embed(), view=self.panel,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class _GuildPluginSelect(discord.ui.Select):
    def __init__(self, panel: "_ConfigPanel", row: int):
        manager = getattr(panel.bot, "plugin_manager", None)
        names = _guild_plugin_names(panel.bot)
        overrides = _guild_plugin_overrides(panel.bot, panel.guild_id)
        is_enabled = getattr(manager, "is_plugin_enabled_for_user", None)
        options = []
        for name in names:
            if name in overrides:
                enabled = overrides[name]
            elif callable(is_enabled):
                enabled = bool(is_enabled(name, None))
            else:
                enabled = False
            options.append(
                discord.SelectOption(
                    label=name[:100],
                    value=name,
                    default=enabled,
                    description=("Enabled in this server" if enabled else "Disabled in this server"),
                )
            )
        super().__init__(
            placeholder="Select plugin tools enabled in this server",
            min_values=0,
            max_values=min(25, len(options)),
            options=options,
            custom_id="maxwell:config:plugins",
            row=row,
        )
        self.panel = panel

    @_config_action(thinking=False)
    async def callback(self, interaction: Any) -> None:
        if not await self.panel.authorized(interaction):
            return
        try:
            await _set_guild_plugin_selection(
                self.panel.bot, self.panel.guild_id, {str(value) for value in self.values}
            )
        except Exception as exc:
            logger.warning("Could not update server plugin settings (%s)", type(exc).__name__)
            await _send(interaction, "Could not save the plugin settings.")
            return
        self.panel._build()
        await _edit_panel(interaction,
            content=None, embed=self.panel.embed(), view=self.panel,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class _GuildChannelSelect(discord.ui.Select):
    def __init__(self, panel: "_ConfigPanel", row: int):
        guild = getattr(panel.command_interaction, "guild", None)
        channels = [
            channel for channel in (getattr(guild, "text_channels", None) or [])
            if getattr(channel, "id", None) is not None
        ][:24]
        options = [discord.SelectOption(label="All channels", value="all")]
        options.extend(
            discord.SelectOption(
                label=("#" + str(getattr(channel, "name", "channel")))[:100],
                value=str(channel.id),
            )
            for channel in channels
        )
        super().__init__(
            placeholder="Choose where Maxwell may reply",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="maxwell:config:channels",
            row=row,
        )
        self.panel = panel

    @_config_action(thinking=False)
    async def callback(self, interaction: Any) -> None:
        if not await self.panel.authorized(interaction):
            return
        value = str(self.values[0])
        guild = getattr(interaction, "guild", None)
        if str(getattr(guild, "id", "") or "") != self.panel.guild_id:
            await _send(interaction, "This menu belongs to a different server.")
            return
        mapping = dict(getattr(self.panel.bot, "_control", {}).get("guild_solo_channel", {}) or {})
        saver = getattr(self.panel.bot, "_save_solo", None)
        if not callable(saver):
            await _send(interaction, "Server channel settings are unavailable right now.")
            return
        if value == "all":
            mapping.pop(self.panel.guild_id, None)
            await saver(mapping, self.panel.guild_id, unblock_autonomy=True)
        else:
            allowed_ids = {
                str(getattr(channel, "id", ""))
                for channel in (getattr(guild, "text_channels", None) or [])
            }
            if value not in allowed_ids:
                await _send(interaction, "Choose a text channel from this server.")
                return
            mapping[self.panel.guild_id] = value
            await saver(mapping, self.panel.guild_id, unblock_autonomy=False)
        self.panel._build()
        await _edit_panel(interaction,
            content=None, embed=self.panel.embed(), view=self.panel,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class _OwnerDiagnosticsSelect(discord.ui.Select):
    _SECTIONS = ("overview", "runtime", "controls", "memory", "autonomy", "tools", "plugins", "data")

    def __init__(self, panel: "_ConfigPanel", row: int):
        options = [
            discord.SelectOption(label=section.replace("_", " ").title(), value=section)
            for section in self._SECTIONS
        ]
        super().__init__(
            placeholder="Choose a redacted diagnostics view",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="maxwell:config:diagnostics",
            row=row,
        )
        self.panel = panel

    @_config_action(thinking=True)
    async def callback(self, interaction: Any) -> None:
        if not await self.panel.authorized(interaction):
            return
        section = str(self.values[0])
        if section not in self._SECTIONS or not _is_application_owner(self.panel.bot, interaction):
            await _send(interaction, "That diagnostics view is unavailable.")
            return
        from .admin_commands import (
            _control,
            _embed,
            _json_file,
            _plugin_data,
            _redact,
            _runtime_data,
            _send as _send_owner,
        )

        if section == "controls":
            await _send_owner(
                interaction,
                embed=_embed(self.panel.bot, section),
                file=_json_file(_redact(_control(self.panel.bot)), "maxwell-controls.json"),
            )
        elif section == "data":
            payload = {
                "runtime": _runtime_data(self.panel.bot),
                "controls": _redact(_control(self.panel.bot)),
                "plugins": _plugin_data(self.panel.bot),
            }
            await _send_owner(
                interaction,
                content="Redacted Maxwell diagnostics export.",
                file=_json_file(payload, "maxwell-diagnostics.json"),
            )
        else:
            await _send_owner(interaction, embed=_embed(self.panel.bot, section))


class _OwnerQuotaModal(discord.ui.Modal):
    def __init__(self, panel: "_ConfigPanel"):
        super().__init__(title="Update a user's message allowance", timeout=180)
        self.panel = panel
        self.user_id_input = discord.ui.TextInput(label="Discord user ID", max_length=20, required=True)
        self.action_input = discord.ui.TextInput(
            label="Action",
            placeholder="status, set, clear, reset, exempt, unexempt",
            max_length=10,
            required=True,
            default="status",
        )
        self.amount_input = discord.ui.TextInput(label="Limit (for set only)", max_length=6, required=False)
        self.add_item(self.user_id_input)
        self.add_item(self.action_input)
        self.add_item(self.amount_input)

    @_config_action(thinking=True)
    async def on_submit(self, interaction: Any) -> None:
        if not await self.panel.authorized(interaction):
            return
        if not _is_application_owner(self.panel.bot, interaction):
            await _send(interaction, "Only the configured application owner can update message allowances.")
            return
        target = str(self.user_id_input.value or "").strip()
        action = str(self.action_input.value or "").strip().lower()
        if not (target.isdecimal() and len(target) <= 20) or action not in {"status", "set", "clear", "reset", "exempt", "unexempt"}:
            await _send(interaction, "Enter a valid Discord user ID and one of the listed actions.")
            return
        ledger = getattr(self.panel.bot, "_message_quota", None)
        if ledger is None:
            await _send(interaction, "Message allowance data is unavailable.")
            return
        if action == "set":
            try:
                amount = int(str(self.amount_input.value or ""))
            except (TypeError, ValueError):
                await _send(interaction, "For `set`, enter a whole-number limit from 1 to 100,000.")
                return
            if not 1 <= amount <= 100_000:
                await _send(interaction, "The limit must be from 1 to 100,000.")
                return
            ledger.configure(target, limit=amount)
        elif action == "clear":
            ledger.configure(target, clear=True)
        elif action == "reset":
            ledger.configure(target, reset=True)
        elif action == "exempt":
            ledger.configure(target, exempt=True)
        elif action == "unexempt":
            ledger.configure(target, exempt=False)
        from .admin_commands import _control

        control = _control(self.panel.bot)
        state = ledger.status(target, int(control.get("message_quota_limit") or 300), int(control.get("message_quota_window_seconds") or 18000))
        await _send(interaction, f"User `{target}` · {state['used']}/{state['limit']} messages in the last {state['window_seconds']}s · exempt: {'yes' if state['exempt'] else 'no'}")


class _OwnerQuotaButton(discord.ui.Button):
    def __init__(self, panel: "_ConfigPanel", row: int):
        super().__init__(label="Manage user allowance", style=discord.ButtonStyle.primary, custom_id="maxwell:config:user_quota", row=row)
        self.panel = panel

    async def callback(self, interaction: Any) -> None:
        if not await self.panel.authorized(interaction):
            return
        if not _is_application_owner(self.panel.bot, interaction):
            await _send(interaction, "Only the configured application owner can manage message allowances.")
            return
        await interaction.response.send_modal(_OwnerQuotaModal(self.panel))


class _OwnerReloadButton(discord.ui.Button):
    def __init__(self, panel: "_ConfigPanel", row: int):
        super().__init__(label="Reload controls", style=discord.ButtonStyle.secondary, custom_id="maxwell:config:reload", row=row)
        self.panel = panel

    @_config_action(thinking=True)
    async def callback(self, interaction: Any) -> None:
        if not await self.panel.authorized(interaction):
            return
        if not _is_application_owner(self.panel.bot, interaction):
            await _send(interaction, "Only the configured application owner can reload global controls.")
            return
        from .admin_commands import _reload_control

        try:
            await _reload_control(self.panel.bot)
        except Exception as exc:
            logger.warning("Control reload failed (%s)", type(exc).__name__)
            await _send(interaction, "Could not reload the operator control file.")
        else:
            await _send(interaction, "Reloaded the operator control file.")


class _ConfigCloseButton(discord.ui.Button):
    def __init__(self, panel: "_ConfigPanel"):
        super().__init__(label="Close", style=discord.ButtonStyle.secondary,
                         custom_id="maxwell:config:close", row=4)
        self.panel = panel

    @_config_action(thinking=False)
    async def callback(self, interaction: Any) -> None:
        if not await self.panel.authorized(interaction):
            return
        await _edit_panel(interaction,
            embed=None, content="Settings closed. Your saved choices are ready for your next request. Run `/config` to return.",
            view=None, allowed_mentions=discord.AllowedMentions.none(),
        )
        self.panel.stop()


class _ConfigShortcutButton(discord.ui.Button):
    def __init__(self, panel: "_ConfigPanel", key: str):
        super().__init__(
            label={"style": "Personality", "language": "Language", "visibility": "Reply visibility"}[key],
            style=discord.ButtonStyle.primary if key == "style" else discord.ButtonStyle.secondary,
            custom_id=f"maxwell:config:shortcut:{key}",
            row=1 if panel.can_choose_scope else 0,
        )
        self.panel = panel
        self.key = key

    async def callback(self, interaction: Any) -> None:
        if not await self.panel.authorized(interaction):
            return
        if self.panel.scope != "personal":
            return
        if self.key in {"style", "language"}:
            await interaction.response.send_modal(self.panel.edit_modal(key=self.key))
        else:
            await self._show_setting(interaction)

    @_config_action()
    async def _show_setting(self, interaction: Any) -> None:
        self.panel.selected_key = self.key
        self.panel.notice = ""
        self.panel._build()
        await _edit_panel(interaction,
            content=None, embed=self.panel.embed(), view=self.panel,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class _ConfigEditButton(discord.ui.Button):
    def __init__(self, panel: "_ConfigPanel"):
        super().__init__(
            label="Edit personality" if panel.selected_key == "style" else "Edit language",
            style=discord.ButtonStyle.primary,
            custom_id="maxwell:config:edit",
            row=3 if panel.can_choose_scope else 2,
        )
        self.panel = panel

    async def callback(self, interaction: Any) -> None:
        if not await self.panel.authorized(interaction):
            return
        modal = self.panel.edit_modal()
        if modal is None:
            await _send(interaction, "That setting cannot be edited as text.")
            return
        await interaction.response.send_modal(modal)


class _ConfigResetButton(discord.ui.Button):
    def __init__(self, panel: "_ConfigPanel"):
        super().__init__(
            label="Reset this setting",
            style=discord.ButtonStyle.secondary,
            custom_id="maxwell:config:reset",
            row=3 if panel.can_choose_scope else 2,
        )
        self.panel = panel

    async def callback(self, interaction: Any) -> None:
        await self.panel.reset(interaction, context=self._config_context)


class _ConfigTextModal(discord.ui.Modal):
    def __init__(self, panel: "_ConfigPanel", *, key: str, label: str, current: str, max_length: int, placeholder: str):
        super().__init__(title=f"Edit {label.lower()}", timeout=180)
        self.panel = panel
        self.key = key
        self.field = discord.ui.TextInput(
            label=label[:45],
            placeholder=placeholder[:100],
            default=current[:max_length] if current else None,
            max_length=max_length,
            required=True,
            style=discord.TextStyle.paragraph if max_length > 120 else discord.TextStyle.short,
        )
        self.add_item(self.field)

    @_config_action(thinking=True)
    async def on_submit(self, interaction: Any) -> None:
        if not await self.panel.authorized(interaction):
            return
        value = str(self.field.value or "").strip()
        try:
            await asyncio.to_thread(self.panel.set_text_value, self.key, value)
            await self.panel._refresh_personal()
        except ValueError as exc:
            await _send(interaction, str(exc))
            return
        self.panel.selected_key = self.key
        self.panel.notice = "**Saved.** Your next reply will use this setting."
        await _send(interaction, "Saved. This applies to your replies across channels, servers and DMs.")
        self.panel._build()
        try:
            await self.panel.command_interaction.edit_original_response(
                content=None, embed=self.panel.embed(), view=self.panel,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            logger.debug("Could not refresh the original /config panel after a text edit", exc_info=True)


class _ByokCredentialsModal(discord.ui.Modal):
    def __init__(self, panel: "_ConfigPanel", provider: str, status: dict | None = None):
        label = PROVIDERS[provider]["label"]
        title = f"Set {label}"[:45]
        super().__init__(title=title, timeout=180)
        self.panel = panel
        self.provider = provider
        saved = status if status and status.get("provider") == provider else None
        preset_url = PROVIDERS[provider]["base_url"]
        endpoint_default = (saved or {}).get("base_url") or preset_url
        model_default = (saved or {}).get("model") or PROVIDERS[provider]["suggested_model"]
        if saved:
            modalities_default = saved.get("modalities") or "text, tools"
            generation_default = saved.get("generation") or format_generation({})
        elif preset_url:
            modalities_default = "text, vision, tools"
            generation_default = format_generation({})
        else:
            modalities_default = "text, tools"
            generation_default = format_generation({})
        self.endpoint = discord.ui.TextInput(
            label="Endpoint URL",
            default=endpoint_default or None,
            placeholder="https://api.example.com/v1 — blank keeps the official URL",
            max_length=300,
            required=not bool(preset_url),
        )
        self.model = discord.ui.TextInput(
            label="Model ID",
            default=model_default or None,
            placeholder="The model name this endpoint expects",
            max_length=120,
            required=True,
        )
        self.api_key = discord.ui.TextInput(
            label="API key (private form)",
            placeholder=(
                "Leave blank to keep the saved key"
                if saved
                else "Paste the provider key here; it is encrypted before storage"
            ),
            max_length=512,
            required=not bool(saved),
        )
        self.modalities = discord.ui.TextInput(
            label="Modalities",
            default=modalities_default,
            placeholder="text, vision, audio, tools",
            max_length=80,
            required=False,
        )
        self.generation = discord.ui.TextInput(
            label="Generation",
            default=generation_default,
            placeholder="reasoning=off max_tokens=4096 temperature=0.4 effort=low context=128000",
            max_length=200,
            required=False,
        )
        self.add_item(self.endpoint)
        self.add_item(self.model)
        self.add_item(self.api_key)
        self.add_item(self.modalities)
        self.add_item(self.generation)

    @_config_action(thinking=True)
    async def on_submit(self, interaction: Any) -> None:
        if not await self.panel.authorized(interaction):
            return
        if self.panel.scope != "personal":
            await _send(interaction, "BYOK credentials are personal settings only.")
            return
        vault = getattr(self.panel.bot, "_byok_vault", None)
        if vault is None or not getattr(vault, "enabled", False):
            await _send(
                interaction,
                "BYOK is unavailable until the operator configures MAXWELL_BYOK_ENCRYPTION_KEY.",
            )
            return
        try:
            settings = parse_model_support(
                str(self.modalities.value or ""),
                str(self.generation.value or ""),
            )
            secret = str(self.api_key.value or "").strip()
            if not secret:
                existing = await asyncio.to_thread(vault.get, self.panel.user_id)
                if not existing or existing.get("provider") != self.provider:
                    raise ValueError("API key is required")
                secret = str(existing.get("api_key") or "")
            await asyncio.to_thread(
                vault.save,
                self.panel.user_id,
                self.provider,
                str(self.model.value or ""),
                secret,
                base_url=str(self.endpoint.value or ""),
                settings=settings,
            )
        except ValueError as exc:
            await _send(interaction, str(exc))
            return
        except Exception as exc:
            logger.warning("BYOK credential save failed (%s)", type(exc).__name__)
            await _send(interaction, "Could not save that BYOK configuration.")
            return
        self.panel.selected_provider = self.provider
        self.panel._build()
        await _send(interaction, "Saved. This provider will receive the request context you send to Maxwell.")
        try:
            await self.panel.command_interaction.edit_original_response(
                content=None, embed=self.panel.embed(), view=self.panel,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            logger.debug("Could not refresh /config after BYOK update", exc_info=True)


class _ByokProviderSelect(discord.ui.Select):
    def __init__(self, panel: "_ConfigPanel", row: int):
        options = [
            discord.SelectOption(
                label=details["label"],
                value=provider,
                **(
                    {"description": "Any public OpenAI-compatible HTTPS endpoint"}
                    if provider == "custom"
                    else {}
                ),
                default=provider == panel.selected_provider,
            )
            for provider, details in PROVIDERS.items()
        ]
        super().__init__(
            placeholder="Choose your provider",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="maxwell:config:byok_provider",
            row=row,
        )
        self.panel = panel

    @_config_action(thinking=False)
    async def callback(self, interaction: Any) -> None:
        if not await self.panel.authorized(interaction):
            return
        self.panel.selected_provider = self.values[0]
        self.panel._build()
        await _edit_panel(interaction,
            content=None, embed=self.panel.embed(), view=self.panel,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class _ByokKeyButton(discord.ui.Button):
    def __init__(self, panel: "_ConfigPanel", row: int):
        super().__init__(
            label="Configure model",
            style=discord.ButtonStyle.primary,
            custom_id="maxwell:config:byok_key",
            row=row,
        )
        self.panel = panel

    async def callback(self, interaction: Any) -> None:
        if not await self.panel.authorized(interaction):
            return
        if self.panel.scope != "personal":
            await _send(interaction, "BYOK controls are available only in personal settings.")
            return
        if self.panel.selected_provider not in PROVIDERS:
            await _send(interaction, "Choose a supported provider first.")
            return
        status = None
        vault = getattr(self.panel.bot, "_byok_vault", None)
        if vault is not None and getattr(vault, "enabled", False):
            try:
                status = await asyncio.to_thread(vault.status, self.panel.user_id)
            except Exception:
                status = None
        await interaction.response.send_modal(
            _ByokCredentialsModal(self.panel, self.panel.selected_provider, status)
        )


class _ByokTestButton(discord.ui.Button):
    def __init__(self, panel: "_ConfigPanel", row: int):
        super().__init__(
            label="Test connection",
            style=discord.ButtonStyle.secondary,
            custom_id="maxwell:config:byok_test",
            row=row,
        )
        self.panel = panel

    @_config_action(thinking=True)
    async def callback(self, interaction: Any) -> None:
        if not await self.panel.authorized(interaction):
            return
        if self.panel.scope != "personal":
            await _send(interaction, "BYOK controls are available only in personal settings.")
            return
        vault = getattr(self.panel.bot, "_byok_vault", None)
        if vault is None:
            await _send(interaction, "BYOK settings are unavailable right now.")
            return
        try:
            credential = await asyncio.to_thread(vault.get, self.panel.user_id)
        except Exception as exc:
            logger.warning("BYOK credential read failed (%s)", type(exc).__name__)
            await _send(interaction, "Could not read your BYOK configuration.")
            return
        if not credential or credential["provider"] != self.panel.selected_provider:
            await _send(interaction, "Save a key for the selected provider first.")
            return
        response = getattr(interaction, "response", None)
        defer = getattr(response, "defer", None)
        is_done = getattr(response, "is_done", None)
        if callable(defer) and not (callable(is_done) and is_done()):
            await defer(ephemeral=True, thinking=True)
        try:
            provider = make_request_provider(self.panel.bot, credential)
            await asyncio.wait_for(
                provider.generate_response(
                    [{"role": "user", "content": "Reply with OK."}],
                    timeout=20,
                    max_tokens=16,
                    temperature=0,
                    disable_reasoning=True,
                ),
                timeout=25,
            )
        except Exception as exc:
            logger.info("BYOK connection test failed (%s)", type(exc).__name__)
            await _send(interaction, "Test failed. Check the provider, model, and key.")
        else:
            await _send(interaction, "Provider connection succeeded.")
        finally:
            if "provider" in locals():
                close = getattr(provider, "close", None)
                if callable(close):
                    try:
                        await close()
                    except Exception:
                        logger.debug("Could not close BYOK test client")


class _ByokDeleteButton(discord.ui.Button):
    def __init__(self, panel: "_ConfigPanel", row: int):
        super().__init__(
            label="Delete key",
            style=discord.ButtonStyle.danger,
            custom_id="maxwell:config:byok_delete",
            row=row,
        )
        self.panel = panel

    @_config_action(thinking=True)
    async def callback(self, interaction: Any) -> None:
        if not await self.panel.authorized(interaction):
            return
        if self.panel.scope != "personal":
            await _send(interaction, "BYOK controls are available only in personal settings.")
            return
        vault = getattr(self.panel.bot, "_byok_vault", None)
        if vault is None:
            await _send(interaction, "BYOK settings are unavailable right now.")
            return
        try:
            removed = await asyncio.to_thread(vault.delete, self.panel.user_id)
        except Exception as exc:
            logger.warning("BYOK credential deletion failed (%s)", type(exc).__name__)
            await _send(interaction, "Could not delete your BYOK key.")
            return
        await _send(interaction, "Deleted your BYOK key." if removed else "No BYOK key was saved.")
        self.panel._build()
        try:
            await self.panel.command_interaction.edit_original_response(
                content=None, embed=self.panel.embed(), view=self.panel,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            logger.debug("Could not refresh /config after BYOK deletion", exc_info=True)


class _ConfigPanel(discord.ui.View):
    def __init__(self, bot: Any, store: Any, interaction: Any, *, personal: dict | None = None):
        super().__init__(timeout=180)
        self.bot = bot
        self.store = store
        self.command_interaction = interaction
        self.user_id = _user_id(interaction)
        self.guild_id = _guild_id(interaction)
        self._action_lock = asyncio.Lock()
        self.personal = personal if personal is not None else store.get(self.user_id)
        self.scope = "personal"
        self.selected_key = "overview"
        self.notice = ""
        self.selected_provider = "openai"
        self.owner_quota_action = "status"
        try:
            status = self._byok_status()
            if status:
                self.selected_provider = status["provider"]
        except Exception:
            pass
        self.can_manage_server = bool(self.guild_id and _can_manage_server(bot, interaction))
        self.is_owner = _is_application_owner(bot, interaction)
        self.can_choose_scope = self.can_manage_server or self.is_owner
        self._build()

    def add_item(self, item):
        item._config_context = (self.scope, self.selected_key)
        return super().add_item(item)

    async def _refresh_personal(self) -> None:
        self.personal = await asyncio.to_thread(self.store.get, self.user_id)

    async def authorized(self, interaction: Any) -> bool:
        if _user_id(interaction) != self.user_id:
            await _send(interaction, "Only the person who opened `/config` can use this menu.")
            return False
        if _guild_id(interaction) != self.guild_id:
            await _send(interaction, "This `/config` menu belongs to a different interaction context.")
            return False
        if self.scope == "server" and not _can_manage_server(self.bot, interaction):
            await _send(interaction, "You no longer have permission to manage this server's settings.")
            return False
        if self.scope == "owner" and not _is_application_owner(self.bot, interaction):
            await _send(interaction, "Application-owner settings are restricted to the configured application owners.")
            return False
        return True

    async def interaction_check(self, interaction: Any) -> bool:
        return await self.authorized(interaction)

    async def on_error(self, interaction: Any, error: Exception, item: Any) -> None:
        logger.warning("Config action failed (%s: %s)", type(error).__name__, error)
        await _send(interaction, "Could not complete that settings change. Please try again; if it keeps happening, contact the operator.")

    async def on_timeout(self) -> None:
        try:
            await self.command_interaction.edit_original_response(
                content="This menu expired. Run `/config` again.", embed=self.embed(),
                view=None,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            logger.debug("Could not remove expired /config controls", exc_info=True)

    def _build(self) -> None:
        self.clear_items()
        if self.can_choose_scope:
            self.add_item(_ConfigScopeSelect(self))
        self.add_item(_ConfigSettingSelect(self))
        self.add_item(_ConfigCloseButton(self))
        if self.selected_key == "overview":
            if self.scope == "personal":
                for key in ("style", "language", "visibility"):
                    self.add_item(_ConfigShortcutButton(self, key))
            return
        base_row = 2 if self.can_choose_scope else 1
        if self.scope == "personal" and self.selected_key == "byok":
            self.add_item(_ByokProviderSelect(self, base_row))
            self.add_item(_ByokKeyButton(self, base_row + 1))
            last_row = base_row + 2
            self.add_item(_ByokTestButton(self, last_row))
            self.add_item(_ByokDeleteButton(self, last_row))
            return
        if self.scope == "personal":
            choices = _PERSONAL_VALUE_CHOICES.get(self.selected_key)
            if choices:
                self.add_item(_ConfigValueSelect(self, choices))
            if self.selected_key in {"language", "style"}:
                self.add_item(_ConfigEditButton(self))
            self.add_item(_ConfigResetButton(self))
            return
        if self.scope == "server":
            if self.selected_key == "channels":
                self.add_item(_GuildChannelSelect(self, base_row))
                return
            if self.selected_key == "plugins":
                names = _guild_plugin_names(self.bot)
                if names and len(names) <= 25:
                    self.add_item(_GuildPluginSelect(self, base_row))
                self.add_item(_ConfigResetButton(self))
                return
            if self.selected_key == "capabilities":
                self.add_item(_GuildCapabilitySelect(self, base_row))
                return
            choices = [("On", "on"), ("Off", "off")] if self.selected_key in {"moderation", "progress", "ticket"} else None
            if choices:
                self.add_item(_ConfigValueSelect(self, choices))
            self.add_item(_ConfigResetButton(self))
            return
        if self.selected_key == "diagnostics":
            self.add_item(_OwnerDiagnosticsSelect(self, base_row))
        elif self.selected_key in {"tools_enabled", "autonomy_enabled", "message_quota_enabled"}:
            self.add_item(_ConfigValueSelect(self, [("On", "on"), ("Off", "off")]))
        elif self.selected_key == "user_quota":
            self.add_item(_OwnerQuotaButton(self, base_row))
        elif self.selected_key == "reload":
            self.add_item(_OwnerReloadButton(self, base_row))

    def _current_value(self) -> str:
        if self.scope == "personal":
            if self.selected_key == "byok":
                try:
                    status = self._byok_status()
                except VaultUnavailable:
                    return "Unavailable: operator encryption key is not configured or cannot decrypt stored credentials"
                except Exception:
                    return "Unavailable"
                if not status:
                    return "No key saved"
                return (
                    f"{status['provider_label']} / {status['model']} / "
                    f"{status['base_url']} / {status['modalities']} / "
                    f"{status['generation']} / key {status['masked_key']}"
                )
            row = self.personal
            if self.selected_key == "style":
                return str(row.get("personality") or "not set")
            value = row.get("defaults", {}).get(self.selected_key)
            if self.selected_key == "language" and not value:
                return "Use Maxwell's default language"
            return _friendly_personal_value(self.selected_key, value)
        if self.selected_key == "channels":
            from .admin_commands import _control

            target = _control(self.bot).get("guild_solo_channel", {}).get(self.guild_id)
            return f"Restricted to <#{target}>" if target else "All channels"
        if self.selected_key == "plugins":
            names = _guild_plugin_names(self.bot)
            if len(names) > 25:
                return "Unavailable: Discord's plugin selector limit was exceeded"
            overrides = _guild_plugin_overrides(self.bot, self.guild_id)
            if not overrides:
                return "Inheriting global and personal plugin settings"
            enabled = sorted(name for name, value in overrides.items() if value)
            if not enabled:
                return "No optional plugin tools enabled in this server"
            shown = ", ".join(enabled[:8])
            extra = len(enabled) - 8
            return f"Enabled: {shown}" + (f", and {extra} more" if extra > 0 else "")
        if self.selected_key == "capabilities":
            disabled = _guild_disabled(self.bot, self.guild_id)
            names = [GUILD_CAPABILITIES[key] for key in sorted(disabled)]
            return "All groups enabled" if not names else "Disabled: " + ", ".join(names)
        if self.selected_key == "moderation":
            return "Off" if "moderation" in _guild_disabled(self.bot, self.guild_id) else "On"
        if self.selected_key == "progress":
            enabled = bool(getattr(self.bot, "_progress_enabled", lambda _gid: False)(self.guild_id))
            return "On" if enabled else "Off"
        if self.selected_key == "ticket":
            enabled = bool(getattr(self.bot, "_ticket_greeting_enabled", lambda _gid: False)(self.guild_id))
            return "On" if enabled else "Off"
        if self.scope == "owner":
            if self.selected_key == "diagnostics":
                return "Redacted diagnostics and exports"
            if self.selected_key in {"tools_enabled", "autonomy_enabled", "message_quota_enabled"}:
                from .admin_commands import _control

                return "On" if _control(self.bot).get(self.selected_key) else "Off"
            if self.selected_key == "user_quota":
                return "Manage a user's message allowance"
            if self.selected_key == "reload":
                return "Reload trusted bot_control.json"
        return "Unavailable"

    def choice_value(self) -> str:
        if self.scope == "personal":
            return str(self.personal["defaults"].get(self.selected_key, ""))
        current = self._current_value()
        return {"On": "on", "Off": "off"}.get(current, "")

    def _byok_status(self) -> dict[str, str] | None:
        vault = getattr(self.bot, "_byok_vault", None)
        if vault is None or not getattr(vault, "enabled", False):
            raise VaultUnavailable("BYOK encryption is unavailable")
        return vault.status(self.user_id)

    def embed(self) -> discord.Embed:
        text = self.render()
        heading, _, body = text.partition("\n")
        embed = discord.Embed(
            title=heading.strip("#* "), description=body.strip(), color=0x5865F2,
        )
        embed.set_footer(text="Only you can see this menu • Changes save automatically")
        return embed

    def render(self) -> str:
        if self.selected_key == "overview":
            return self.overview()
        if self.scope == "personal":
            title = "Your personal settings"
            selected = _PERSONAL_SETTINGS.get(self.selected_key, ("Setting", ""))[0]
            hint = (
                "These choices apply to your requests. You can override them on an individual `/maxwell` command."
            )
            if self.selected_key in {"style", "language"}:
                hint = "Applies to your replies across channels, servers and DMs."
            value = self._current_value()
            if self.selected_key == "byok":
                hint = (
                    "Keys are encrypted at rest. A public HTTPS OpenAI-compatible "
                    "endpoint receives the request context you send to Maxwell. "
                    "Set modalities to what that model actually accepts."
                )
            if len(value) > 240:
                value = value[:237] + "..."
        elif self.scope == "server":
            guild = getattr(self.command_interaction, "guild", None)
            guild_name = str(getattr(guild, "name", "this server") or "this server")[:80]
            title = f"Settings for {guild_name}"
            selected = _SERVER_SETTINGS.get(self.selected_key, ("Setting", ""))[0]
            hint = "Only the server owner or a member with Manage Server can change these settings."
            if self.selected_key == "plugins":
                hint += " Plugin choices limit tool and guild-event access in this server."
            value = self._current_value()
            if len(value) > 240:
                value = value[:237] + "..."
        else:
            title = "Application owner settings"
            selected = _OWNER_SETTINGS.get(self.selected_key, ("Setting", ""))[0]
            hint = "Global controls and diagnostics are limited to the configured application owner."
            value = self._current_value()
            if len(value) > 240:
                value = value[:237] + "..."
        guidance = _SETTING_HELP.get(self.selected_key, "")
        safe_value = discord.utils.escape_markdown(value)
        return (
            f"**{title}**\n{hint}\n\n"
            f"**{selected}**\n{guidance}\n\n**Current:** {safe_value}\n\n"
            f"{self.notice}\n"
            "Choose an option below, or go back to settings."
        )[:1900]

    def overview(self) -> str:
        if self.scope == "personal":
            personal = self.personal
            defaults = personal["defaults"]
            style = str(personal.get("personality") or "Default personality")
            style = discord.utils.escape_markdown(style[:180] + ("…" if len(style) > 180 else ""))
            language = discord.utils.escape_markdown(str(defaults.get("language") or "Automatic"))
            visibility = _friendly_personal_value("visibility", defaults.get("visibility"))
            return (
                "## Your personal settings\n"
                f"**Personality**\n{style}\n\n"
                f"**Language:** {language}\n"
                f"**App replies:** {visibility}\n\n"
                "Your personality and language follow you across channels, servers and DMs.\n"
                "Use the buttons to change them. More options has web search, answer detail and other command defaults."
            )
        if self.scope == "server":
            guild = getattr(self.command_interaction, "guild", None)
            name = discord.utils.escape_markdown(str(getattr(guild, "name", "this server"))[:80])
            return (
                f"## Settings for {name}\nThese choices affect everyone in this server.\n\n"
                "**Where Maxwell replies** — choose Response channels.\n"
                "**What Maxwell can do** — choose Plugin tools or Enabled capabilities.\n"
                "**Keep chat quieter** — choose Tool progress or Ticket greetings.\n\n"
                "Choose a setting below to see its current state and explanation. "
                "Only the server owner or members with Manage Server can make changes."
            )
        return (
            "## Application owner settings\nThese controls affect the entire hosted bot.\n\n"
            "Choose Runtime diagnostics to inspect the service, Message allowance to manage usage, "
            "or Global tool access to control tool execution.\n\n"
            "Access is checked again whenever a control is used."
        )

    @_config_action()
    async def set_choice(self, interaction: Any, value: str) -> None:
        if not await self.authorized(interaction):
            return
        if self.scope == "personal":
            if self.selected_key not in _PERSONAL_VALUE_CHOICES or value not in _PERSONAL_DEFAULT_VALUES.get(self.selected_key, set()):
                await _send(interaction, "That value is not available for this setting.")
                return
            stored: Any = int(value) if self.selected_key == "context" else value
            await asyncio.to_thread(self.store.set_default, self.user_id, self.selected_key, stored)
            await self._refresh_personal()
        elif self.selected_key == "moderation":
            if value not in {"on", "off"}:
                await _send(interaction, "That setting value is unavailable.")
                return
            disabled = _guild_disabled(self.bot, self.guild_id)
            if value == "off":
                disabled.add("moderation")
            else:
                disabled.discard("moderation")
            try:
                await _set_guild_disabled(self.bot, self.guild_id, disabled)
            except Exception as exc:
                logger.warning("Could not update moderation policy (%s)", type(exc).__name__)
                await _send(interaction, "Could not save the moderation policy.")
                return
        elif self.selected_key in {"tools_enabled", "autonomy_enabled", "message_quota_enabled"} and self.scope == "owner":
            if not _is_application_owner(self.bot, interaction) or value not in {"on", "off"}:
                await _send(interaction, "That global setting is unavailable.")
                return
            from .admin_commands import _set_control

            try:
                await _set_control(self.bot, self.selected_key, value == "on")
            except Exception as exc:
                logger.warning("Could not update global control %s (%s)", self.selected_key, type(exc).__name__)
                await _send(interaction, "Could not save that global setting.")
                return
        elif self.selected_key == "progress":
            if value not in {"on", "off"}:
                await _send(interaction, "That setting value is unavailable.")
                return
            enabled = value == "on"
            enabled_set = getattr(self.bot, "_progress_servers", None)
            disabled_set = getattr(self.bot, "_progress_servers_off", None)
            if not isinstance(enabled_set, set):
                enabled_set = self.bot._progress_servers = set()
            if not isinstance(disabled_set, set):
                disabled_set = self.bot._progress_servers_off = set()
            (enabled_set.add if enabled else enabled_set.discard)(self.guild_id)
            (disabled_set.discard if enabled else disabled_set.add)(self.guild_id)
            saver = getattr(self.bot, "_save_progress_servers", None)
            if callable(saver):
                await asyncio.to_thread(saver)
        elif self.selected_key == "ticket":
            if value not in {"on", "off"}:
                await _send(interaction, "That setting value is unavailable.")
                return
            enabled_set = getattr(self.bot, "_ticket_greeting_servers", None)
            if not isinstance(enabled_set, set):
                enabled_set = self.bot._ticket_greeting_servers = set()
            (enabled_set.add if value == "on" else enabled_set.discard)(self.guild_id)
            saver = getattr(self.bot, "_save_ticket_greeting_servers", None)
            if callable(saver):
                await asyncio.to_thread(saver)
        else:
            await _send(interaction, "That setting cannot be changed with this menu.")
            return
        self.notice = "**Saved.** Your next request will use this choice." if self.scope == "personal" else "**Saved.** This setting is now active."
        self._build()
        await _edit_panel(interaction,
            content=None, embed=self.embed(), view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    def set_text_value(self, key: str, value: str) -> None:
        if self.scope == "personal" and key == "language":
            if not value or len(value) > 80:
                raise ValueError("Enter a language up to 80 characters long. Use Reset to remove it.")
            self.store.set_default(self.user_id, key, value)
            return
        if self.scope == "personal" and key == "style":
            self.store.set_personality(self.user_id, value)
            return
        raise ValueError("That setting cannot be edited as text.")

    def edit_modal(self, *, key: str | None = None) -> _ConfigTextModal | None:
        key = key or self.selected_key
        if self.scope == "personal" and key == "language":
            current = str(self.personal["defaults"].get("language") or "")
            return _ConfigTextModal(
                self, key="language", label="Response language", current=current,
                max_length=80, placeholder="For example: Spanish",
            )
        if self.scope == "personal" and key == "style":
            current = str(self.personal.get("personality") or "")
            return _ConfigTextModal(
                self, key="style", label="Personality", current=current,
                max_length=800, placeholder="For example: Keep replies brief and direct",
            )
        return None

    @_config_action()
    async def reset(self, interaction: Any) -> None:
        if not await self.authorized(interaction):
            return
        if self.selected_key == "overview":
            return
        if self.scope == "personal":
            if self.selected_key == "style":
                await asyncio.to_thread(self.store.set_personality, self.user_id, "")
            elif self.selected_key == "byok":
                await _send(interaction, "Use **Delete key** to remove BYOK credentials.")
                return
            else:
                await asyncio.to_thread(self.store.reset_default, self.user_id, self.selected_key)
        elif self.selected_key in {"capabilities", "moderation"}:
            disabled = _guild_disabled(self.bot, self.guild_id)
            if self.selected_key == "capabilities":
                disabled.clear()
            else:
                disabled.discard("moderation")
            try:
                await _set_guild_disabled(self.bot, self.guild_id, disabled)
            except Exception as exc:
                logger.warning("Could not reset server capabilities (%s)", type(exc).__name__)
                await _send(interaction, "Could not reset that server setting.")
                return
        elif self.scope == "server" and self.selected_key == "plugins":
            try:
                from .admin_commands import _control, _set_control

                mapping = _control(self.bot).get("guild_plugin_overrides", {})
                mapping = dict(mapping) if isinstance(mapping, dict) else {}
                mapping.pop(self.guild_id, None)
                await _set_control(self.bot, "guild_plugin_overrides", json.dumps(mapping))
            except Exception as exc:
                logger.warning("Could not reset server plugin settings (%s)", type(exc).__name__)
                await _send(interaction, "Could not reset the plugin settings.")
                return
        elif self.selected_key == "channels":
            from .admin_commands import _control

            mapping = dict(_control(self.bot).get("guild_solo_channel", {}) or {})
            mapping.pop(self.guild_id, None)
            saver = getattr(self.bot, "_save_solo", None)
            if not callable(saver):
                await _send(interaction, "Server channel settings are unavailable right now.")
                return
            await saver(mapping, self.guild_id, unblock_autonomy=True)
        elif self.selected_key == "progress":
            getattr(self.bot, "_progress_servers", set()).discard(self.guild_id)
            getattr(self.bot, "_progress_servers_off", set()).discard(self.guild_id)
            saver = getattr(self.bot, "_save_progress_servers", None)
            if callable(saver):
                await asyncio.to_thread(saver)
        elif self.selected_key == "ticket":
            getattr(self.bot, "_ticket_greeting_servers", set()).discard(self.guild_id)
            saver = getattr(self.bot, "_save_ticket_greeting_servers", None)
            if callable(saver):
                await asyncio.to_thread(saver)
        if self.scope == "personal":
            await self._refresh_personal()
        self.notice = "**Reset.** The default is restored."
        self._build()
        await _edit_panel(interaction,
            content=None, embed=self.embed(), view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def _handle_config(bot: Any, interaction: Any) -> bool:
    data = ui._interaction_data(interaction)
    if str(data.get("name") or "") != "config":
        return False
    store = _preference_store(bot)
    if store is None:
        await _send(interaction, "Personal settings are unavailable right now.")
        return True
    await _acknowledge(interaction, thinking=True)
    personal = await asyncio.to_thread(store.get, _user_id(interaction))
    panel = _ConfigPanel(bot, store, interaction, personal=personal)
    response = getattr(interaction, "response", None)
    sender = getattr(response, "send_message", None)
    done = getattr(response, "is_done", None)
    if callable(done) and done():
        await interaction.edit_original_response(
            content=None, embed=panel.embed(), view=panel,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return True
    if not callable(sender):
        await _send(interaction, panel.render())
        return True
    await sender(
        None, embed=panel.embed(), view=panel, ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )
    return True


async def _handle_cancel(bot: Any, interaction: Any) -> bool:
    data = ui._interaction_data(interaction)
    if str(data.get("name") or "") != "cancel":
        return False
    uid = _user_id(interaction)
    source_channel_id = str(
        getattr(interaction, "channel_id", None)
        or getattr(getattr(interaction, "channel", None), "id", None)
        or ""
    )
    keys = [source_channel_id]
    private_key = ui.private_channel_key(interaction)
    if private_key not in keys:
        keys.append(private_key)
    active = getattr(bot, "_active_requests", None) or {}
    owners = getattr(bot, "_active_request_user", None) or {}
    task = None
    for key in keys:
        candidate = active.get(key)
        if candidate is not None and not candidate.done() and str(owners.get(key) or "") == uid:
            task = candidate
            break
    if task is None:
        await _send(interaction, "You have no running Maxwell request in this interaction context.")
        return True
    task.cancel()
    await _send(interaction, "Cancelled your running Maxwell request.")
    return True


def install_command_suite(bot: Any, store: Any) -> None:
    """Register private settings and cancellation alongside discovery essentials."""
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
    ui.register_interaction_handler(_handle_cancel, priority=5, name="command_cancel")
    for stale_name in ("personality", "diagnostics", "maintenance"):
        ui.unregister_command(stale_name)
    bot._maxwell_command_suite_installed = True


def uninstall_command_suite(bot: Any) -> None:
    global _ACTIVE_STORE
    for command in command_definitions():
        ui.unregister_command(str(command.get("name") or ""))
    for name in ("command_config", "command_cancel"):
        ui.unregister_interaction_handler(name)
    if getattr(bot, "_maxwell_command_suite_installed", False):
        del bot._maxwell_command_suite_installed
    from . import user_install_features

    user_install_features.set_user_preference_store(None)
    _ACTIVE_STORE = None

__all__ = [
    "CONFIG_COMMAND",
    "CANCEL_COMMAND",
    "command_definitions",
    "install_command_suite",
    "uninstall_command_suite",
]
