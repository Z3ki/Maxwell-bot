"""Owner-only Discord control panel for Maxwell.

Adds a global ``/owner`` application command that exposes safe runtime status,
redacted control exports, and validated live control edits. The command works
for both guild-installed and user-installed contexts but is authorized only by
MAXWELL_OWNER_IDS / CREATOR_ID.
"""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
from typing import Any

import discord

import user_install as ui
from api.state import _sanitize_control
from control_defaults import DEFAULT_CONTROL
from identity import configured_admin_ids
from utils import _atomic_json_write_sync

OWNER_COMMAND_NAME = "owner"
_SENSITIVE_KEY_PARTS = (
    "api_key",
    "secret",
    "password",
    "authorization",
    "cookie",
)

_ACTION_CHOICES = [
    {"name": "Overview", "value": "overview"},
    {"name": "Runtime", "value": "runtime"},
    {"name": "Controls", "value": "controls"},
    {"name": "Memory", "value": "memory"},
    {"name": "Autonomy", "value": "autonomy"},
    {"name": "Tools", "value": "tools"},
    {"name": "Plugins", "value": "plugins"},
    {"name": "Full data export", "value": "data"},
    {"name": "Set control", "value": "set"},
    {"name": "Enable boolean control", "value": "enable"},
    {"name": "Disable boolean control", "value": "disable"},
    {"name": "Reload control file", "value": "reload_control"},
    {"name": "User token usage", "value": "quota"},
    {"name": "Set user daily limit", "value": "quota_set"},
    {"name": "Clear user limit override", "value": "quota_clear"},
    {"name": "Reset user usage", "value": "quota_reset"},
    {"name": "Exempt user from limit", "value": "quota_exempt"},
    {"name": "Remove user exemption", "value": "quota_unexempt"},
]

OWNER_COMMAND = {
    "name": OWNER_COMMAND_NAME,
    "description": "Owner-only Maxwell status, data, and live controls.",
    "type": 1,
    "integration_types": [0, 1],
    "contexts": [0, 1, 2],
    "options": [
        {
            "name": "action",
            "description": "What to view or change",
            "type": 3,
            "required": False,
            "choices": _ACTION_CHOICES,
        },
        {
            "name": "key",
            "description": "Control key, or user ID for quota actions",
            "type": 3,
            "required": False,
            "max_length": 100,
        },
        {
            "name": "value",
            "description": "Control value, or token count for quota_set",
            "type": 3,
            "required": False,
            "max_length": 1000,
        },
    ],
}


def _options(interaction: Any) -> dict[str, Any]:
    data = ui._interaction_data(interaction)
    return dict(ui._option_pairs(data.get("options")))


def _is_owner(bot: Any, user_id: Any) -> bool:
    if user_id is None:
        return False
    return str(user_id) in configured_admin_ids(getattr(bot, "config", None))


def _is_sensitive_key(key: Any) -> bool:
    lowered = str(key or "").lower()
    return (
        any(part in lowered for part in _SENSITIVE_KEY_PARTS)
        or lowered == "token"
        or lowered.endswith("_token")
    )


def _redact(value: Any, key: str = "") -> Any:
    if key and _is_sensitive_key(key):
        return "<redacted>" if value not in (None, "") else value
    if isinstance(value, dict):
        return {str(k): _redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, tuple):
        return [_redact(item) for item in value]
    return value


def _control(bot: Any) -> dict[str, Any]:
    current = dict(DEFAULT_CONTROL)
    raw = getattr(bot, "_control", None)
    if isinstance(raw, dict):
        current.update(raw)
    return _sanitize_control(current)


def _fmt(value: Any) -> str:
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, (list, tuple, set, dict)):
        return str(len(value))
    text = str(value)
    return text if len(text) <= 80 else text[:77] + "..."


def _field_lines(control: dict[str, Any], keys: tuple[str, ...]) -> str:
    return "\n".join(
        f"`{key}`: **{_fmt(control.get(key))}**"
        for key in keys
        if key in control
    ) or "No data."


