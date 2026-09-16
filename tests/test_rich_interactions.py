from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import discord

import plugin_manager
from plugin_manager import PluginContext, PluginManager
from plugins.maxwell_extras.rich_interactions import (
    RichInteractionStore,
    install_rich_interactions,
)
from plugins.maxwell_extras.tools import SendRichMessageTool


def _runtime(tmp_path: Path):
    seen = []
    bot = SimpleNamespace(tools={})

    async def on_message(message):
        seen.append(message)

    bot.on_message = on_message
    manager = PluginManager(
        bot,
        plugins_dir=str(tmp_path / "plugins"),
        data_dir=str(tmp_path / "data"),
        state_file=str(tmp_path / "data" / "plugins.json"),
    )
    bot.plugin_manager = manager
    manager.state["plugins"]["maxwell_extras"] = {
        "enabled_globally": True,
        "allowed_users": [],
        "denied_users": [],
    }
    ctx = PluginContext(manager, "maxwell_extras")
    tool = SendRichMessageTool(bot)
    store = install_rich_interactions(bot, ctx, tool)
    return bot, manager, tool, store, seen


def test_rich_interaction_store_round_trip(tmp_path):
    store = RichInteractionStore(tmp_path / "actions.json")
    token = store.add({"label": "Explain", "prompt": "Explain it"})
    assert token
    assert store.get(token)["prompt"] == "Explain it"


def test_rich_message_supports_callback_and_premium_buttons(tmp_path):
    _bot, _manager, tool, store, _seen = _runtime(tmp_path)
    buttons = tool._link_buttons(
        json.dumps(
            [
                {"label": "Explain", "style": "primary", "prompt": "Explain this"},
                {"style": "premium", "sku_id": "123456789"},
            ]
        )
    )
    assert len(buttons) == 2
    callback = buttons[0]
    assert callback.style is discord.ButtonStyle.primary
    assert callback.custom_id.startswith("maxwell:rich:")
    token = callback.custom_id.rsplit(":", 1)[1]
    assert store.get(token)["prompt"] == "Explain this"
    assert buttons[1].style is discord.ButtonStyle.premium
    assert int(buttons[1].sku_id) == 123456789


def test_component_click_becomes_maxwell_turn(tmp_path):
    _bot, manager, tool, _store, seen = _runtime(tmp_path)
    button = tool._link_buttons(
        json.dumps([{"label": "Continue", "style": "success", "prompt": "Continue the task"}])
    )[0]
    callback = manager._listeners["on_interaction"][0][1]

    class Response:
        def __init__(self):
            self.done = False
            self.deferred = False

        def is_done(self):
            return self.done

        async def defer(self, **_kwargs):
            self.done = True
            self.deferred = True

    interaction = SimpleNamespace(
        id=987,
        data={"custom_id": button.custom_id, "component_type": 2},
        response=Response(),
        followup=SimpleNamespace(send=None),
        user=SimpleNamespace(id=42, name="tester", display_name="tester", bot=False),
        channel_id=55,
        channel=SimpleNamespace(id=55, name="general", guild=None),
        guild=None,
        message=SimpleNamespace(content="Choose what Maxwell should do", embeds=[]),
    )

    asyncio.run(callback(interaction))
    assert interaction.response.deferred is True
    assert len(seen) == 1
    assert "Continue the task" in seen[0].content
    assert "new conversational turn" in seen[0].content


def test_plugin_context_allows_interactions_after_ui_upgrade(tmp_path):
    _bot, manager, _tool, _store, _seen = _runtime(tmp_path)
    assert "on_interaction" in plugin_manager.ALLOWED_EVENTS

    ctx = PluginContext(manager, "demo")

    async def handler(_interaction):
        return None

    ctx.on_event("on_interaction", handler)
    assert manager._listeners["on_interaction"][-1] == ("demo", handler)
