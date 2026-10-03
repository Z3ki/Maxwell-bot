"""Inbox notice handling."""

import asyncio
from types import SimpleNamespace

from bot import _tool_results_need_followup
from bot_tools import InboxActTool, InboxListTool
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
        assert "Ada" not in text
        assert "Hello" not in text
        shown = store.render_item(
            next(item for item in await store.load_items() if item["id"] == "email_11")
        )
        assert "Ada" in shown
        assert "Hello" in shown
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
    ):
        assert name in TOOL_PARAMETERS
        assert _tool_results_need_followup([f"Tool {name}: ok"])
    assert "action" in TOOL_PARAMETERS["inbox_act"]["properties"]


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