def _runtime_data(bot: Any) -> dict[str, Any]:
    guilds = list(getattr(bot, "guilds", None) or [])
    channels = sum(len(getattr(guild, "channels", None) or []) for guild in guilds)
    members = sum(int(getattr(guild, "member_count", 0) or 0) for guild in guilds)
    latency = getattr(bot, "latency", None)
    try:
        latency_ms = round(float(latency) * 1000, 1)
    except (TypeError, ValueError, OverflowError):
        latency_ms = None

    manager = getattr(bot, "plugin_manager", None)
    loaded_plugins = getattr(manager, "loaded_plugins", None) or {}
    tools = getattr(bot, "tools", None) or {}
    detached = getattr(bot, "_detached_tasks", None) or set()
    active_detached = sum(1 for task in detached if not getattr(task, "done", lambda: True)())
    user = getattr(bot, "user", None)
    config = getattr(bot, "config", None)
    model = (
        getattr(getattr(bot, "provider", None), "model", None)
        or getattr(config, "OLLAMA_MODEL", None)
        or "unknown"
    )
    return {
        "connected": bool(user),
        "bot_name": str(getattr(user, "display_name", None) or getattr(user, "name", "Maxwell")),
        "bot_user_id": str(getattr(user, "id", "") or ""),
        "latency_ms": latency_ms,
        "guilds": len(guilds),
        "channels": channels,
        "members_visible": members,
        "tools_loaded": len(tools),
        "plugins_loaded": len(loaded_plugins),
        "background_tasks": active_detached,
        "model": str(model),
        "owners_configured": len(configured_admin_ids(config)),
    }


def _plugin_data(bot: Any) -> dict[str, Any]:
    manager = getattr(bot, "plugin_manager", None)
    loaded = getattr(manager, "loaded_plugins", None) or {}
    state = getattr(manager, "state", None) or {}
    return {
        "loaded": sorted(str(name) for name in loaded),
        "state": _redact(state),
        "subscribed_events": (
            manager.subscribed_events()
            if manager is not None and callable(getattr(manager, "subscribed_events", None))
            else []
        ),
    }


def _embed(bot: Any, section: str) -> discord.Embed:
    control = _control(bot)
    runtime = _runtime_data(bot)
    embed = discord.Embed(
        title="Maxwell Owner Control",
        description=(
            "Owner-only live status and controls. Use `action:set`, `action:enable`, "
            "or `action:disable` with a control key to make a persistent change."
        ),
        color=discord.Color.blurple(),
    )

    if section in {"overview", "runtime"}:
        latency = runtime["latency_ms"]
        latency_text = f"{latency} ms" if latency is not None else "unknown"
        embed.add_field(
            name="Runtime",
            value=(
                f"Connected: **{'yes' if runtime['connected'] else 'no'}**\n"
                f"Latency: **{latency_text}**\n"
                f"Guilds: **{runtime['guilds']}** · Channels: **{runtime['channels']}**\n"
                f"Visible members: **{runtime['members_visible']}**\n"
                f"Background tasks: **{runtime['background_tasks']}**"
            ),
            inline=False,
        )
        embed.add_field(
            name="AI / Capacity",
            value=(
                f"Model: **{runtime['model']}**\n"
                f"AI concurrency: **{_fmt(control.get('ai_concurrency'))}**\n"
                f"Max tool iterations: **{_fmt(control.get('max_tool_iterations'))}**\n"
                f"Prompt budget: **{_fmt(control.get('prompt_context_budget'))}** chars\n"
                f"Max live output: **{_fmt(control.get('live_max_output_tokens'))}** tokens"
                f"\nDaily user limit: **{_fmt(control.get('daily_user_token_limit'))}** "
                f"({'on' if control.get('daily_user_token_limit_enabled') else 'off'})"
            ),
            inline=False,
        )
        embed.add_field(
            name="Loaded",
            value=(
                f"Tools: **{runtime['tools_loaded']}**\n"
                f"Plugins: **{runtime['plugins_loaded']}**\n"
                f"Owners: **{runtime['owners_configured']}**"
            ),
            inline=True,
        )
        if section == "runtime":
            return embed

    if section in {"overview", "controls"}:
        embed.add_field(
            name="Core controls",
            value=_field_lines(
                control,
                (
                    "bot_enabled",
                    "tools_enabled",
                    "typing_indicator",
                    "error_replies",
                    "error_details",
                    "require_direct_response",
                    "conversation_watch_enabled",
                    "reply_to_bots",
                ),
            ),
            inline=False,
        )
        if section == "controls":
            embed.set_footer(text="A complete redacted control JSON file is attached.")
            return embed

    if section in {"overview", "memory"}:
        embed.add_field(
            name="Memory",
            value=_field_lines(
                control,
                (
                    "store_memory",
                    "long_term_memory_enabled",
                    "cross_context_enabled",
                    "cross_context_extract_enabled",
                    "entity_memory_enabled",
                    "knowledge_graph_enabled",
                    "memory_history_messages",
                    "memory_context_budget",
                ),
            ),
            inline=False,
        )
        if section == "memory":
            return embed

    if section in {"overview", "autonomy"}:
        embed.add_field(
            name="Autonomy",
            value=_field_lines(
                control,
                (
                    "autonomy_enabled",
                    "autonomy_interval_seconds",
                    "autonomy_floor_enabled",
                    "autonomy_floor_cooldown_seconds",
                    "autonomy_disable_reasoning",
                    "enable_night_fallback",
                ),
            ),
            inline=False,
        )
        if section == "autonomy":
            return embed

    if section in {"overview", "tools"}:
        disabled = control.get("disabled_tools") or []
        embed.add_field(
            name="Tools",
            value=(
                _field_lines(
                    control,
                    (
                        "tools_enabled",
                        "native_tool_calls",
                        "tool_history_messages",
                        "tool_iteration_timeout_seconds",
                        "autofix_enabled",
                    ),
                )
                + f"\nDisabled tools: **{len(disabled)}**"
            ),
            inline=False,
        )
        if section == "tools":
            return embed

    if section == "plugins":
        plugins = _plugin_data(bot)
        loaded = plugins["loaded"]
        events = plugins["subscribed_events"]
        embed.add_field(
            name="Plugins",
            value=(
                "Loaded: " + (", ".join(f"`{name}`" for name in loaded) or "none")
                + "\nSubscribed events: "
                + (", ".join(f"`{name}`" for name in events) or "none")
            )[:1024],
            inline=False,
        )
        return embed

    embed.set_footer(
        text="Examples: /owner action:enable key:autonomy_enabled · "
        "/owner action:set key:ai_concurrency value:3"
    )
    return embed


