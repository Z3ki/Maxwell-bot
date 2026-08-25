"""Inbox store, friend-request actions, and VC presence tools."""

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
from inbox import InboxStore, apply_inbox_action, friend_item_id, needs_decision
from tool_schemas import TOOL_PARAMETERS


class IncomingRel:
    def __init__(self, uid, name="Ada"):
        self.type = SimpleNamespace(name="incoming_request")
        self.user = SimpleNamespace(id=uid, display_name=name, name=name)
        self.accepted = False
        self.deleted = False

    async def accept(self, confirm_stranger_request=False):
        self.accepted = True
        self.confirm = confirm_stranger_request

    async def delete(self):
        self.deleted = True


class FriendRel:
    def __init__(self, uid, name="Ada"):
        self.type = SimpleNamespace(name="friend")
        self.user = SimpleNamespace(id=uid, display_name=name, name=name)


def test_empty_inbox_omits_planner_section(tmp_path):
    store = InboxStore(str(tmp_path))

    async def run():
        assert store.render_planner(await store.load_items()) == ""

    asyncio.run(run())


def test_upsert_and_planner_budget(tmp_path):
    store = InboxStore(str(tmp_path))

    async def run():
        await store.upsert(
            {
                "id": friend_item_id("11"),
                "kind": "friend_request",
                "actor_id": "11",
                "actor_name": "Ada",
                "summary": "Ada sent a friend request",
                "actions": ["accept", "decline"],
            }
        )
        text = store.render_planner(await store.load_items())
        assert "=== INBOX" in text
        assert "friend_11" in text
        assert "Ada" in text
        assert len(text) <= 500

    asyncio.run(run())


def test_ingest_relationship_seed_add_friend_remove(tmp_path):
    store = InboxStore(str(tmp_path))
    incoming = IncomingRel(22, "Bea")

    async def run():
        first = await store.ingest_relationship(incoming, event="seed")
        second = await store.ingest_relationship(incoming, event="seed")
        assert first["id"] == "friend_22"
        assert first["state"] == "unread"
        assert second["id"] == first["id"]

        bot = SimpleNamespace(relationships=[incoming])
        added = await store.seed_from_bot(bot)
        assert added == 1

        await store.ingest_relationship(
            FriendRel(22, "Bea"), event="update", before=incoming
        )
        row = await store.get("friend_22")
        assert row["state"] == "acted"

        await store.ingest_relationship(incoming, event="add")
        await store.ingest_relationship(incoming, event="remove")
        gone = await store.get("friend_22")
        assert gone["state"] == "dismissed"

    asyncio.run(run())


def test_apply_inbox_action_accept_decline_dismiss(tmp_path):
    store = InboxStore(str(tmp_path))
    rel = IncomingRel(33, "Cara")

    class Bot:
        def __init__(self):
            self.inbox = store

        def get_relationship(self, uid):
            return rel if int(uid) == 33 else None

    async def run():
        await store.ingest_relationship(rel, event="add")
        bot = Bot()
        accepted = await apply_inbox_action(
            bot, action="accept", item_id="friend_33"
        )
        assert "Accepted" in accepted
        assert rel.accepted is True
        assert (await store.get("friend_33"))["state"] == "acted"

        await store.ingest_relationship(rel, event="add")
        declined = await apply_inbox_action(
            bot, action="decline", user_id="33"
        )
        assert "Declined" in declined
        assert rel.deleted is True

        await store.add_notice(
            kind="guild_join",
            summary="Joined server Test",
            item_id="guild_1",
        )
        dismissed = await apply_inbox_action(
            bot, action="dismiss", item_id="guild_1"
        )
        assert "Dismissed" in dismissed
        assert (await store.get("guild_1"))["state"] == "dismissed"

        bad = await apply_inbox_action(bot, action="accept", item_id="guild_1")
        assert bad.startswith("Error:")

    asyncio.run(run())


def test_inbox_tools_list_and_act(tmp_path):
    store = InboxStore(str(tmp_path))
    rel = IncomingRel(44, "Dee")
    bot = SimpleNamespace(inbox=store, get_relationship=lambda uid: rel)
    message = SimpleNamespace()

    async def run():
        await store.ingest_relationship(rel, event="add")
        listed = await InboxListTool(bot).execute(message)
        assert "friend_44" in listed
        acted = await InboxActTool(bot).execute(
            message, action="accept", item_id="friend_44"
        )
        assert "Accepted" in acted
        empty = await InboxListTool(bot).execute(message)
        assert "empty" in empty.lower()

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


def test_commands_post_accepts_inbox_act(tmp_path, monkeypatch):
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
        missing = await api.commands_post(Req({"type": "inbox_act", "action": "accept"}))
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


