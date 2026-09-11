"""Inbox notice handling and VC presence tools."""

import asyncio
from types import SimpleNamespace

from bot import _tool_results_need_followup
from bot_tools import (
    InboxActTool,
    InboxListTool,
    JoinVcTool,
    VcStatusTool,
    VcWhereTool,
    _find_member_voice,
    _is_voice_channel,
)
from inbox import InboxStore, apply_inbox_action, needs_decision
from tool_schemas import TOOL_PARAMETERS


def _item(iid, kind, created, state="unread", **extra):
    row = {
        "id": iid,
        "kind": kind,
        "state": state,
        "created_at": created,
        "actor_id": "",
        "actor_name": "someone",
        "summary": f"{kind} {iid}",
        "actions": ["dismiss"],
        "payload": {},
    }
    row.update(extra)
    return row


def test_empty_inbox_omits_planner_section(tmp_path):
    store = InboxStore(str(tmp_path))

    async def run():
        assert store.render_planner(await store.load_items()) == ""

    asyncio.run(run())


def test_notice_upsert_and_planner_budget(tmp_path):
    store = InboxStore(str(tmp_path))

    async def run():
        await store.upsert(
            {
                "id": "email_11",
                "kind": "email",
                "actor_id": "sender@example.test",
                "actor_name": "Ada",
                "summary": "Ada sent mail",
                "actions": ["read", "dismiss"],
                "payload": {"subject": "Hello"},
            }
        )
        text = store.render_planner(await store.load_items())
        assert "=== INBOX" in text
        assert "email_11" in text
        assert "Ada" in text
        assert len(text) <= 900

    asyncio.run(run())


def test_apply_inbox_action_read_and_dismiss(tmp_path):
    store = InboxStore(str(tmp_path))
    bot = SimpleNamespace(inbox=store)

    async def run():
        await store.add_notice(
            kind="guild_join",
            summary="Joined server Test",
            item_id="guild_1",
        )
        read = await apply_inbox_action(bot, action="read", item_id="guild_1")
        assert read == "Marked guild_1 read"
        assert (await store.get("guild_1"))["state"] == "read"

        dismissed = await apply_inbox_action(bot, action="dismiss", item_id="guild_1")
        assert dismissed == "Dismissed guild_1"
        assert (await store.get("guild_1"))["state"] == "dismissed"

    asyncio.run(run())


def test_legacy_friend_actions_fail_clearly(tmp_path):
    store = InboxStore(str(tmp_path))
    bot = SimpleNamespace(inbox=store)

    async def run():
        out = await apply_inbox_action(bot, action="accept", item_id="friend_1")
        assert "official bot" in out.lower()
        assert "cannot accept" in out.lower()

        out = await apply_inbox_action(bot, action="decline", user_id="1")
        assert "official bot" in out.lower()
        assert "cannot accept" in out.lower()

    asyncio.run(run())


def test_inbox_tools_list_and_read(tmp_path):
    store = InboxStore(str(tmp_path))
    bot = SimpleNamespace(inbox=store)
    message = SimpleNamespace()

    async def run():
        await store.add_notice(
            kind="email",
            summary="New message",
            actor_name="Dee",
            item_id="email_44",
            actions=["read", "dismiss"],
            payload={"subject": "Status"},
        )
        listed = await InboxListTool(bot).execute(message)
        assert "email_44" in listed

        acted = await InboxActTool(bot).execute(
            message, action="read", item_id="email_44"
        )
        assert acted == "Marked email_44 read"
        assert store.render_planner(await store.load_items()) == ""
        listed = await InboxListTool(bot).execute(message)
        assert "email_44" in listed

    asyncio.run(run())


class VoiceChannel:
    def __init__(self):
        self.id = 77
        self.name = "General"
        self.bitrate = 64000
        self.members = []
        self.guild = SimpleNamespace(name="Gild", text_channels=[])


def test_is_voice_channel_accepts_duck_type():
    assert _is_voice_channel(VoiceChannel()) is True
    assert _is_voice_channel(SimpleNamespace(name="text")) is False


def test_join_vc_and_where_and_status():
    voice = VoiceChannel()
    member = SimpleNamespace(
        id=55,
        display_name="Eli",
        voice=SimpleNamespace(channel=voice),
    )
    voice.members = [member]
    guild = SimpleNamespace(
        id=9,
        name="Gild",
        get_member=lambda uid: member if int(uid) == 55 else None,
        voice_channels=[voice],
        text_channels=[SimpleNamespace(send=True)],
    )
    voice.guild = guild

    class Bot:
        def __init__(self):
            self.config = SimpleNamespace(ENABLE_VC=True)
            self.guilds = [guild]
            self.voice_clients = []
            self.joined = None
            self.listened = False

        def get_channel(self, cid):
            return voice if int(cid) == 77 else None

        def _vc_get_client(self, _guild, _target):
            return None

        async def _vc_connect_channel(self, target):
            self.joined = target
            return SimpleNamespace(is_connected=lambda: True, channel=target)

        async def _vc_start_listening(self, _guild, _text, _target):
            self.listened = True
            return True

        def _vc_is_listening(self, _vc):
            return True

    bot = Bot()
    message = SimpleNamespace(guild=guild, channel=SimpleNamespace(send=True))

    async def run():
        joined = await JoinVcTool(bot).execute(message, voice_channel_id="77")
        assert "Joined" in joined
        assert bot.joined is voice
        assert bot.listened is True

        followed = await JoinVcTool(bot).execute(message, user_id="55")
        assert "Joined" in followed

        where = await VcWhereTool(bot).execute(message, user_id="<@55>")
        assert "General" in where
        assert "Eli" in where

        missing = await VcWhereTool(bot).execute(message, user_id="99")
        assert "not in a voice channel" in missing

        idle = await VcStatusTool(bot).execute(message)
        assert "Not connected" in idle

        bot.voice_clients = [
            SimpleNamespace(
                guild=guild,
                channel=voice,
                is_connected=lambda: True,
            )
        ]
        live = await VcStatusTool(bot).execute(message)
        assert "Connected" in live
        assert "Eli" in live

        found, ch = _find_member_voice(bot, 55, guild)
        assert found is member
        assert ch is voice

    asyncio.run(run())