def _json_file(payload: Any, filename: str) -> discord.File:
    data = json.dumps(payload, indent=2, sort_keys=True, default=str).encode("utf-8")
    return discord.File(io.BytesIO(data), filename=filename)


def _coerce_value(key: str, raw: Any) -> Any:
    default = DEFAULT_CONTROL[key]
    text = str(raw if raw is not None else "").strip()
    if isinstance(default, bool):
        lowered = text.lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
        raise ValueError("boolean values must be true/false, on/off, yes/no, or 1/0")
    if isinstance(default, int):
        try:
            return int(text)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("this control requires a number") from exc
    if isinstance(default, float):
        try:
            return float(text)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("this control requires a number") from exc
    if isinstance(default, (list, dict)):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError("this control requires valid JSON") from exc
        if not isinstance(parsed, type(default)):
            raise TypeError(f"this control requires a JSON {type(default).__name__}")
        return parsed
    return text


async def _set_control(bot: Any, key: str, raw_value: Any) -> tuple[Any, Any]:
    key = str(key or "").strip()
    if key not in DEFAULT_CONTROL:
        raise ValueError("unknown control key")
    if _is_sensitive_key(key):
        raise ValueError("sensitive controls cannot be changed through Discord")
    value = _coerce_value(key, raw_value)
    current = _control(bot)
    before = current.get(key)
    current[key] = value
    sanitized = _sanitize_control(current)
    after = sanitized.get(key)

    config = getattr(bot, "config", None)
    data_dir = getattr(config, "DATA_DIR", None)
    if not data_dir:
        raise RuntimeError("Maxwell DATA_DIR is unavailable")
    path = Path(data_dir) / "bot_control.json"
    await asyncio.to_thread(_atomic_json_write_sync, path, sanitized)
    bot._control = sanitized

    loader = getattr(bot, "_load_control", None)
    if callable(loader):
        try:
            loader(force=True)
        except TypeError:
            loader()
    return before, after


async def _reload_control(bot: Any) -> None:
    loader = getattr(bot, "_load_control", None)
    if not callable(loader):
        raise TypeError("live control loader is unavailable")
    try:
        loader(force=True)
    except TypeError:
        loader()


async def _send(
    interaction: Any,
    *,
    content: str | None = None,
    embed: discord.Embed | None = None,
    file: discord.File | None = None,
) -> None:
    kwargs: dict[str, Any] = {"ephemeral": True}
    if content is not None:
        kwargs["content"] = content
    if embed is not None:
        kwargs["embed"] = embed
    if file is not None:
        kwargs["file"] = file

    response = getattr(interaction, "response", None)
    send_message = getattr(response, "send_message", None)
    is_done = getattr(response, "is_done", None)
    done = bool(is_done()) if callable(is_done) else False
    if callable(send_message) and not done:
        await send_message(**kwargs)
        return
    followup = getattr(interaction, "followup", None)
    send = getattr(followup, "send", None)
    if callable(send):
        await send(**kwargs)
        return
    raise RuntimeError("interaction has no response transport")


