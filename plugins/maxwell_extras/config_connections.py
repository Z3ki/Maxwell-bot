"""Guided personal AI connections for the private /config panel."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import discord

from maxwell_core.providers.errors import (
    ProviderAuthenticationError,
    ProviderInvalidRequestError,
    ProviderRateLimitError,
    ProviderUsageExhaustedError,
)
from .byok import (
    PROVIDERS,
    effective_settings,
    make_request_provider,
    parse_model_support,
)
from .config_ui import _config_action, _edit_panel, _send

logger = logging.getLogger(__name__)
KEY_URLS = {
    "openai": "https://platform.openai.com/api-keys",
    "openrouter": "https://openrouter.ai/settings/keys",
    "groq": "https://console.groq.com/keys",
}


async def refresh_panel(panel: Any, interaction: Any, notice: str) -> None:
    await panel._refresh_connection()
    panel.notice = notice
    panel._build()
    # A deferred modal or connection test has its own ephemeral response;
    # refresh the original settings message, not that separate response.
    await _send(interaction, notice)
    try:
        await panel.command_interaction.edit_original_response(
            content=None,
            embed=panel.embed(),
            view=panel,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except Exception:
        logger.debug("Could not refresh AI connection panel", exc_info=True)


def available(panel: Any) -> bool:
    vault = getattr(panel.bot, "_byok_vault", None)
    return bool(vault is not None and getattr(vault, "enabled", False))


def active_selected(panel: Any) -> bool:
    status = panel.connection_status
    return bool(status and status["provider"] == panel.selected_provider)


class ProviderSelect(discord.ui.Select):
    def __init__(self, panel: Any):
        super().__init__(
            placeholder="1. Choose an AI provider",
            row=1,
            custom_id="maxwell:config:connection:provider",
            options=[
                discord.SelectOption(
                    label=("Other provider" if key == "custom" else value["label"]),
                    value=key,
                    default=key == panel.selected_provider,
                    description=(
                        "Use a custom OpenAI-compatible API"
                        if key == "custom"
                        else "Connect with your own API key"
                    ),
                )
                for key, value in PROVIDERS.items()
            ],
            disabled=not available(panel) or bool(panel.connection_error),
        )
        self.panel = panel

    @_config_action()
    async def callback(self, interaction: Any) -> None:
        if self.values[0] not in PROVIDERS:
            return
        self.panel.selected_provider = self.values[0]
        self.panel.notice = "Provider selected. Save a connection to use it."
        self.panel._build()
        await _edit_panel(
            interaction, content=None, embed=self.panel.embed(), view=self.panel
        )


class ConnectModal(discord.ui.Modal):
    def __init__(self, panel: Any):
        provider = panel.selected_provider
        super().__init__(
            title=f"Connect {PROVIDERS[provider]['label']}"[:45], timeout=180
        )
        self.panel = panel
        self.provider = provider
        self._config_context = (panel.scope, panel.selected_key)
        saved = panel.connection_status if active_selected(panel) else None
        self.model = discord.ui.TextInput(
            label="Model name",
            max_length=120,
            required=True,
            default=(saved or {}).get("model")
            or PROVIDERS[provider]["suggested_model"]
            or None,
            placeholder="Copy the model ID from your provider's model list",
        )
        self.api_key = discord.ui.TextInput(
            label="API key",
            max_length=512,
            required=not bool(saved),
            placeholder="Leave blank to keep your saved key"
            if saved
            else "Paste your provider's API key",
        )
        self.add_item(self.api_key)
        self.add_item(self.model)
        self.endpoint = None
        if provider == "custom":
            self.endpoint = discord.ui.TextInput(
                label="API base URL",
                max_length=300,
                required=True,
                default=(saved or {}).get("base_url") or None,
                placeholder="https://api.example.com/v1",
            )
            self.add_item(self.endpoint)

    @_config_action(thinking=True)
    async def on_submit(self, interaction: Any) -> None:
        if self.panel.scope != "personal" or not available(self.panel):
            await _send(
                interaction,
                "Personal AI connections are unavailable. Ask the bot operator for help.",
            )
            return
        if self.provider != self.panel.selected_provider:
            await _send(
                interaction, "The selected provider changed. Open Connect again."
            )
            return
        vault = self.panel.bot._byok_vault
        try:
            existing = await asyncio.to_thread(vault.get, self.panel.user_id)
            same = (
                existing if existing and existing["provider"] == self.provider else None
            )
            secret = str(self.api_key.value or "").strip() or (same or {}).get(
                "api_key", ""
            )
            if not secret:
                raise ValueError("Paste an API key for this provider.")
            settings = (
                effective_settings((same or {}).get("settings"))
                if same
                else parse_model_support("text, tools", "")
            )
            endpoint = (
                str(self.endpoint.value or "")
                if self.endpoint
                else (same or {}).get("base_url", "")
            )
            await asyncio.to_thread(
                vault.save,
                self.panel.user_id,
                self.provider,
                str(self.model.value or ""),
                secret,
                base_url=endpoint,
                settings=settings,
            )
        except ValueError as exc:
            await _send(interaction, str(exc))
            return
        except Exception as exc:
            logger.warning("AI connection save failed (%s)", type(exc).__name__)
            await _send(
                interaction,
                "Could not save the connection. Try again or contact the bot operator.",
            )
            return
        await refresh_panel(
            self.panel,
            interaction,
            "Saved. Your messages will use this connection. Test it below.",
        )


class ConnectButton(discord.ui.Button):
    def __init__(self, panel: Any):
        super().__init__(
            label="Edit connection"
            if active_selected(panel)
            else "2. Connect provider",
            style=discord.ButtonStyle.primary,
            row=2,
            custom_id="maxwell:config:connection:connect",
            disabled=not available(panel) or bool(panel.connection_error),
        )
        self.panel = panel
        self.provider = panel.selected_provider

    async def callback(self, interaction: Any) -> None:
        if not await self.panel.authorized(interaction):
            return
        if (
            self._config_context != (self.panel.scope, self.panel.selected_key)
            or self.panel.closed
            or self.panel.is_finished()
        ):
            await _send(
                interaction, "Open the current AI connection screen to continue."
            )
            return
        if (
            not available(self.panel)
            or self.panel.connection_error
            or self.panel.scope != "personal"
        ):
            await _send(
                interaction,
                "Personal AI connections are unavailable. Ask the bot operator for help.",
            )
            return
        if self.provider != self.panel.selected_provider:
            await _send(
                interaction,
                "The selected provider changed. Use its current Connect button.",
            )
            return
        # Cached redacted status makes opening the modal immediate, with no disk
        # read before Discord's three-second acknowledgement deadline.
        await interaction.response.send_modal(ConnectModal(self.panel))


class TestButton(discord.ui.Button):
    def __init__(self, panel: Any):
        super().__init__(
            label="Test connection",
            row=3,
            custom_id="maxwell:config:connection:test",
            disabled=not active_selected(panel) or not available(panel),
        )
        self.panel = panel
        self.provider = panel.selected_provider

    @_config_action(thinking=True)
    async def callback(self, interaction: Any) -> None:
        provider = None
        try:
            if self.provider != self.panel.selected_provider:
                await _send(
                    interaction,
                    "The selected provider changed. Use its current Test button.",
                )
                return
            if not available(self.panel):
                await _send(interaction, "Personal AI connections are unavailable.")
                return
            credential = await asyncio.to_thread(
                self.panel.bot._byok_vault.get, self.panel.user_id
            )
            if not credential or credential["provider"] != self.panel.selected_provider:
                await _send(interaction, "Connect the selected provider first.")
                return
            provider = make_request_provider(self.panel.bot, credential)
            # Saved temperature and output cap are used. Forcing temperature 0
            # makes some hosts demand top_p, and a 16-token cap cuts off a
            # model that thinks before it answers.
            await asyncio.wait_for(
                provider.generate_response(
                    [{"role": "user", "content": "Reply with OK."}],
                    timeout=20,
                    disable_reasoning=True,
                ),
                timeout=45,
            )
        except ProviderAuthenticationError:
            notice = "Key rejected. Edit the connection and check your API key and account permissions."
        except ProviderUsageExhaustedError:
            notice = (
                "Your provider has no available API credit. Check its billing page."
            )
        except ProviderRateLimitError:
            notice = "Your provider is busy or rate limited. Try the test again later."
        except ProviderInvalidRequestError:
            notice = "The provider rejected this model or its settings. Check the model name and Advanced settings."
        except TimeoutError:
            notice = "The provider did not respond in time. Check the API base URL or try again later."
        except Exception as exc:
            logger.info("AI connection test failed (%s)", type(exc).__name__)
            notice = "Could not reach the provider. Check the API base URL, model name and API key."
        else:
            notice = "Connection works. The provider answered a small test request."
        finally:
            if provider is not None:
                try:
                    await provider.close()
                except Exception:
                    logger.debug("Could not close connection test client")
        await refresh_panel(self.panel, interaction, notice)


async def update_settings(
    panel: Any, interaction: Any, settings: dict, *, endpoint: str | None = None
) -> None:
    vault = panel.bot._byok_vault
    credential = await asyncio.to_thread(vault.get, panel.user_id)
    if not credential or credential["provider"] != panel.selected_provider:
        await _send(interaction, "Connect the selected provider first.")
        return
    await asyncio.to_thread(
        vault.save,
        panel.user_id,
        credential["provider"],
        credential["model"],
        credential["api_key"],
        base_url=credential["base_url"] if endpoint is None else endpoint,
        settings=settings,
    )
    await refresh_panel(
        panel, interaction, "Saved. Your next app request will use these settings."
    )


class FeaturesSelect(discord.ui.Select):
    def __init__(self, panel: Any):
        status = panel.connection_status or {}
        selected = set(status.get("modalities", "").replace(",", " ").split())
        super().__init__(
            placeholder="Select what this model supports",
            row=1,
            min_values=0,
            max_values=3,
            custom_id="maxwell:config:connection:features",
            options=[
                discord.SelectOption(
                    label=label,
                    value=key,
                    default=key in selected,
                    description=description,
                )
                for key, label, description in (
                    ("vision", "Images", "Allow image attachments as model input"),
                    ("audio", "Audio", "Allow audio attachments as model input"),
                    (
                        "tools",
                        "Maxwell tools",
                        "Let this model call Maxwell's available tools",
                    ),
                )
            ],
        )
        self.panel = panel
        self.provider = panel.selected_provider

    @_config_action()
    async def callback(self, interaction: Any) -> None:
        if self.provider != self.panel.selected_provider or not active_selected(
            self.panel
        ):
            await _send(interaction, "This connection changed. Open Advanced again.")
            return
        if not set(self.values) <= {"vision", "audio", "tools"}:
            return
        status = self.panel.connection_status
        settings = parse_model_support(
            "text " + " ".join(self.values), status["generation"]
        )
        await update_settings(self.panel, interaction, settings)


class ReasoningSelect(discord.ui.Select):
    def __init__(self, panel: Any):
        current = "reasoning=on" in (panel.connection_status or {}).get(
            "generation", ""
        )
        super().__init__(
            placeholder="Reasoning support",
            row=2,
            custom_id="maxwell:config:connection:reasoning",
            options=[
                discord.SelectOption(
                    label=label, value=value, default=current == enabled
                )
                for label, value, enabled in (
                    ("Reasoning off", "off", False),
                    ("Reasoning on — model must support it", "on", True),
                )
            ],
        )
        self.panel = panel
        self.provider = panel.selected_provider

    @_config_action()
    async def callback(self, interaction: Any) -> None:
        if self.provider != self.panel.selected_provider or not active_selected(
            self.panel
        ):
            await _send(interaction, "This connection changed. Open Advanced again.")
            return
        status = self.panel.connection_status
        settings = parse_model_support(status["modalities"], status["generation"])
        settings["reasoning"] = self.values[0] == "on"
        await update_settings(self.panel, interaction, settings)


class AdvancedModal(discord.ui.Modal):
    def __init__(self, panel: Any, *, endpoint: bool = False):
        super().__init__(
            title="API base URL" if endpoint else "Model response settings", timeout=180
        )
        self.panel = panel
        self.provider = panel.selected_provider
        self._config_context = (panel.scope, panel.selected_key)
        self.endpoint_mode = endpoint
        status = panel.connection_status
        settings = parse_model_support(status["modalities"], status["generation"])
        definitions = (
            [
                (
                    "base_url",
                    "API base URL",
                    status["base_url"],
                    "Public HTTPS URL; blank restores the official URL",
                    300,
                )
            ]
            if endpoint
            else [
                (
                    "max_tokens",
                    "Maximum reply tokens",
                    str(settings["max_tokens"]),
                    "16 to 16384; default 4096",
                    5,
                ),
                (
                    "temperature",
                    "Creativity",
                    str(settings["temperature"]),
                    "0 to 2; default 0.4",
                    8,
                ),
                (
                    "effort",
                    "Reasoning effort (optional)",
                    settings["effort"],
                    "none, minimal, low, medium, high, max",
                    10,
                ),
                (
                    "context",
                    "Context token limit (optional)",
                    str(settings["context"] or ""),
                    "Leave blank for the provider default",
                    9,
                ),
            ]
        )
        self.fields = {}
        for key, label, default, placeholder, size in definitions:
            field = discord.ui.TextInput(
                label=label,
                default=default or None,
                placeholder=placeholder,
                max_length=size,
                required=key in {"max_tokens", "temperature"},
            )
            self.fields[key] = field
            self.add_item(field)

    @_config_action(thinking=True)
    async def on_submit(self, interaction: Any) -> None:
        if self.provider != self.panel.selected_provider or not active_selected(
            self.panel
        ):
            await _send(interaction, "This connection changed. Open Advanced again.")
            return
        status = self.panel.connection_status
        try:
            settings = parse_model_support(status["modalities"], status["generation"])
            endpoint = None
            if self.endpoint_mode:
                endpoint = str(self.fields["base_url"].value or "").strip()
            else:
                generation = (
                    f"reasoning={'on' if settings['reasoning'] else 'off'} "
                    + " ".join(
                        f"{key}={field.value}"
                        for key, field in self.fields.items()
                        if field.value
                    )
                )
                settings = parse_model_support(status["modalities"], generation)
            await update_settings(self.panel, interaction, settings, endpoint=endpoint)
        except ValueError as exc:
            await _send(interaction, str(exc))


class AdvancedButton(discord.ui.Button):
    def __init__(self, panel: Any, *, endpoint: bool = False):
        super().__init__(
            label="Edit API base URL" if endpoint else "Reply limits & creativity",
            row=3,
            custom_id=f"maxwell:config:connection:edit:{'url' if endpoint else 'response'}",
        )
        self.panel = panel
        self.endpoint_mode = endpoint
        self.provider = panel.selected_provider

    async def callback(self, interaction: Any) -> None:
        if not await self.panel.authorized(interaction):
            return
        if (
            self._config_context != (self.panel.scope, self.panel.selected_key)
            or not active_selected(self.panel)
            or self.panel.closed
            or self.panel.is_finished()
            or self.provider != self.panel.selected_provider
        ):
            await _send(interaction, "Open the current Advanced screen to continue.")
            return
        await interaction.response.send_modal(
            AdvancedModal(self.panel, endpoint=self.endpoint_mode)
        )


class DisconnectButton(discord.ui.Button):
    def __init__(self, panel: Any):
        super().__init__(
            label="Remove key & use Maxwell",
            style=discord.ButtonStyle.danger,
            row=2,
            custom_id="maxwell:config:connection:confirm_remove",
        )
        self.panel = panel

    @_config_action(thinking=True)
    async def callback(self, interaction: Any) -> None:
        vault = getattr(self.panel.bot, "_byok_vault", None)
        if vault is None:
            await _send(interaction, "Connection settings are unavailable.")
            return
        await asyncio.to_thread(vault.delete, self.panel.user_id)
        self.panel.selected_key = "byok"
        await refresh_panel(
            self.panel,
            interaction,
            "Personal key removed. Your messages now use Maxwell's default AI.",
        )


def build(panel: Any, nav_button: Any) -> None:
    if panel.selected_key == "byok_remove":
        panel.add_item(DisconnectButton(panel))
        panel.add_item(nav_button(panel, "byok", label="Keep connection", row=2))
    elif panel.selected_key == "byok_advanced":
        if not active_selected(panel) or not available(panel):
            panel.add_item(nav_button(panel, "byok", label="Back to connection", row=1))
            return
        panel.add_item(FeaturesSelect(panel))
        panel.add_item(ReasoningSelect(panel))
        panel.add_item(AdvancedButton(panel))
        panel.add_item(AdvancedButton(panel, endpoint=True))
    else:
        panel.add_item(ProviderSelect(panel))
        panel.add_item(ConnectButton(panel))
        if active_selected(panel) and available(panel):
            panel.add_item(nav_button(panel, "byok_advanced", label="Advanced", row=2))
        panel.add_item(TestButton(panel))
        if panel.connection_present:
            panel.add_item(
                nav_button(panel, "byok_remove", label="Use Maxwell's AI", row=3)
            )
        if (
            available(panel)
            and not panel.connection_error
            and panel.selected_provider in KEY_URLS
        ):
            panel.add_item(
                discord.ui.Button(
                    label="Get an API key", url=KEY_URLS[panel.selected_provider], row=3
                )
            )


def summary(panel: Any) -> str:
    if panel.connection_error == "unreadable" and not panel.connection_present:
        return "AI connection status unavailable"
    if panel.connection_error and panel.connection_present:
        return "Your saved AI connection is unavailable"
    status = panel.connection_status
    if status:
        return f"{status['provider_label']} · {status['model']}"
    return "Maxwell's default AI"


def render(panel: Any) -> str:
    if panel.selected_key == "byok_remove":
        return (
            "## Use Maxwell's AI\nRemove your personal API key and return to Maxwell's default AI for your messages?\n\n"
            "You can connect again later. Choose **Keep connection** to cancel."
        )
    if panel.selected_key == "byok_advanced":
        status = panel.connection_status
        if not active_selected(panel) or not available(panel):
            return (
                "## Advanced connection settings\nConnect the selected provider first."
            )
        settings = parse_model_support(status["modalities"], status["generation"])
        url = discord.utils.escape_markdown(status["base_url"])
        return (
            "## Advanced connection settings\nEnable only features your model supports. Text is always enabled. "
            "Images and audio are inputs; they do not enable image or voice generation. "
            "Enable reasoning before setting its effort.\n\n"
            f"**Model:** {discord.utils.escape_markdown(status['model'])}\n"
            f"**API base URL:** {url}\n**Reply token limit:** {settings['max_tokens']:,}\n"
            f"**Creativity:** {settings['temperature']}\n"
            f"**Reasoning effort:** {settings['effort'] or 'Provider default'}\n"
            f"**Context token limit:** {settings['context'] or 'Provider default'}\n\n{panel.notice}"
        )
    status = panel.connection_status
    active = discord.utils.escape_markdown(summary(panel))
    text = f"## Your AI connection\n**Currently using:** {active}\n\n"
    if panel.connection_error:
        text += "Personal connections are unavailable right now. Ask the bot operator for help.\n"
        if panel.connection_present:
            text += "Your saved key is still present. Remove it below to return to Maxwell's AI.\n"
    else:
        text += "Optional: replace Maxwell's model with your own provider for your messages, "
        text += "including chat, mentions, and app actions.\n\n"
        selected = PROVIDERS[panel.selected_provider]["label"]
        text += f"**1. Choose a provider**\n**2. Connect {selected}** with your API key and model name.\n"
        if status:
            text += f"\n**Saved key:** {status['masked_key']}\n"
            if not active_selected(panel):
                text += "Saving another provider replaces this connection. Picking a provider alone changes nothing.\n"
        if panel.selected_provider == "openai":
            text += "\nOpenAI uses API billing, separate from a ChatGPT subscription.\n"
        text += "\nThe provider receives your request and its included context. Keys are stored encrypted. "
        text += (
            "Test connection sends a small API request and may use provider credit.\n"
        )
    return text + f"\n{panel.notice}"
