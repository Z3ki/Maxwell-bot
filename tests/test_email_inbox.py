"""New mail lands in the inbox once, and a dismissed message stays dismissed."""

import asyncio

import pytest

from email_inbox import (
    EmailInboxPoller,
    MailPollState,
    _decode_header,
    _parse_fetch_response,
    email_item_id,
)
from inbox import InboxStore


def _cfg():
    return {
        "imap_host": "127.0.0.1",
        "imap_port": 993,
        "user": "maxwell@z3ki.dev",
        "password": "hunter2",
    }


def _mail(uid, subject="Invoice", sender="Ada", addr="ada@example.com"):
    return {
        "uid": uid,
        "subject": subject,
        "from_name": sender,
        "from_addr": addr,
        "date": "Mon, 24 Aug 2026 10:00:00 +0000",
        "message_id": f"<{uid}@example.com>",
        "snippet": "the thing you asked about",
    }


def _poller(tmp_path, store, mails_by_call):
    """A poller whose IMAP round trip is replaced by a scripted result.

    Each entry in mails_by_call is either a list of messages or an exception
    to raise; the last entry repeats once the script runs out.
    """
    poller = EmailInboxPoller(store, _cfg(), data_dir=str(tmp_path), interval=60)
    calls = {"n": 0}

    def fake_fetch(host, port, user, password, last_uid, limit):
        index = calls["n"]
        calls["n"] += 1
        batch = mails_by_call[min(index, len(mails_by_call) - 1)]
        if isinstance(batch, Exception):
            raise batch
        return [m for m in batch if m["uid"] > last_uid][:limit]

    return poller, fake_fetch, calls


def test_new_mail_becomes_an_inbox_notice(tmp_path, monkeypatch):
    store = InboxStore(str(tmp_path))
    poller, fake, _ = _poller(tmp_path, store, [[_mail(11), _mail(12, "Re: DNS")]])
    monkeypatch.setattr("email_inbox._fetch_new_sync", fake)

    async def run():
        assert await poller.poll_once() == 2
        items = await store.load_items()
        assert {i["id"] for i in items} == {email_item_id(11), email_item_id(12)}
        row = next(i for i in items if i["id"] == email_item_id(11))
        assert row["kind"] == "email"
        assert row["state"] == "unread"
        assert row["actor_name"] == "Ada"
        assert row["actor_id"] == "ada@example.com"
        assert row["payload"]["subject"] == "Invoice"
        assert row["payload"]["uid"] == "11"
        # The planner tail leads with sender and subject, not an actor id.
        rendered = store.render_planner(items)
        assert "Ada <ada@example.com>" in rendered
        assert '"Invoice"' in rendered

    asyncio.run(run())


def test_a_dismissed_message_is_not_refiled(tmp_path, monkeypatch):
    """The server still reports it UNSEEN; his decision has to stick."""
    store = InboxStore(str(tmp_path))
    poller, fake, _ = _poller(tmp_path, store, [[_mail(11)]])
    monkeypatch.setattr("email_inbox._fetch_new_sync", fake)

    async def run():
        assert await poller.poll_once() == 1
        await store.mark(email_item_id(11), "dismissed")
        # Rewind the high-water mark so the same UID comes back.
        poller.state.last_uid = 0
        assert await poller.poll_once() == 0
        row = await store.get(email_item_id(11))
        assert row["state"] == "dismissed"
        assert store.render_planner(await store.load_items()) == ""

    asyncio.run(run())


def test_high_water_mark_survives_a_restart(tmp_path, monkeypatch):
    store = InboxStore(str(tmp_path))
    poller, fake, _ = _poller(tmp_path, store, [[_mail(30), _mail(31)]])
    monkeypatch.setattr("email_inbox._fetch_new_sync", fake)

    async def run():
        await poller.poll_once()
        assert poller.state.last_uid == 31
        fresh = MailPollState(str(tmp_path))
        await fresh.load()
        assert fresh.last_uid == 31

    asyncio.run(run())


def test_a_failed_file_does_not_advance_the_mark(tmp_path, monkeypatch):
    """Losing a message forever is worse than announcing it twice."""

    class Broken(InboxStore):
        async def insert_if_absent(self, item):
            raise RuntimeError("disk full")

    store = Broken(str(tmp_path))
    poller, fake, _ = _poller(tmp_path, store, [[_mail(7)]])
    monkeypatch.setattr("email_inbox._fetch_new_sync", fake)

    async def run():
        assert await poller.poll_once() == 0
        assert poller.state.last_uid == 0

    asyncio.run(run())


def test_imap_failure_backs_off_and_recovers(tmp_path, monkeypatch):
    store = InboxStore(str(tmp_path))
    poller, fake, _ = _poller(
        tmp_path, store, [OSError("connection refused"), OSError("nope"), [_mail(3)]]
    )
    monkeypatch.setattr("email_inbox._fetch_new_sync", fake)

    async def run():
        assert poller.backoff_seconds() == 60
        await poller.poll_once()
        assert poller.backoff_seconds() == 120
        await poller.poll_once()
        assert poller.backoff_seconds() == 240
        assert await poller.poll_once() == 1
        assert poller.backoff_seconds() == 60

    asyncio.run(run())


def test_backoff_is_capped(tmp_path):
    store = InboxStore(str(tmp_path))
    poller = EmailInboxPoller(
        store, _cfg(), data_dir=str(tmp_path), interval=60, max_backoff=600
    )
    poller._failures = 99
    assert poller.backoff_seconds() == 600


def test_poller_is_inert_without_a_password(tmp_path):
    cfg = _cfg()
    cfg["password"] = ""
    poller = EmailInboxPoller(InboxStore(str(tmp_path)), cfg, data_dir=str(tmp_path))
    assert poller.configured() is False
    assert asyncio.run(poller.poll_once()) == 0


def test_encoded_headers_are_decoded():
    assert _decode_header("=?utf-8?q?caf=C3=A9?=") == "café"
    assert _decode_header(None) == ""
    assert _decode_header("plain subject") == "plain subject"


def test_fetch_response_shapes():
    header = b"Subject: hi\r\n"
    body = b"hello there"
    assert _parse_fetch_response(
        [(b"1 (BODY[HEADER]", header), (b" BODY[1]", body), b")"]
    ) == (header, body)
    assert _parse_fetch_response([]) == (b"", b"")


@pytest.mark.parametrize("uid,expected", [(1, "email_1"), ("42", "email_42")])
def test_item_ids(uid, expected):
    assert email_item_id(uid) == expected