async def handle_owner_interaction(bot: Any, interaction: Any) -> bool:
    data = ui._interaction_data(interaction)
    if str(data.get("name") or "") != OWNER_COMMAND_NAME:
        return False

    user = getattr(interaction, "user", None)
    uid = getattr(user, "id", None)
    if not _is_owner(bot, uid):
        await _send(interaction, content="This command is restricted to Maxwell's owner.")
        return True

    opts = _options(interaction)
    action = str(opts.get("action") or "overview").strip().lower()
    key = str(opts.get("key") or "").strip()
    value = opts.get("value")

    if action in {"overview", "runtime", "memory", "autonomy", "tools", "plugins"}:
        await _send(interaction, embed=_embed(bot, action))
        return True

    if action == "controls":
        await _send(
            interaction,
            embed=_embed(bot, "controls"),
            file=_json_file(_redact(_control(bot)), "maxwell-controls.json"),
        )
        return True

    if action == "data":
        payload = {
            "runtime": _runtime_data(bot),
            "controls": _redact(_control(bot)),
            "plugins": _plugin_data(bot),
        }
        await _send(
            interaction,
            content="Maxwell owner data export. Sensitive control values are redacted.",
            file=_json_file(payload, "maxwell-owner-data.json"),
        )
        return True

    if action == "reload_control":
        try:
            await _reload_control(bot)
        except Exception as exc:
            await _send(interaction, content=f"Control reload failed: {type(exc).__name__}: {exc}")
        else:
            await _send(interaction, content="Reloaded `bot_control.json` into the live bot.")
        return True

    if action in {"quota", "quota_set", "quota_clear", "quota_reset", "quota_exempt", "quota_unexempt"}:
        valid_discord = key.isdecimal() and len(key) <= 20
        valid_telegram = key.startswith("tg:") and key[3:].isdecimal() and len(key) <= 23
        if not (valid_discord or valid_telegram):
            await _send(interaction, content="Provide a Discord user ID or `tg:<Telegram user ID>` in `key`.")
            return True
        ledger = getattr(bot, "_daily_tokens", None)
        if ledger is None:
            await _send(interaction, content="Daily token ledger is unavailable.")
            return True
        try:
            if action == "quota_set":
                amount = int(str(value or ""))
                if not 1 <= amount <= 100_000_000:
                    raise ValueError("limit must be between 1 and 100,000,000")
                ledger.configure(key, limit=amount)
            elif action == "quota_clear":
                ledger.configure(key, clear=True)
            elif action == "quota_reset":
                ledger.configure(key, reset=True)
            elif action == "quota_exempt":
                ledger.configure(key, exempt=True)
            elif action == "quota_unexempt":
                ledger.configure(key, exempt=False)
            state = ledger.status(key, int(_control(bot)["daily_user_token_limit"]))
        except (TypeError, ValueError) as exc:
            await _send(interaction, content=f"Could not update quota: {exc}")
            return True
        await _send(interaction, content=(
            f"User `{key}` · {state['day']} UTC · "
            f"{state['spent']:,}/{state['limit']:,} tokens used "
            f"({state['reserved']:,} pending) · "
            f"exempt: {'yes' if state['exempt'] else 'no'}"
        ))
        return True

    if action in {"set", "enable", "disable"}:
        if not key:
            await _send(interaction, content="Provide `key` for this action.")
            return True
        if action in {"enable", "disable"}:
            if key not in DEFAULT_CONTROL or not isinstance(DEFAULT_CONTROL[key], bool):
                await _send(interaction, content=f"`{key}` is not a boolean control.")
                return True
            value = action == "enable"
        elif value is None:
            await _send(interaction, content="Provide `value` when using `action:set`.")
            return True

        try:
            before, after = await _set_control(bot, key, value)
        except (TypeError, ValueError, RuntimeError) as exc:
            await _send(interaction, content=f"Could not update `{key}`: {exc}")
        else:
            await _send(
                interaction,
                content=f"Updated `{key}`: **{_fmt(before)} → {_fmt(after)}**",
            )
        return True

    await _send(interaction, content=f"Unknown owner action: `{action}`")
    return True


def install_owner_control(bot: Any) -> None:
    """Register /owner and intercept it before the normal AI command path."""
    if getattr(bot, "_maxwell_owner_control_installed", False):
        return

    ui.register_command(OWNER_COMMAND)
    ui.register_interaction_handler(
        handle_owner_interaction, priority=10, name="owner_control"
    )
    bot._maxwell_owner_control_installed = True


__all__ = [
    "OWNER_COMMAND",
    "OWNER_COMMAND_NAME",
    "handle_owner_interaction",
    "install_owner_control",
]
