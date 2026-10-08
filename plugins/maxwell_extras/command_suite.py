"""Purpose-specific Discord application commands for Maxwell."""

from __future__ import annotations

import logging
import asyncio
import json
from typing import Any

import discord

import user_install as ui
from control_defaults import GUILD_CAPABILITIES
from .config_ui import _acknowledge, _config_action, _edit_panel, _send
from . import config_connections as connections


logger = logging.getLogger(__name__)
_ACTIVE_STORE: Any = None

_APP_META = {"type": 1, "integration_types": [0, 1], "contexts": [0, 1, 2]}
_PERSONAL_DEFAULT_VALUES = {
    "mode": {"ask", "research", "summarize", "explain", "rewrite", "translate", "brainstorm", "code"},
    "web": {"auto", "search", "off"},
    "detail": {"quick", "balanced", "deep"},
    "context": {str(count) for count in ui.USER_INSTALL_CONTEXT_COUNTS},
    "visibility": {"private", "public"},
    "language": {"auto", "English", "Spanish"},
}
_PERSONAL_SETTINGS = {
    "mode": ("Response mode", "Choose how Maxwell approaches your requests."),
    "web": ("Web research", "Choose when Maxwell searches the web."),
    "detail": ("Answer detail", "Choose how much detail Maxwell gives."),
    "context": ("Recent messages", "Choose up to 1,000 recent channel messages for app requests."),
    "visibility": ("Reply visibility", "Choose whether /maxwell and message app replies are private or public."),
    "language": ("Language", "Set a preferred language, such as English or Spanish."),
    "style": ("Personality", "Your reply personality across channels, servers and DMs."),
    "byok": ("AI connection", "Connect your own AI account with an API key."),
}
_SERVER_SETTINGS = {
    "channels": ("Response channels", "Limit Maxwell to one channel in this server or allow all channels."),
    "plugins": ("Extra tools", "Choose which optional plugin tools are available in this server."),
    "capabilities": ("Allowed tools", "Choose which tool groups are available in this server."),
    "moderation": ("Moderation", "Enable or disable Maxwell's moderation tools in this server."),
    "progress": ("Progress updates", "Show or hide progress messages while Maxwell works."),
    "ticket": ("Ticket greetings", "Enable or disable greetings in ticket channels."),
}
_OWNER_SETTINGS = {
    "diagnostics": ("Diagnostics", "View redacted application diagnostics."),
    "tools_enabled": ("Tools across the bot", "Enable or disable model tool execution globally."),
    "autonomy_enabled": ("Automatic activity", "Enable or disable autonomous activity globally."),
    "reload": ("Reload controls", "Reload the trusted bot_control.json file."),
}
_PERSONAL_VALUE_CHOICES = {
    "language": [("Automatic — follow the conversation", "auto"), ("English", "English"), ("Spanish", "Spanish")],
    "mode": [
        ("Answer normally", "ask"), ("Research", "research"),
        ("Summarize", "summarize"), ("Explain", "explain"),
        ("Rewrite", "rewrite"), ("Translate", "translate"),
        ("Brainstorm", "brainstorm"), ("Write code", "code"),
    ],
    "web": [("Automatic — Maxwell decides", "auto"), ("Always search", "search"), ("Off", "off")],
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
    "web": "Let Maxwell decide when to search, always search, or turn search off for app requests.",
    "detail": "Brief is the default. Balanced adds explanation. Deep asks for a fuller explanation.",
    "context": "Choose up to 1,000 messages. Very long conversations may include fewer messages. Only recent messages Maxwell can access in this channel are included. Choose none to use just your request.",
    "visibility": "Applies to /maxwell and message app actions. Public replies appear in the channel; Private replies are only visible to you. You can override this on /maxwell. Servers must allow Use External Apps for user-installed apps to reply publicly.",
    "language": "For example, enter Spanish or English. Reset lets Maxwell choose the language again.",
    "style": "For example: Keep replies short and skip emojis. Applies to your messages, mentions and commands everywhere. Changes only your replies.",
    "byok": "Paste a public HTTPS OpenAI-compatible endpoint, the model ID, and the modalities that model accepts. The key is encrypted. Private hosts are rejected.",
    "channels": "Choose a response channel, or use Allow all channels. The channel picker includes every text channel in this server.",
    "plugins": "Select the optional tools to allow in this server. Reset inherits the existing global and personal choices.",
    "capabilities": "Selected groups are allowed. Unselect a group to disable its tools. Discord permissions still apply.",
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
        return {value: label for label, value in _PERSONAL_VALUE_CHOICES["detail"]}.get(text, text)
    if key == "context":
        return "No recent messages" if text == "0" else f"Last {text} messages"
    if key == "visibility":
        return {"private": "Private — only you", "public": "Public — visible in this channel"}.get(text, "Private — only you")
    if key == "language" and text in {"auto", "not set"}:
        return "Automatic"
    return text


class _ConfigNavButton(discord.ui.Button):
    """One click opens a settings screen; navigation never writes preferences."""
    def __init__(self, panel: "_ConfigPanel", key: str, *, label: str | None = None, row: int = 1, scope: str | None = None):
        settings = {"personal": _PERSONAL_SETTINGS, "server": _SERVER_SETTINGS, "owner": _OWNER_SETTINGS}[panel.scope]
        super().__init__(
            label=label or settings.get(key, (key.title(), ""))[0],
            style=discord.ButtonStyle.secondary,
            custom_id=f"maxwell:config:nav:{scope or panel.scope}:{key}:{row}", row=row,
        )
        self.panel = panel
        self.key = key
        self.target_scope = scope

    @_config_action()
    async def callback(self, interaction: Any) -> None:
        scope = self.target_scope or self.panel.scope
        if scope == "server" and not _can_manage_server(self.panel.bot, interaction):
            await _send(interaction, "You no longer have permission to manage this server's settings.")
            return
        if scope == "owner" and not _is_application_owner(self.panel.bot, interaction):
            await _send(interaction, "Application-owner settings are restricted to the configured application owners.")
            return
        self.panel.scope = scope
        self.panel.selected_key = self.key
        self.panel.notice = ""
        self.panel._build()
        await _edit_panel(interaction, content=None, embed=self.panel.embed(), view=self.panel,
                          allowed_mentions=discord.AllowedMentions.none())


class _ConfigValueSelect(discord.ui.Select):
    def __init__(self, panel: "_ConfigPanel", choices: list[tuple[str, str]], *, key: str | None = None, row: int = 1):
        self.key = key or panel.selected_key
        current = panel.choice_value(self.key)
        if self.key == "language" and current not in {value for _, value in choices}:
            choices = [*choices, (current, current)]
        label = (_PERSONAL_SETTINGS if panel.scope == "personal" else
                 _SERVER_SETTINGS if panel.scope == "server" else _OWNER_SETTINGS).get(self.key, ("Choose an option", ""))[0]
        options = [
            discord.SelectOption(label=label, value=value, default=value == current, description=_CHOICE_HELP.get(value))
            for label, value in choices
        ]
        super().__init__(
            placeholder=f"{label} — saves immediately",
            min_values=1,
            max_values=1,
            options=options,
            custom_id=f"maxwell:config:value:{self.key}",
            row=row,
        )
        self.panel = panel

    async def callback(self, interaction: Any) -> None:
        await self.panel.set_choice(interaction, self.values[0], key=self.key, context=self._config_context)


class _GuildCapabilitySelect(discord.ui.Select):
    def __init__(self, panel: "_ConfigPanel", row: int):
        disabled = _guild_disabled(panel.bot, panel.guild_id)
        options = [
            discord.SelectOption(
                label=label[:100],
                value=key,
                default=key not in disabled,
                description=f"{label} tools {('disabled' if key in disabled else 'enabled')}"[:100],
            )
            for key, label in GUILD_CAPABILITIES.items()
        ]
        super().__init__(
            placeholder="Select tools allowed in this server",
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
        enabled = {str(value) for value in self.values}
        if not enabled <= set(GUILD_CAPABILITIES):
            await _send(interaction, "That capability group is not available.")
            return
        try:
            await _set_guild_disabled(self.panel.bot, self.panel.guild_id, set(GUILD_CAPABILITIES) - enabled)
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
                    label=name.replace("_", " ").title()[:100],
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


class _GuildChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, panel: "_ConfigPanel", row: int):
        from .admin_commands import _control

        target = _control(panel.bot).get("guild_solo_channel", {}).get(panel.guild_id)
        super().__init__(
            placeholder="Choose one response channel", min_values=1, max_values=1,
            channel_types=[discord.ChannelType.text, discord.ChannelType.news],
            default_values=[discord.Object(id=int(target))] if str(target or "").isdigit() else [],
            custom_id="maxwell:config:channels", row=row,
        )
        self.panel = panel

    @_config_action(thinking=False)
    async def callback(self, interaction: Any) -> None:
        if not await self.panel.authorized(interaction):
            return
        value = str(getattr(self.values[0], "id", self.values[0]))
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
            channel = next(channel for channel in guild.text_channels if str(channel.id) == value)
            permissions_for = getattr(channel, "permissions_for", None)
            bot_member = getattr(guild, "me", None)
            if callable(permissions_for) and bot_member is not None:
                permissions = permissions_for(bot_member)
                if not permissions.view_channel or not permissions.send_messages:
                    await _send(interaction, "Maxwell cannot read and send messages in that channel. Update its channel permissions or choose another.")
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


class _ConfigEditButton(discord.ui.Button):
    def __init__(self, panel: "_ConfigPanel"):
        super().__init__(
            label="Edit personality" if panel.selected_key == "style" else "Another language…",
            style=discord.ButtonStyle.primary,
            custom_id="maxwell:config:edit",
            row=2,
        )
        self.panel = panel

    async def callback(self, interaction: Any) -> None:
        if not await self.panel.authorized(interaction):
            return
        if (self._config_context != (self.panel.scope, self.panel.selected_key)
                or self.panel.closed or self.panel.is_finished()):
            await _send(interaction, "Open the current settings screen to edit it.")
            return
        modal = self.panel.edit_modal()
        if modal is None:
            await _send(interaction, "That setting cannot be edited as text.")
            return
        await interaction.response.send_modal(modal)


class _ConfigResetButton(discord.ui.Button):
    def __init__(self, panel: "_ConfigPanel"):
        super().__init__(
            label="Allow all channels" if panel.selected_key == "channels" else "Restore default",
            style=discord.ButtonStyle.secondary,
            custom_id="maxwell:config:reset",
            row=2,
        )
        self.panel = panel

    async def callback(self, interaction: Any) -> None:
        await self.panel.reset(interaction, context=self._config_context)


class _ConfigTextModal(discord.ui.Modal):
    def __init__(self, panel: "_ConfigPanel", *, key: str, label: str, current: str, max_length: int, placeholder: str):
        super().__init__(title=f"Edit {label.lower()}", timeout=180)
        self.panel = panel
        self.key = key
        self._config_context = (panel.scope, panel.selected_key)
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


class _ConfigPanel(discord.ui.View):
    def __init__(self, bot: Any, store: Any, interaction: Any, *, personal: dict | None = None):
        super().__init__(timeout=300)
        self.bot = bot
        self.store = store
        self.command_interaction = interaction
        self.closed = False
        self.user_id = _user_id(interaction)
        self.guild_id = _guild_id(interaction)
        self._action_lock = asyncio.Lock()
        self.personal = personal if personal is not None else store.get(self.user_id)
        self.scope = "personal"
        self.selected_key = "overview"
        self.notice = ""
        self.selected_provider = "openai"
        self.connection_status = None
        self.connection_present = False
        self.connection_error = "" if connections.available(self) else "unavailable"
        self.can_manage_server = bool(self.guild_id and _can_manage_server(bot, interaction))
        self.is_owner = _is_application_owner(bot, interaction)
        self.can_choose_scope = self.can_manage_server or self.is_owner
        self._build()

    def add_item(self, item):
        item._config_context = (self.scope, self.selected_key)
        return super().add_item(item)

    def stop(self) -> None:
        self.closed = True
        super().stop()

    async def _refresh_personal(self) -> None:
        self.personal = await asyncio.to_thread(self.store.get, self.user_id)
        await self._refresh_connection()

    async def _refresh_connection(self) -> None:
        vault = getattr(self.bot, "_byok_vault", None)
        self.connection_status = None
        self.connection_error = ""
        if vault is None:
            self.connection_present = False
            self.connection_error = "unavailable"
            return
        try:
            self.connection_present = await asyncio.to_thread(vault.has_credential, self.user_id)
            if not vault.enabled:
                self.connection_error = "unavailable"
                return
            self.connection_status = await asyncio.to_thread(vault.status, self.user_id)
        except Exception:
            self.connection_error = "unreadable"


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
        logger.warning("Config action failed (%s)", type(error).__name__)
        await _send(interaction, "Could not complete that settings change. Please try again; if it keeps happening, contact the operator.")

    async def on_timeout(self) -> None:
        self.closed = True
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
            for scope, label in (("personal", "My settings"), ("server", "Server settings"), ("owner", "Bot controls")):
                if scope == "server" and not self.can_manage_server or scope == "owner" and not self.is_owner:
                    continue
                button = _ConfigNavButton(self, "overview", label=label, row=0, scope=scope)
                button.style = discord.ButtonStyle.primary if scope == self.scope else discord.ButtonStyle.secondary
                self.add_item(button)
        self.add_item(_ConfigCloseButton(self))
        if self.selected_key != "overview":
            parent = "byok" if self.selected_key in {"byok_advanced", "byok_remove"} else (
                "replies" if self.scope == "personal" and self.selected_key in {"mode", "web", "context"} else "overview")
            self.add_item(_ConfigNavButton(self, parent, label="Back", row=4))
        if self.selected_key == "overview":
            keys = ("style", "language", "replies", "byok") if self.scope == "personal" else tuple(
                _SERVER_SETTINGS if self.scope == "server" else _OWNER_SETTINGS)
            for i, key in enumerate(keys):
                self.add_item(_ConfigNavButton(self, key, label="Replies & context" if key == "replies" else None, row=1 + i // 2))
            return
        if self.scope == "personal" and self.selected_key.startswith("byok"):
            connections.build(self, _ConfigNavButton)
            return
        if self.scope == "personal":
            if self.selected_key == "replies":
                for row, key in ((1, "visibility"), (2, "detail")):
                    self.add_item(_ConfigValueSelect(self, _PERSONAL_VALUE_CHOICES[key], key=key, row=row))
                for key in ("mode", "web", "context"):
                    self.add_item(_ConfigNavButton(self, key, row=3))
                return
            choices = _PERSONAL_VALUE_CHOICES.get(self.selected_key)
            if choices:
                self.add_item(_ConfigValueSelect(self, choices))
            if self.selected_key in {"language", "style"}:
                self.add_item(_ConfigEditButton(self))
            self.add_item(_ConfigResetButton(self))
            return
        if self.scope == "server":
            if self.selected_key == "channels":
                self.add_item(_GuildChannelSelect(self, 1))
            elif self.selected_key == "plugins":
                names = _guild_plugin_names(self.bot)
                if names and len(names) <= 25:
                    self.add_item(_GuildPluginSelect(self, 1))
            elif self.selected_key == "capabilities":
                self.add_item(_GuildCapabilitySelect(self, 1))
            elif self.selected_key in {"moderation", "progress", "ticket"}:
                self.add_item(_ConfigValueSelect(self, [("Enabled", "on"), ("Disabled", "off")]))
            self.add_item(_ConfigResetButton(self))
            return
        if self.selected_key == "diagnostics":
            self.add_item(_OwnerDiagnosticsSelect(self, 1))
        elif self.selected_key in {"tools_enabled", "autonomy_enabled"}:
            self.add_item(_ConfigValueSelect(self, [("Enabled", "on"), ("Disabled", "off")]))
        elif self.selected_key == "reload":
            self.add_item(_OwnerReloadButton(self, 1))

    def _current_value(self) -> str:
        if self.scope == "personal":
            if self.selected_key.startswith("byok"):
                return connections.summary(self)
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
            if self.selected_key in {"tools_enabled", "autonomy_enabled"}:
                from .admin_commands import _control

                return "On" if _control(self.bot).get(self.selected_key) else "Off"
            if self.selected_key == "reload":
                return "Reload trusted bot_control.json"
        return "Unavailable"

    def choice_value(self, key: str | None = None) -> str:
        if self.scope == "personal":
            value = str(self.personal["defaults"].get(key or self.selected_key, ""))
            return "auto" if (key or self.selected_key) == "language" and not value else value
        current = self._current_value()
        return {"On": "on", "Off": "off"}.get(current, "")

    def embed(self) -> discord.Embed:
        text = self.render()
        heading, _, body = text.partition("\n")
        embed = discord.Embed(
            title=heading.strip("#* "), description=body.strip(), color=0x5865F2,
        )
        embed.set_footer(text="Only you can see this menu • Choices and submitted forms save immediately")
        return embed

    def render(self) -> str:
        if self.selected_key == "overview":
            return self.overview()
        if self.scope == "personal" and self.selected_key.startswith("byok"):
            return connections.render(self)
        if self.scope == "personal" and self.selected_key == "replies":
            defaults = self.personal["defaults"]
            lines = [f"**{_PERSONAL_SETTINGS[key][0]}:** {_friendly_personal_value(key, defaults[key])}"
                     for key in ("visibility", "detail", "mode", "web", "context")]
            return ("## Replies & context\nDefaults for `/maxwell` and message app actions. "
                    "Choose who sees your replies and how much detail you want.\n\n" + "\n".join(lines) +
                    "\n\nUse the buttons for response mode, web search and recent messages. "
                    "Private app replies do not read public channel history.\n\n" + self.notice)
        settings = {"personal": _PERSONAL_SETTINGS, "server": _SERVER_SETTINGS, "owner": _OWNER_SETTINGS}[self.scope]
        selected = settings.get(self.selected_key, ("Setting", ""))[0]
        area = {"personal": "Your personal settings", "server": "This server", "owner": "Application owner settings"}[self.scope]
        value = self._current_value()
        guidance = _SETTING_HELP.get(self.selected_key, settings.get(self.selected_key, ("", ""))[1])
        return (f"## {area} · {selected}\n{guidance}\n\n"
                f"**Current:** {discord.utils.escape_markdown(value[:800])}\n\n{self.notice}")[:1900]

    def overview(self) -> str:
        if self.scope == "personal":
            defaults = self.personal["defaults"]
            style = discord.utils.escape_markdown(str(self.personal.get("personality") or "Default personality")[:160])
            language = discord.utils.escape_markdown(str(defaults.get("language") or "Automatic"))
            visibility = _friendly_personal_value("visibility", defaults.get("visibility"))
            active = discord.utils.escape_markdown(connections.summary(self))
            return ("## Your personal settings\nChoose what you want to change.\n\n"
                    f"**Personality:** {style}\n**Language:** {language}\n"
                    f"**App replies:** {visibility}\n**AI connection:** {active}\n\n"
                    "Personality and language follow you everywhere. A saved AI connection replaces Maxwell's model for your messages. Replies & context apply to app requests.")
        if self.scope == "server":
            guild = getattr(self.command_interaction, "guild", None)
            name = discord.utils.escape_markdown(str(getattr(guild, "name", "this server"))[:80])
            return (f"## Settings for {name}\nThese choices affect everyone in this server.\n\n"
                    "**Response channels** — where Maxwell can reply.\n"
                    "**Allowed tools** and **Extra tools** — what Maxwell can do.\n"
                    "**Moderation**, **Progress updates**, **Ticket greetings** — how it behaves.\n\n"
                    "Choose a button below. You need Manage Server to change these settings.")
        return ("## Application owner settings\nThese controls affect the entire bot.\n\n"
                "Open diagnostics, manage global features or reload saved controls. "
                "Choose a button below. Only configured bot owners have access.")

    @_config_action()
    async def set_choice(self, interaction: Any, value: str, *, key: str | None = None) -> None:
        if not await self.authorized(interaction):
            return
        key = key or self.selected_key
        if self.scope == "personal":
            saved_language = key == "language" and value == self.personal["defaults"].get("language")
            if key not in _PERSONAL_VALUE_CHOICES or (value not in _PERSONAL_DEFAULT_VALUES.get(key, set()) and not saved_language):
                await _send(interaction, "That value is not available for this setting.")
                return
            if key == "language" and value == "auto":
                await asyncio.to_thread(self.store.reset_default, self.user_id, key)
            else:
                stored: Any = int(value) if key == "context" else value
                await asyncio.to_thread(self.store.set_default, self.user_id, key, stored)
            await self._refresh_personal()
        elif key == "moderation":
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
        elif key in {"tools_enabled", "autonomy_enabled"} and self.scope == "owner":
            if not _is_application_owner(self.bot, interaction) or value not in {"on", "off"}:
                await _send(interaction, "That global setting is unavailable.")
                return
            from .admin_commands import _set_control

            try:
                await _set_control(self.bot, key, value == "on")
            except Exception as exc:
                logger.warning("Could not update global control %s (%s)", key, type(exc).__name__)
                await _send(interaction, "Could not save that global setting.")
                return
        elif key == "progress":
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
        elif key == "ticket":
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
        self.notice = f"**Saved.** {_PERSONAL_SETTINGS.get(key, (key, ''))[0]}: {_friendly_personal_value(key, value)}." if self.scope == "personal" else "**Saved.** This setting is now active."
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
    await panel._refresh_connection()
    if panel.connection_status:
        panel.selected_provider = panel.connection_status["provider"]
    panel._build()
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
