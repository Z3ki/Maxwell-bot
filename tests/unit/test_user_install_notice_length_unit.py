"""The one-time terms note never pushes a private reply past Discord's limit."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from bot import MaxwellBot
from plugins.maxwell_extras.user_preferences import UserPreferenceStore
from user_install import DISCORD_MESSAGE_LIMIT, UserInstallSession, _split_for_note


class Followup:
    def __init__(self, fail_first=False):
        self.payloads = []
        self.fail_first = fail_first

    async def send(self, content=None, **kwargs):
        if self.fail_first:
            self.fail_first = False
            raise RuntimeError("discord rejected the payload")
        self.payloads.append({"content": content, **kwargs})
        return SimpleNamespace(id=len(self.payloads), content=content)


class Response:
    def is_done(self):
        return True


def _session(tmp_path, followup=None):
    store = UserPreferenceStore(tmp_path / "user_preferences.json")
    bot = SimpleNamespace(
        _user_preferences=store,
        config=SimpleNamespace(MAXWELL_PUBLIC_BASE_URL="https://operator.example.test"),
    )
    interaction = SimpleNamespace(
        id=99,
        channel_id=555,
        channel=SimpleNamespace(name="general", id=555),
        guild=None,
        user=SimpleNamespace(id=2, name="u", display_name="u", bot=False),
        data={"name": "maxwell", "options": []},
        response=Response(),
        followup=followup or Followup(),
        client=bot,
    )
    return UserInstallSession(interaction, visibility="private"), interaction


def _contents(interaction):
    return [p["content"] for p in interaction.followup.payloads]


def test_short_reply_carries_the_note_in_one_message(tmp_path):
    session, interaction = _session(tmp_path)
    asyncio.run(session.send("Here is the answer."))
    (only,) = _contents(interaction)
    assert "Terms" in only and only.endswith("Here is the answer.")
    assert len(only) <= DISCORD_MESSAGE_LIMIT


def test_long_first_chunk_is_split_instead_of_overflowing(tmp_path):
    session, interaction = _session(tmp_path)
    chunk = MaxwellBot._split_response("word " * 1000, limit=1900)[0]
    assert len(chunk) > 1800
    asyncio.run(session.send(chunk))
    contents = _contents(interaction)
    assert len(contents) == 2
    assert all(len(c) <= DISCORD_MESSAGE_LIMIT for c in contents)
    assert "Terms" in contents[0] and "Terms" not in contents[1]
    note_end = contents[0].index("\n\n") + 2
    rebuilt = (contents[0][note_end:] + " " + contents[1]).split()
    assert rebuilt == chunk.split()


def test_note_is_shown_only_once_per_session(tmp_path):
    session, interaction = _session(tmp_path)
    asyncio.run(session.send("first"))
    asyncio.run(session.send("second"))
    assert _contents(interaction)[1] == "second"


def test_failed_send_releases_the_note_for_the_next_message(tmp_path):
    session, interaction = _session(tmp_path, Followup(fail_first=True))
    with pytest.raises(RuntimeError):
        asyncio.run(session.send("answer"))
    asyncio.run(session.send("fallback error text"))
    (only,) = _contents(interaction)
    assert "Terms" in only and only.endswith("fallback error text")


def test_files_and_views_ride_on_the_last_part(tmp_path):
    session, interaction = _session(tmp_path)
    marker = object()
    asyncio.run(session.send("x " * 990, view=marker))
    first, last = interaction.followup.payloads
    assert "view" not in first and last["view"] is marker


@pytest.mark.parametrize(
    "note_len, body, expected_head",
    [
        (10, "short", "short"),
        (DISCORD_MESSAGE_LIMIT, "body", ""),
    ],
)
def test_split_for_note_edges(note_len, body, expected_head):
    head, rest = _split_for_note("n" * note_len, body)
    assert head == expected_head
    assert (head + rest) == body or (head == "" and rest == body)


def test_split_for_note_hard_cuts_unbroken_text():
    body = "x" * 3000
    head, rest = _split_for_note("n" * 100, body)
    assert len("n" * 100) + 2 + len(head) <= DISCORD_MESSAGE_LIMIT
    assert head + rest == body
