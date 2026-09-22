"""The legal links are delivered once by DM without blocking use."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import legal_notice


def _bot(tmp_path):
    bot = SimpleNamespace(
        config=SimpleNamespace(DATA_DIR=str(tmp_path), MAXWELL_PUBLIC_BASE_URL="https://maxwell.example.test"),
        command_prefix=",",
        _directly_addressed=lambda _message: False,
    )
    legal_notice.install(bot)
    return bot


class User:
    def __init__(self, uid=11, *, fail=False):
        self.id = uid
        self.bot = False
        self.fail = fail
        self.sent = []

    async def send(self, content):
        if self.fail:
            raise RuntimeError("DMs closed")
        self.sent.append(content)


def _message(user, *, content="hey", dm=False):
    return SimpleNamespace(
        author=user,
        channel=SimpleNamespace(recipient=user if dm else None),
        content=content,
        guild=None if dm else SimpleNamespace(id=99),
        user_install=False,
    )


def test_first_notice_contains_links_and_is_sent_once(tmp_path):
    bot = _bot(tmp_path)
    user = User()
    asyncio.run(legal_notice.notify_user(bot, user))
    asyncio.run(legal_notice.notify_user(bot, user))
    assert len(user.sent) == 1
    assert "https://maxwell.example.test/terms/" in user.sent[0]
    assert "https://maxwell.example.test/privacy/" in user.sent[0]
    assert "agree" not in user.sent[0].lower()
    assert legal_notice.has_notice(bot, user.id)
    other = _bot(tmp_path)
    asyncio.run(legal_notice.notify_user(other, user))
    assert len(user.sent) == 1


def test_failed_dm_does_not_record_notice_and_can_retry(tmp_path, caplog):
    bot = _bot(tmp_path)
    user = User(fail=True)
    asyncio.run(legal_notice.notify_user(bot, user))
    assert not legal_notice.has_notice(bot, user.id)
    assert "Could not DM legal notice" in caplog.text
    user.fail = False
    asyncio.run(legal_notice.notify_user(bot, user))
    assert legal_notice.has_notice(bot, user.id)
    assert len(user.sent) == 1


def test_parallel_first_interactions_send_only_one_dm(tmp_path):
    bot = _bot(tmp_path)
    user = User()

    async def run():
        await asyncio.gather(*(legal_notice.notify_user(bot, user) for _ in range(5)))

    asyncio.run(run())
    assert len(user.sent) == 1


def test_slow_dm_does_not_hold_up_other_users(tmp_path):
    bot = _bot(tmp_path)
    slow = User(11)
    fast = User(12)
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_send(content):
        started.set()
        await release.wait()
        slow.sent.append(content)

    slow.send = slow_send

    async def run():
        pending = asyncio.create_task(legal_notice.notify_user(bot, slow))
        await started.wait()
        try:
            await asyncio.wait_for(legal_notice.notify_user(bot, fast), timeout=1)
            assert len(fast.sent) == 1
        finally:
            release.set()
            await pending

    asyncio.run(run())
    assert len(slow.sent) == 1


def test_existing_agreements_are_imported(tmp_path):
    (tmp_path / "tos_consent.json").write_text(json.dumps({"users": {"42": {"version": "0", "agreed_at": 1}}}))
    bot = _bot(tmp_path)
    user = User(42)
    asyncio.run(legal_notice.notify_user(bot, user))
    assert user.sent == []
    assert legal_notice.has_notice(bot, 42)


def test_directed_interaction_covers_dm_ping_and_commands(tmp_path):
    bot = _bot(tmp_path)
    user = User()
    assert legal_notice.directed_interaction(bot, _message(user, dm=True))
    assert legal_notice.directed_interaction(bot, _message(user, content=",help"))
    chatter = _message(user)
    assert not legal_notice.directed_interaction(bot, chatter)
    bot._directly_addressed = lambda _message: True
    assert legal_notice.directed_interaction(bot, chatter)


def test_public_site_has_links_without_consent_gate():
    root = Path(__file__).resolve().parents[1] / "web"
    home = (root / "index.html").read_text()
    terms = (root / "terms" / "index.html").read_text()
    privacy = (root / "privacy" / "index.html").read_text()
    assert 'href="/terms/"' in home
    assert 'href="/privacy/"' in home
    assert "legal-gate.js" not in home
    assert "legal-gate" not in home
    assert "consent prompt" not in privacy
    assert "clicking Agree" not in terms