def test_commands_post_accepts_legacy_inbox_act_queue(tmp_path, monkeypatch):
    """Old dashboard actions may still be queued, but execution fails safely."""
    import json

    import api.api_server as api

    monkeypatch.setattr(api, "DATA_DIR", tmp_path)
    (tmp_path / "bot_commands.json").write_text("[]", encoding="utf-8")

    class Req:
        def __init__(self, body):
            self._body = body

        async def json(self):
            return self._body

    async def run():
        bad = await api.commands_post(Req({"type": "inbox_act", "action": "nope"}))
        assert bad.status == 400
        missing = await api.commands_post(
            Req({"type": "inbox_act", "action": "accept"})
        )
        assert missing.status == 400
        resp = await api.commands_post(
            Req({"type": "inbox_act", "action": "accept", "item_id": "friend_1"})
        )
        assert resp.status == 200
        queued = json.loads((tmp_path / "bot_commands.json").read_text())
        assert queued[-1]["type"] == "inbox_act"
        assert queued[-1]["item_id"] == "friend_1"
        assert queued[-1]["status"] == "pending"

        monkeypatch.setattr(api, "_has_admin_auth", lambda _req: True)
        listed = await api.inbox_get(Req({}))
        assert listed.status == 200

    asyncio.run(run())


def test_new_tools_are_followup_and_have_schemas():
    for name in (
        "inbox_list",
        "inbox_act",
        "join_vc",
        "vc_status",
        "vc_where",
    ):
        assert name in TOOL_PARAMETERS
        assert _tool_results_need_followup([f"Tool {name}: ok"])
    assert "action" in TOOL_PARAMETERS["inbox_act"]["properties"]
    assert "user_id" in TOOL_PARAMETERS["vc_where"]["properties"]


def test_mail_burst_is_capped():
    store = InboxStore(".")
    items = [
        _item(f"email_{n}", "email", f"2026-08-24T10:{n:02d}:00Z")
        for n in range(20)
    ]
    ordered = store.planner_items(items)
    assert len(ordered) == 6
    assert ordered[0]["id"] == "email_19"


def test_newest_first_within_a_kind():
    store = InboxStore(".")
    ordered = store.planner_items(
        [
            _item("email_1", "email", "2026-08-24T09:00:00Z"),
            _item("email_2", "email", "2026-08-24T11:00:00Z"),
            _item("email_3", "email", "2026-08-24T10:00:00Z"),
        ]
    )
    assert [item["id"] for item in ordered] == ["email_2", "email_3", "email_1"]


def test_marking_read_demotes_without_clearing(tmp_path):
    store = InboxStore(str(tmp_path))

    async def run():
        await store.upsert(_item("email_1", "email", "2026-08-24T09:00:00Z"))
        await store.upsert(_item("email_2", "email", "2026-08-24T11:00:00Z"))
        assert await apply_inbox_action(
            SimpleNamespace(inbox=store), action="read", item_id="email_2"
        ) == "Marked email_2 read"
        ordered = store.planner_items(await store.load_items())
        assert [item["id"] for item in ordered] == ["email_1", "email_2"]

    asyncio.run(run())


def test_the_tail_stays_inside_its_budget():
    store = InboxStore(".")
    items = [
        _item(
            f"n_{n}",
            f"kind{n}",
            f"2026-08-24T10:{n:02d}:00Z",
            summary="x" * 300,
        )
        for n in range(40)
    ]
    assert len(store.render_planner(items)) <= 900


def test_a_read_notice_leaves_the_prompt_tail(tmp_path):
    store = InboxStore(str(tmp_path))

    async def run():
        await store.upsert(_item("email_1", "email", "2026-08-24T09:00:00Z"))
        assert "email_1" in store.render_planner(await store.load_items())
        await store.mark("email_1", "read")
        assert store.render_planner(await store.load_items()) == ""

    asyncio.run(run())


def test_the_inbox_tool_still_shows_a_read_notice(tmp_path):
    store = InboxStore(str(tmp_path))

    async def run():
        await store.upsert(_item("email_1", "email", "2026-08-24T09:00:00Z"))
        await store.mark("email_1", "read")
        items = await store.load_items()
        assert [item["id"] for item in store.planner_items(items)] == ["email_1"]
        assert store.planner_items(items, exclude_announced=True) == []

    asyncio.run(run())


def test_official_bot_inbox_has_no_pending_relationship_decisions():
    assert needs_decision({"actions": ["accept", "decline"]}) is False
    assert needs_decision({"actions": ["read", "dismiss"]}) is False
    assert needs_decision({}) is False
