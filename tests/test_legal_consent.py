"""Hosted TOS must block first directed use until the user agrees."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import legal_consent


def _bot(tmp_path, *, admins=(), agreed=()):
    bot = SimpleNamespace(
        config=SimpleNamespace(
            DATA_DIR=str(tmp_path),
            MAXWELL_PUBLIC_BASE_URL="https://maxwell.example.test",
        ),
        _tos_users={},
        _tos_offered_at={},
        _admins={str(a) for a in admins},
        command_prefix=",",
        _directly_addressed=lambda _message: False,
    )
    bot._is_admin = lambda uid: str(uid) in bot._admins
    for uid in agreed:
        bot._tos_users[str(uid)] = legal_consent.TOS_VERSION
    return bot


class _Channel:
    def __init__(self, *, dm=False):
        self.sent = []
        self.recipient = SimpleNamespace(id=11) if dm else None

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        return SimpleNamespace(id=1)


def _message(author_id, *, content="hey", dm=False):
    channel = _Channel(dm=dm)
    message = SimpleNamespace(
        author=SimpleNamespace(id=author_id, bot=False),
        channel=channel,
        content=content,
        guild=None if dm else SimpleNamespace(id=99),
        user_install=False,
    )

    async def reply(**kwargs):
        payload = dict(kwargs)
        payload["_replied"] = True
        return await channel.send(**payload)

    message.reply = reply
    return message


def test_everyone_including_admins_must_agree(tmp_path):
    bot = _bot(tmp_path, admins=("1",), agreed=("2",))
    assert legal_consent.needs_consent(bot, "1") is True
    assert legal_consent.needs_consent(bot, "2") is False
    assert legal_consent.needs_consent(bot, "3") is True


def test_missing_consent_store_does_not_trip_test_stubs():
    bot = SimpleNamespace()
    assert legal_consent.needs_consent(bot, "9") is False


def test_stale_version_requires_a_new_agreement(tmp_path):
    bot = _bot(tmp_path)
    bot._tos_users["9"] = "0"
    assert legal_consent.needs_consent(bot, "9") is True


def test_agreement_survives_reload(tmp_path):
    bot = _bot(tmp_path)
    legal_consent.record_agreement(bot, "42")
    assert legal_consent.has_agreed(bot, "42") is True
    other = _bot(tmp_path)
    legal_consent.load(other)
    assert legal_consent.has_agreed(other, "42") is True
    assert legal_consent.needs_consent(other, "42") is False


def test_gate_sends_one_prompt_and_blocks(tmp_path, monkeypatch):
    bot = _bot(tmp_path)
    message = _message(11, dm=True)
    calls = []

    async def fake_offer(_bot, dest, *, reply_to=None, user_id=None):
        calls.append(dest)
        if reply_to is not None:
            await reply_to.reply(content="tos-prompt")
        else:
            await dest.send(content="tos-prompt")

    monkeypatch.setattr(legal_consent, "offer", fake_offer)
    monkeypatch.setattr(legal_consent.time, "monotonic", lambda: 12.0)

    async def run():
        first = await legal_consent.gate_message(bot, message)
        second = await legal_consent.gate_message(bot, message)
        return first, second

    first, second = asyncio.run(run())
    assert first == "tos_required"
    assert second == "tos_required"
    assert calls == [message.channel]
    assert message.channel.sent == [{"content": "tos-prompt", "_replied": True}]


def test_replying_agree_records_consent(tmp_path):
    bot = _bot(tmp_path)
    message = _message(11, content="agree", dm=True)
    assert asyncio.run(legal_consent.gate_message(bot, message)) == "tos_required"
    assert legal_consent.has_agreed(bot, 11) is True
    assert message.channel.sent
    assert message.channel.sent[0].get("_replied") is True
    assert "free" in message.channel.sent[0]["content"].lower()


def test_text_agree_edits_the_prompt(tmp_path):
    bot = _bot(tmp_path)
    edits = []

    class Prompt:
        async def edit(self, **kwargs):
            edits.append(kwargs)

    bot._tos_prompts = {"11": Prompt()}
    message = _message(11, content="agree", dm=True)
    assert asyncio.run(legal_consent.gate_message(bot, message)) == "tos_required"
    assert legal_consent.has_agreed(bot, 11) is True
    assert edits
    assert edits[0].get("view") is None
    assert "free" in edits[0]["content"].lower()
    assert message.channel.sent == []


def test_command_prefix_agree_also_counts(tmp_path):
    bot = _bot(tmp_path)
    message = _message(11, content=",agree")
    assert asyncio.run(legal_consent.gate_message(bot, message)) == "tos_required"
    assert legal_consent.has_agreed(bot, 11) is True


def test_agreed_user_is_not_prompted(tmp_path):
    bot = _bot(tmp_path, agreed=("11",))
    message = _message(11, dm=True)
    assert asyncio.run(legal_consent.gate_message(bot, message)) is None
    assert message.channel.sent == []


def test_agree_button_updates_the_prompt(tmp_path):
    bot = _bot(tmp_path)
    edited = []

    class Response:
        async def edit_message(self, **kwargs):
            edited.append(kwargs)

    interaction = SimpleNamespace(
        data={"custom_id": legal_consent.CUSTOM_AGREE},
        user=SimpleNamespace(id=77),
        response=Response(),
        message=None,
    )
    assert asyncio.run(legal_consent.handle_interaction(bot, interaction)) is True
    assert legal_consent.has_agreed(bot, 77) is True
    assert edited
    assert edited[0].get("view") is None
    assert "free" in edited[0]["content"].lower()
    assert "free" in edited[0]["embed"].title.lower()


def test_decline_button_updates_the_prompt(tmp_path):
    bot = _bot(tmp_path)
    edited = []

    class Response:
        async def edit_message(self, **kwargs):
            edited.append(kwargs)

    interaction = SimpleNamespace(
        data={"custom_id": legal_consent.CUSTOM_DECLINE},
        user=SimpleNamespace(id=77),
        response=Response(),
        message=None,
    )
    assert asyncio.run(legal_consent.handle_interaction(bot, interaction)) is True
    assert legal_consent.has_agreed(bot, 77) is False
    assert edited
    assert edited[0].get("view") is None
    assert "sitting this out" in edited[0]["embed"].title.lower()


def test_unknown_interaction_is_ignored(tmp_path):
    bot = _bot(tmp_path)
    interaction = SimpleNamespace(data={"custom_id": "nope"}, user=SimpleNamespace(id=1))
    assert asyncio.run(legal_consent.handle_interaction(bot, interaction)) is False


def test_directed_for_consent_covers_dm_ping_and_commands(tmp_path):
    bot = _bot(tmp_path)
    assert legal_consent.directed_for_consent(bot, _message(11, dm=True)) is True
    assert legal_consent.directed_for_consent(bot, _message(11, content=",help")) is True
    chatter = _message(11, content="lol")
    assert legal_consent.directed_for_consent(bot, chatter) is False
    bot._directly_addressed = lambda _message: True
    assert legal_consent.directed_for_consent(bot, chatter) is True


def test_legal_urls_use_public_base(tmp_path):
    bot = _bot(tmp_path)
    terms, privacy = legal_consent.legal_urls(bot)
    assert terms == "https://maxwell.example.test/terms/"
    assert privacy == "https://maxwell.example.test/privacy/"


def test_legal_pages_are_on_the_public_site():
    root = Path(__file__).resolve().parents[1] / "web"
    terms = (root / "terms" / "index.html").read_text()
    privacy = (root / "privacy" / "index.html").read_text()
    home = (root / "index.html").read_text()
    assert "public alpha" in terms.lower()
    assert "MIT" in terms
    assert "US$3" in terms
    assert "Discord user IDs" in privacy
    assert "localStorage" in privacy
    assert 'href="/terms/"' in home
    assert 'href="/privacy/"' in home
    assert "legal-gate.js" in home
