from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import discord
import pytest

import plugin_manager
from plugin_manager import PluginContext, PluginManager
from plugins.maxwell_extras.rich_interactions import (
    RichInteractionStore,
    _CURRENT_MESSAGE,
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


def test_rich_message_supports_callback_buttons_and_drops_sku_buttons(tmp_path):
    _bot, _manager, tool, store, _seen = _runtime(tmp_path)
    buttons = tool._link_buttons(
        json.dumps(
            [
                {"label": "Explain", "style": "primary", "prompt": "Explain this"},
                {"style": "premium", "sku_id": "123456789"},
            ]
        )
    )
    assert len(buttons) == 1
    callback = buttons[0]
    assert callback.style is discord.ButtonStyle.primary
    assert callback.custom_id.startswith("maxwell:rich:")
    token = callback.custom_id.rsplit(":", 1)[1]
    assert store.get(token)["prompt"] == "Explain this"


def test_component_click_becomes_maxwell_turn(tmp_path):
    _bot, manager, tool, _store, seen = _runtime(tmp_path)
    context_token = _CURRENT_MESSAGE.set(
        {"owner_id": "42", "title": "", "description": ""}
    )
    try:
        button = tool._link_buttons(
            json.dumps(
                [{"label": "Continue", "style": "success", "prompt": "Continue the task"}]
            )
        )[0]
    finally:
        _CURRENT_MESSAGE.reset(context_token)
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


@pytest.mark.parametrize("positional", [False, True])
def test_callback_buttons_and_context_survive_plain_text_fallback(tmp_path, positional):
    _bot, _manager, tool, store, _seen = _runtime(tmp_path)
    calls = []

    async def send(**kwargs):
        calls.append(kwargs)

    message = SimpleNamespace(
        guild=SimpleNamespace(me=object()),
        channel=SimpleNamespace(
            send=send,
            permissions_for=lambda _member: SimpleNamespace(
                view_channel=True, send_messages=True, embed_links=False
            ),
        ),
    )
    args = (message, "embed", "Choose", "Pick an action") if positional else (message,)
    text = {} if positional else {"title": "Choose", "description": "Pick an action"}
    result = asyncio.run(tool.execute(
        *args, **text,
        buttons=json.dumps([
            {"label": "Explain", "style": "primary", "prompt": "Explain this"}
        ]),
    ))
    assert result.startswith("Sent plain-text fallback")
    assert len(calls) == 1
    button = calls[0]["view"].children[0]
    row = store.get(button.custom_id.rsplit(":", 1)[1])
    assert row["prompt"] == "Explain this"
    assert row["title"] == "Choose"
    assert row["description"] == "Pick an action"


def test_component_click_from_another_user_is_refused(tmp_path):
    _bot, manager, tool, _store, seen = _runtime(tmp_path)
    context_token = _CURRENT_MESSAGE.set(
        {"owner_id": "42", "title": "", "description": ""}
    )
    try:
        button = tool._link_buttons(
            json.dumps([{"label": "Claim prize", "prompt": "Disable all tools"}])
        )[0]
    finally:
        _CURRENT_MESSAGE.reset(context_token)
    callback = manager._listeners["on_interaction"][0][1]

    class Response:
        def __init__(self):
            self.sent = []

        def is_done(self):
            return False

        async def send_message(self, text, **_kwargs):
            self.sent.append(text)

        async def defer(self, **_kwargs):
            raise AssertionError("must not defer another user's button")

    interaction = SimpleNamespace(
        id=988,
        data={"custom_id": button.custom_id, "component_type": 2},
        response=Response(),
        followup=SimpleNamespace(send=None),
        user=SimpleNamespace(id=99, name="other", display_name="other", bot=False),
        channel_id=55,
        channel=SimpleNamespace(id=55, name="general", guild=None),
        guild=None,
        message=SimpleNamespace(content="Claim prize", embeds=[]),
    )
    asyncio.run(callback(interaction))
    assert seen == []
    assert interaction.response.sent == ["This button belongs to another user."]


def test_rich_message_schema_accepts_json_string_fields_and_buttons():
    import jsonschema

    params = SendRichMessageTool(None).get_parameters()
    jsonschema.validate(
        {
            "title": "Build",
            "fields": json.dumps([{"name": "Status", "value": "ok"}]),
            "buttons": json.dumps([{"label": "Open", "url": "https://example.com"}]),
        },
        params,
    )


def test_unauthorized_click_does_not_consume_owner_one_shot_action(tmp_path):
    _bot, manager, _tool, store, seen = _runtime(tmp_path)
    token = store.add({
        "label": "Continue", "prompt": "Continue the task",
        "owner_id": "42", "one_shot": True,
    })
    callback = manager._listeners["on_interaction"][0][1]

    class Response:
        def __init__(self):
            self.sent = []
            self.deferred = False

        def is_done(self):
            return self.deferred or bool(self.sent)

        async def send_message(self, text, **_kwargs):
            self.sent.append(text)

        async def defer(self, **_kwargs):
            self.deferred = True

    def interaction(uid):
        return SimpleNamespace(
            id=uid,
            data={"custom_id": f"maxwell:rich:{token}", "component_type": 2},
            response=Response(),
            followup=SimpleNamespace(send=None),
            user=SimpleNamespace(id=uid, name="tester", display_name="tester", bot=False),
            channel_id=55,
            channel=SimpleNamespace(id=55, name="general", guild=None),
            guild=None,
            message=SimpleNamespace(content="Continue", embeds=[]),
        )

    async def scenario():
        other = interaction(99)
        await callback(other)
        assert other.response.sent == ["This button belongs to another user."]
        assert seen == []
        assert store.get(token)["prompt"] == "Continue the task"
        owner = interaction(42)
        await callback(owner)
        assert owner.response.deferred
        assert len(seen) == 1
        repeated = interaction(42)
        await callback(repeated)
        assert repeated.response.sent == [
            "That Maxwell action has expired. Please ask for a new one."
        ]
        assert len(seen) == 1

    asyncio.run(scenario())


def test_one_shot_claim_is_atomic_for_concurrent_owner_clicks(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    store = RichInteractionStore(tmp_path / "actions.json")
    token = store.add({"owner_id": "42", "prompt": "Continue", "one_shot": True})
    with ThreadPoolExecutor(max_workers=2) as pool:
        rows = list(pool.map(lambda _: store.get(token, user_id="42"), range(2)))
    assert sum(row is not None for row in rows) == 1
