"""Stateful Discord components for Maxwell rich messages and plugin UI APIs.

The rich-message tool historically supported URL buttons only. This module adds
persistent callback buttons without relying on in-memory View callbacks: each
button stores a small action record and encodes only a stable token in
``custom_id``. Discord returns that token through ``on_interaction`` and the
click is converted into a normal Maxwell interaction turn.

It also upgrades the plugin context with supported interaction subscriptions and
managed persistent views/dynamic items so plugins do not need to monkey-patch
the gateway dispatcher themselves.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from contextvars import ContextVar
from pathlib import Path
from types import MethodType
from typing import Any

import discord

from plugin_manager import PluginContext
from user_install import UserInstallMessageAdapter

_PREFIX = "maxwell:rich:"
_MAX_ACTIONS = 1500
_ACTION_TTL_SECONDS = 30 * 86400
_CURRENT_MESSAGE: ContextVar[dict[str, str] | None] = ContextVar(
    "maxwell_rich_message_context", default=None
)


def _json_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        raw = value
    elif isinstance(value, str):
        try:
            raw = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
    else:
        return []
    return [item for item in raw if isinstance(item, dict)]


class RichInteractionStore:
    """Tiny persistent action registry keyed by component token."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _read(self) -> dict[str, dict[str, Any]]:
        try:
            raw = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return {}
        actions = raw.get("actions") if isinstance(raw, dict) else None
        if not isinstance(actions, dict):
            return {}
        now = time.time()
        out: dict[str, dict[str, Any]] = {}
        for key, row in actions.items():
            if not isinstance(row, dict):
                continue
            created = float(row.get("created_at") or 0)
            if created and now - created > _ACTION_TTL_SECONDS:
                continue
            out[str(key)] = dict(row)
        return out

    def _write(self, actions: dict[str, dict[str, Any]]) -> None:
        if len(actions) > _MAX_ACTIONS:
            ordered = sorted(
                actions.items(), key=lambda pair: float(pair[1].get("created_at") or 0)
            )
            actions = dict(ordered[-_MAX_ACTIONS:])
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(
            json.dumps({"actions": actions}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temp, self.path)

    def add(self, row: dict[str, Any]) -> str:
        actions = self._read()
        token = secrets.token_urlsafe(9).replace("-", "a").replace("_", "b")[:14]
        while token in actions:
            token = secrets.token_urlsafe(9).replace("-", "a").replace("_", "b")[:14]
        stored = dict(row)
        stored["created_at"] = time.time()
        actions[token] = stored
        self._write(actions)
        return token

    def get(self, token: str) -> dict[str, Any] | None:
        actions = self._read()
        row = actions.get(str(token))
        if row is None:
            return None
        if bool(row.get("one_shot")):
            actions.pop(str(token), None)
            self._write(actions)
        return dict(row)


def _button_style(name: Any) -> discord.ButtonStyle:
    value = str(name or "secondary").strip().lower()
    mapping = {
        "primary": discord.ButtonStyle.primary,
        "blurple": discord.ButtonStyle.primary,
        "secondary": discord.ButtonStyle.secondary,
        "grey": discord.ButtonStyle.secondary,
        "gray": discord.ButtonStyle.secondary,
        "success": discord.ButtonStyle.success,
        "green": discord.ButtonStyle.success,
        "danger": discord.ButtonStyle.danger,
        "red": discord.ButtonStyle.danger,
        "link": discord.ButtonStyle.link,
        "premium": getattr(discord.ButtonStyle, "premium", discord.ButtonStyle.secondary),
    }
    return mapping.get(value, discord.ButtonStyle.secondary)


def _message_context_from_execute(args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, str]:
    # execute(message, mode="auto", title=None, description=None, ...)
    title = kwargs.get("title")
    description = kwargs.get("description")
    if title is None and len(args) >= 2:
        title = args[1]
    if description is None and len(args) >= 3:
        description = args[2]
    return {
        "title": str(title or "")[:256],
        "description": str(description or "")[:2000],
    }


def _patch_rich_message_tool(tool: Any, store: RichInteractionStore) -> None:
    if getattr(tool, "_maxwell_interactive_rich_patched", False):
        return

    original_execute = tool.execute
    original_builder = tool._link_buttons
    original_parameters = tool.get_parameters
    original_description = tool.get_description

    async def execute_wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        token = _CURRENT_MESSAGE.set(_message_context_from_execute(args, kwargs))
        try:
            return await original_execute(*args, **kwargs)
        finally:
            _CURRENT_MESSAGE.reset(token)

    def button_builder(self: Any, raw: Any) -> list[discord.ui.Button]:
        out: list[discord.ui.Button] = []
        context = _CURRENT_MESSAGE.get() or {}
        for item in _json_list(raw)[:5]:
            style_name = str(item.get("style") or ("link" if item.get("url") else "secondary"))
            style_key = style_name.strip().lower()

            if style_key == "premium" or item.get("sku_id") is not None:
                try:
                    sku_id = int(item.get("sku_id"))
                except (TypeError, ValueError):
                    continue
                premium = getattr(discord.ButtonStyle, "premium", None)
                if premium is None:
                    continue
                try:
                    out.append(discord.ui.Button(style=premium, sku_id=sku_id))
                except Exception:
                    continue
                continue

            url = str(item.get("url") or "").strip()
            if style_key == "link" or url:
                if not url.startswith(("https://", "http://")):
                    continue
                kwargs_button: dict[str, Any] = {
                    "label": str(item.get("label") or "Open")[:80],
                    "url": url,
                    "style": discord.ButtonStyle.link,
                    "disabled": bool(item.get("disabled", False)),
                }
                emoji = str(item.get("emoji") or "").strip()
                if emoji:
                    kwargs_button["emoji"] = emoji
                try:
                    out.append(discord.ui.Button(**kwargs_button))
                except Exception:
                    continue
                continue

            label = str(item.get("label") or "Continue")[:80]
            prompt = str(
                item.get("prompt")
                or item.get("action")
                or f'Respond naturally to the user choosing "{label}".'
            ).strip()[:3000]
            token = store.add(
                {
                    "label": label,
                    "prompt": prompt,
                    "title": context.get("title", ""),
                    "description": context.get("description", ""),
                    "one_shot": bool(item.get("one_shot", False)),
                }
            )
            kwargs_button = {
                "label": label,
                "style": _button_style(style_key),
                "custom_id": f"{_PREFIX}{token}",
                "disabled": bool(item.get("disabled", False)),
            }
            emoji = str(item.get("emoji") or "").strip()
            if emoji:
                kwargs_button["emoji"] = emoji
            try:
                out.append(discord.ui.Button(**kwargs_button))
            except Exception:
                continue

        # Preserve legacy behavior for malformed/older payloads if our parser
        # produced nothing.
        return out or list(original_builder(raw))

    def parameters_wrapper(self: Any) -> dict[str, Any]:
        schema = original_parameters()
        props = schema.setdefault("properties", {})
        buttons = props.setdefault("buttons", {"type": "string"})
        buttons["description"] = (
            "JSON list (max 5) of Discord buttons. Link: {label,url,emoji?}. "
            "Callback: {label,style:primary|secondary|success|danger,prompt,emoji?,"
            "disabled?,one_shot?}; clicking becomes a new Maxwell turn. Premium: "
            "{style:premium,sku_id}. Premium/SKU buttons open Discord purchase UI and "
            "do not fire callbacks."
        )
        return schema

    def description_wrapper(self: Any) -> str:
        return (
            original_description()
            + " Callback buttons can be Primary/Secondary/Success/Danger and route "
            "the click back into Maxwell as a conversational turn; Premium SKU buttons "
            "are also supported when configured in Discord."
        )

    tool.execute = MethodType(execute_wrapper, tool)
    tool._link_buttons = MethodType(button_builder, tool)
    tool.get_parameters = MethodType(parameters_wrapper, tool)
    tool.get_description = MethodType(description_wrapper, tool)
    tool._maxwell_interactive_rich_patched = True


def _upgrade_plugin_ui_api(bot: Any) -> None:
    """Ensure view tracking dicts exist. add_view / on_interaction are core."""
    manager = getattr(bot, "plugin_manager", None)
    if manager is None:
        return
    if not isinstance(getattr(manager, "_maxwell_plugin_views", None), dict):
        manager._maxwell_plugin_views = {}
    if not isinstance(getattr(manager, "_maxwell_dynamic_items", None), dict):
        manager._maxwell_dynamic_items = {}


def _component_data(interaction: Any) -> dict[str, Any]:
    data = getattr(interaction, "data", None)
    return data if isinstance(data, dict) else {}


def _source_message_text(interaction: Any) -> str:
    message = getattr(interaction, "message", None)
    if message is None:
        return ""
    parts: list[str] = []
    content = str(getattr(message, "content", "") or "").strip()
    if content:
        parts.append(content)
    for embed in list(getattr(message, "embeds", None) or [])[:3]:
        title = str(getattr(embed, "title", "") or "").strip()
        desc = str(getattr(embed, "description", "") or "").strip()
        if title:
            parts.append(title)
        if desc:
            parts.append(desc)
    return "\n".join(parts)[:2000]


async def _ack_interaction(interaction: Any) -> None:
    response = getattr(interaction, "response", None)
    if response is None:
        return
    is_done = getattr(response, "is_done", None)
    try:
        if callable(is_done) and is_done():
            return
    except Exception:
        pass
    defer = getattr(response, "defer", None)
    if callable(defer):
        try:
            await defer(thinking=True)
        except TypeError:
            await defer()


async def _expired(interaction: Any) -> None:
    response = getattr(interaction, "response", None)
    if response is not None:
        is_done = getattr(response, "is_done", None)
        try:
            done = bool(is_done()) if callable(is_done) else False
        except Exception:
            done = False
        send = getattr(response, "send_message", None)
        if not done and callable(send):
            await send("That Maxwell action has expired. Please ask for a new one.", ephemeral=True)
            return
    followup = getattr(interaction, "followup", None)
    send = getattr(followup, "send", None)
    if callable(send):
        await send("That Maxwell action has expired. Please ask for a new one.", ephemeral=True)


def install_rich_interactions(bot: Any, ctx: PluginContext, tool: Any) -> RichInteractionStore:
    """Enable stateful rich buttons and plugin-owned Discord UI."""

    _upgrade_plugin_ui_api(bot)
    store = RichInteractionStore(ctx.store_path("rich_interactions.json"))
    _patch_rich_message_tool(tool, store)

    async def on_interaction(interaction: Any) -> None:
        data = _component_data(interaction)
        custom_id = str(data.get("custom_id") or "")
        if not custom_id.startswith(_PREFIX):
            return
        token = custom_id[len(_PREFIX) :]
        row = store.get(token)
        if row is None:
            await _expired(interaction)
            return

        await _ack_interaction(interaction)
        label = str(row.get("label") or "action")
        action_prompt = str(row.get("prompt") or "").strip()
        values = data.get("values")
        source = _source_message_text(interaction)
        if not source:
            source = "\n".join(
                p for p in (str(row.get("title") or ""), str(row.get("description") or "")) if p
            )[:2000]

        pieces = [
            f'The user clicked the Discord button "{label}" on a Maxwell rich message.',
            f"Button instruction: {action_prompt}",
        ]
        if values:
            pieces.append(f"Selected values: {values}")
        if source:
            pieces.append(f"Original rich-message context:\n{source}")
        pieces.append(
            "Treat this click as the user's new conversational turn. Respond naturally and "
            "use tools when appropriate; do not merely describe that a button was clicked."
        )
        prompt = "\n\n".join(pieces)
        message = UserInstallMessageAdapter(
            interaction,
            prompt,
            note="Discord component interaction routed from send_rich_message.",
        )
        await bot.on_message(message)

    ctx.on_event("on_interaction", on_interaction)
    return store


__all__ = ["RichInteractionStore", "install_rich_interactions"]