def test_a_waiting_person_outranks_a_pile_of_mail(tmp_path):
    """Mail arrives in bursts; a friend request must not be pushed out."""
    store = InboxStore(str(tmp_path))
    items = [
        _item(f"email_{n}", "email", f"2026-08-24T10:{n:02d}:00Z") for n in range(20)
    ]
    items.append(_item("friend_9", "friend_request", "2026-08-24T09:00:00Z"))

    ordered = store.planner_items(items)
    assert ordered[0]["id"] == "friend_9"
    # Mail is capped, so it cannot fill the tail on its own.
    assert sum(1 for i in ordered if i["kind"] == "email") == 6


def test_newest_first_within_a_kind(tmp_path):
    store = InboxStore(str(tmp_path))
    ordered = store.planner_items(
        [
            _item("email_1", "email", "2026-08-24T09:00:00Z"),
            _item("email_2", "email", "2026-08-24T11:00:00Z"),
            _item("email_3", "email", "2026-08-24T10:00:00Z"),
        ]
    )
    assert [i["id"] for i in ordered] == ["email_2", "email_3", "email_1"]


def test_marking_read_demotes_without_clearing(tmp_path):
    store = InboxStore(str(tmp_path))

    async def run():
        await store.upsert(_item("email_1", "email", "2026-08-24T09:00:00Z"))
        await store.upsert(_item("email_2", "email", "2026-08-24T11:00:00Z"))
        assert await apply_inbox_action(
            SimpleNamespace(inbox=store), action="read", item_id="email_2"
        ) == "Marked email_2 read"
        ordered = store.planner_items(await store.load_items())
        # Still there, but the unread one now leads.
        assert [i["id"] for i in ordered] == ["email_1", "email_2"]

    asyncio.run(run())


def test_accept_on_an_email_explains_itself(tmp_path):
    store = InboxStore(str(tmp_path))

    async def run():
        await store.upsert(
            _item("email_1", "email", "2026-08-24T09:00:00Z", actions=["read", "dismiss"])
        )
        out = await apply_inbox_action(
            SimpleNamespace(inbox=store), action="accept", item_id="email_1"
        )
        assert "not valid for a email" in out
        # It names the actions that would have worked.
        assert "dismiss" in out and "read" in out

    asyncio.run(run())


def test_the_tail_stays_inside_its_budget(tmp_path):
    store = InboxStore(str(tmp_path))
    items = [
        _item(f"n_{n}", f"kind{n}", f"2026-08-24T10:{n:02d}:00Z", summary="x" * 300)
        for n in range(40)
    ]
    text = store.render_planner(items)
    assert len(text) <= 900


# ─── an announced notice stops being announced ──────────────────────────


def test_a_read_notice_leaves_the_prompt_tail(tmp_path):
    """The reported bug: the same email announced three times, reworded.

    Nothing marked a notice as said, and `read` kept it in the tail, so every
    prompt carried it again and he narrated it again — "update from z3ki…",
    "z3ki already replied…", "update: z3ki just replied…".
    """
    store = InboxStore(str(tmp_path))

    async def run():
        await store.upsert(_item("email_1", "email", "2026-08-24T09:00:00Z"))
        assert "email_1" in store.render_planner(await store.load_items())

        await store.mark("email_1", "read")
        assert store.render_planner(await store.load_items()) == ""

    asyncio.run(run())


def test_a_read_request_someone_is_waiting_on_stays(tmp_path):
    # Reading a friend request does not answer it, so it keeps showing.
    store = InboxStore(str(tmp_path))

    async def run():
        await store.upsert(
            _item(
                "friend_9",
                "friend_request",
                "2026-08-24T09:00:00Z",
                actions=["accept", "decline"],
            )
        )
        await store.mark("friend_9", "read")
        assert "friend_9" in store.render_planner(await store.load_items())

    asyncio.run(run())


def test_the_inbox_tool_still_shows_a_read_notice(tmp_path):
    # It left the prompt tail, not the inbox. "Show me my inbox" shows it.
    store = InboxStore(str(tmp_path))

    async def run():
        await store.upsert(_item("email_1", "email", "2026-08-24T09:00:00Z"))
        await store.mark("email_1", "read")
        items = await store.load_items()
        assert [i["id"] for i in store.planner_items(items)] == ["email_1"]
        assert store.planner_items(items, exclude_announced=True) == []

    asyncio.run(run())


def test_needs_decision_reads_the_actions(tmp_path):
    assert needs_decision({"actions": ["accept", "decline"]}) is True
    assert needs_decision({"actions": ["ACCEPT"]}) is True
    assert needs_decision({"actions": ["read", "dismiss"]}) is False
    assert needs_decision({}) is False
